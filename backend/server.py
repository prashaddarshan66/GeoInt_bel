"""GEOINT Change Detection backend.

FastAPI + MongoDB. AOI/imagery/change-event persistence with GeoJSON, async
processing jobs via BackgroundTasks, CDSE STAC search with DEMO fallback.
"""
from __future__ import annotations

import os
import uuid
import math
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from fastapi import FastAPI, APIRouter, HTTPException, BackgroundTasks, Query
from fastapi.responses import HTMLResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field

from cdse import cdse_client
from orchestrator import run_change_detection

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

MAX_AOI_AREA_KM2 = float(os.environ.get("MAX_AOI_AREA_KM2", "50"))
MAX_DATE_RANGE_DAYS = int(os.environ.get("MAX_DATE_RANGE_DAYS", "730"))
DEFAULT_CLOUD_THRESHOLD = int(os.environ.get("DEFAULT_CLOUD_THRESHOLD", "20"))

app = FastAPI(title="GEOINT Change Detection API")
api = APIRouter(prefix="/api/v1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("geoint")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def bbox_from_polygon(coords: list[list[list[float]]]) -> list[float]:
    ring = coords[0]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return [min(xs), min(ys), max(xs), max(ys)]


def polygon_area_km2(coords: list[list[list[float]]]) -> float:
    ring = coords[0]
    if len(ring) < 4:
        return 0.0
    mean_lat = sum(p[1] for p in ring) / len(ring)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mean_lat))
    area = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        area += (x1 * m_per_deg_lon) * (y2 * m_per_deg_lat) - (x2 * m_per_deg_lon) * (
            y1 * m_per_deg_lat
        )
    return abs(area) / 2.0 / 1_000_000.0


class GeoJSONPolygon(BaseModel):
    type: str = "Polygon"
    coordinates: list[list[list[float]]]


class AOICreate(BaseModel):
    name: str
    description: str | None = None
    geometry: GeoJSONPolygon


class AOI(BaseModel):
    id: str
    name: str
    description: str | None
    geometry: dict
    area_km2: float
    bbox: list[float]
    created_at: str


class ImagerySearchRequest(BaseModel):
    aoi_id: str
    start_date: str
    end_date: str
    max_cloud_cover: int = Field(default=20, ge=0, le=100)
    providers: list[str] = Field(default_factory=lambda: ["sentinel-2-l2a", "sentinel-1-grd"])


class ChangeDetectionRequest(BaseModel):
    aoi_id: str
    before_observation_id: str
    after_observation_id: str
    use_sar: bool = True


@api.get("/status")
async def status() -> dict[str, Any]:
    return {
        "status": "ok",
        "demo_mode": not cdse_client.is_configured,
        "cdse_configured": cdse_client.is_configured,
        "limits": {
            "max_aoi_area_km2": MAX_AOI_AREA_KM2,
            "max_date_range_days": MAX_DATE_RANGE_DAYS,
            "default_cloud_threshold": DEFAULT_CLOUD_THRESHOLD,
        },
    }


@api.post("/aois", response_model=AOI)
async def create_aoi(payload: AOICreate) -> AOI:
    area_km2 = polygon_area_km2(payload.geometry.coordinates)
    if area_km2 > MAX_AOI_AREA_KM2:
        raise HTTPException(400, f"AOI area {area_km2:.2f} km² exceeds max {MAX_AOI_AREA_KM2} km²")
    if area_km2 <= 0:
        raise HTTPException(400, "AOI has zero area")
    aoi_id = str(uuid.uuid4())
    doc = {
        "id": aoi_id,
        "name": payload.name,
        "description": payload.description,
        "geometry": payload.geometry.model_dump(),
        "area_km2": area_km2,
        "bbox": bbox_from_polygon(payload.geometry.coordinates),
        "created_at": now_iso(),
    }
    await db.aois.insert_one(doc)
    doc.pop("_id", None)
    return AOI(**doc)


@api.get("/aois", response_model=list[AOI])
async def list_aois() -> list[AOI]:
    docs = await db.aois.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    return [AOI(**d) for d in docs]


@api.get("/aois/{aoi_id}", response_model=AOI)
async def get_aoi(aoi_id: str) -> AOI:
    doc = await db.aois.find_one({"id": aoi_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "AOI not found")
    return AOI(**doc)


@api.delete("/aois/{aoi_id}")
async def delete_aoi(aoi_id: str) -> dict[str, str]:
    r = await db.aois.delete_one({"id": aoi_id})
    if r.deleted_count == 0:
        raise HTTPException(404, "AOI not found")
    await db.observations.delete_many({"aoi_id": aoi_id})
    await db.change_events.delete_many({"aoi_id": aoi_id})
    await db.jobs.delete_many({"aoi_id": aoi_id})
    return {"status": "deleted"}


def _feature_to_observation(feature: dict[str, Any], aoi_id: str) -> dict[str, Any]:
    props = feature.get("properties", {})
    coll = feature.get("collection") or feature.get("collections") or "unknown"
    if isinstance(coll, list):
        coll = coll[0] if coll else "unknown"
    return {
        "observation_id": f"{aoi_id}:{feature['id']}",
        "aoi_id": aoi_id,
        "product_id": feature["id"],
        "collection": coll,
        "platform": props.get("platform"),
        "observation_datetime": props.get("datetime"),
        "cloud_percentage": props.get("eo:cloud_cover"),
        "product_level": props.get("product_level"),
        "bbox": feature.get("bbox"),
        "geometry": feature.get("geometry"),
        "demo": bool(props.get("demo", not cdse_client.is_configured)),
        "selected": False,
        "downloaded": False,
        "processed": False,
        "created_at": now_iso(),
    }


@api.post("/imagery/search")
async def imagery_search(payload: ImagerySearchRequest) -> dict[str, Any]:
    aoi = await db.aois.find_one({"id": payload.aoi_id}, {"_id": 0})
    if not aoi:
        raise HTTPException(404, "AOI not found")
    d0 = datetime.strptime(payload.start_date, "%Y-%m-%d")
    d1 = datetime.strptime(payload.end_date, "%Y-%m-%d")
    if d1 < d0:
        raise HTTPException(400, "end_date must be after start_date")
    if (d1 - d0).days > MAX_DATE_RANGE_DAYS:
        raise HTTPException(400, f"Date range exceeds max of {MAX_DATE_RANGE_DAYS} days")

    bbox = aoi["bbox"]
    all_features: list[dict[str, Any]] = []
    for coll in payload.providers:
        try:
            feats = await cdse_client.search(
                collection=coll, bbox=bbox,
                start_date=payload.start_date, end_date=payload.end_date,
                cloud_cover_max=(payload.max_cloud_cover if coll == "sentinel-2-l2a" else None),
            )
        except Exception as exc:
            logger.exception("CDSE search failed for %s", coll)
            raise HTTPException(502, f"CDSE search failed for {coll}: {exc}")
        for f in feats:
            all_features.append(_feature_to_observation(f, payload.aoi_id))

    for obs in all_features:
        await db.observations.update_one(
            {"observation_id": obs["observation_id"]}, {"$set": obs}, upsert=True
        )
    all_features.sort(key=lambda o: o["observation_datetime"])
    return {
        "aoi_id": payload.aoi_id,
        "count": len(all_features),
        "demo_mode": not cdse_client.is_configured,
        "observations": all_features,
    }


@api.get("/aois/{aoi_id}/observations")
async def list_observations(aoi_id: str) -> list[dict]:
    return await db.observations.find({"aoi_id": aoi_id}, {"_id": 0}).sort("observation_datetime", 1).to_list(2000)


async def _run_change_detection_job(job_id: str, aoi_id: str, before_id: str, after_id: str, use_sar: bool) -> None:
    try:
        aoi = await db.aois.find_one({"id": aoi_id}, {"_id": 0})
        if not aoi:
            raise RuntimeError("AOI not found")
        before = await db.observations.find_one({"observation_id": before_id}, {"_id": 0})
        after = await db.observations.find_one({"observation_id": after_id}, {"_id": 0})
        if not before or not after:
            raise RuntimeError("Observations not found")

        async def progress_cb(stage: str, p: float) -> None:
            await db.jobs.update_one(
                {"job_id": job_id},
                {"$set": {"stage": stage, "progress": p, "status": "RUNNING"}},
            )

        events, diag = await run_change_detection(
            aoi_id, aoi["bbox"], before, after, use_sar, progress_cb=progress_cb
        )
        for ev in events:
            ev["id"] = str(uuid.uuid4())
            await db.change_events.insert_one(dict(ev))

        pu = 0.0
        if cdse_client.is_configured:
            # Rough estimate: 1 PU per fetched scene, x2 for before+after, x2 for S1
            pu = 2.0 + (2.0 if use_sar and before.get("collection") == "sentinel-1-grd" else 0.0)

        await db.jobs.update_one(
            {"job_id": job_id},
            {"$set": {"status": "COMPLETED", "stage": "COMPLETED", "progress": 1.0,
                      "completed_at": now_iso(), "output": {"change_event_count": len(events), "diagnostics": diag},
                      "cdse_processing_units_used": pu}},
        )
    except Exception as exc:
        logger.exception("Change detection job failed")
        await db.jobs.update_one(
            {"job_id": job_id},
            {"$set": {"status": "FAILED", "error_message": str(exc), "completed_at": now_iso()}},
        )


@api.post("/change-detection")
async def submit_change_detection(req: ChangeDetectionRequest, background: BackgroundTasks) -> dict[str, Any]:
    aoi = await db.aois.find_one({"id": req.aoi_id}, {"_id": 0})
    if not aoi:
        raise HTTPException(404, "AOI not found")
    if req.before_observation_id == req.after_observation_id:
        raise HTTPException(400, "before and after must differ")
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id, "aoi_id": req.aoi_id, "job_type": "CHANGE_DETECTION",
        "status": "PENDING", "stage": "QUEUED", "progress": 0.0,
        "input_parameters": req.model_dump(), "cdse_processing_units_used": 0,
        "created_at": now_iso(),
    }
    await db.jobs.insert_one(dict(job))
    background.add_task(_run_change_detection_job, job_id, req.aoi_id,
                        req.before_observation_id, req.after_observation_id, req.use_sar)
    job.pop("_id", None)
    return job


@api.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    doc = await db.jobs.find_one({"job_id": job_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Job not found")
    return doc


@api.get("/aois/{aoi_id}/jobs")
async def aoi_jobs(aoi_id: str) -> list[dict]:
    return await db.jobs.find({"aoi_id": aoi_id}, {"_id": 0}).sort("created_at", -1).to_list(200)


@api.get("/aois/{aoi_id}/changes")
async def list_changes(
    aoi_id: str,
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    min_area_m2: float = Query(0.0, ge=0.0),
    change_type: Optional[str] = None,
) -> dict[str, Any]:
    q: dict[str, Any] = {"aoi_id": aoi_id, "confidence": {"$gte": min_confidence}}
    if min_area_m2 > 0:
        q["area_m2"] = {"$gte": min_area_m2}
    if change_type:
        q["change_type"] = change_type
    docs = await db.change_events.find(q, {"_id": 0}).sort("confidence", -1).to_list(1000)
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": d["geometry"],
             "properties": {k: v for k, v in d.items() if k != "geometry"}}
            for d in docs
        ],
    }


@api.get("/changes/{change_id}")
async def get_change(change_id: str) -> dict:
    doc = await db.change_events.find_one({"id": change_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Change event not found")
    return doc


@api.get("/changes/{change_id}/report", response_class=HTMLResponse)
async def change_report(change_id: str) -> HTMLResponse:
    doc = await db.change_events.find_one({"id": change_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Change event not found")
    aoi = await db.aois.find_one({"id": doc["aoi_id"]}, {"_id": 0}) or {}
    return HTMLResponse(_render_report_html(doc, aoi))


def _render_report_html(ev: dict, aoi: dict) -> str:
    conf_pct = f"{(ev.get('confidence', 0) * 100):.0f}%"
    sensors = " + ".join([s.upper() for s in ev.get("detected_by_sensors", [])])
    area = ev.get("area_m2", 0)
    area_str = (
        f"{area / 1_000_000:.3f} km²" if area >= 100000
        else f"{area / 10_000:.2f} ha" if area >= 5000
        else f"{area:.0f} m²"
    )
    metrics = ev.get("metrics", {})
    metric_rows = "".join(
        f"<tr><td>{k.replace('_', ' ').upper()}</td><td class='mono'>{v}</td></tr>"
        for k, v in metrics.items()
    )
    conf_tier = "high" if ev.get("confidence", 0) >= 0.8 else "med" if ev.get("confidence", 0) >= 0.6 else "low"
    conf_color = {"high": "#FFB020", "med": "#22D3EE", "low": "#94A3B8"}[conf_tier]
    import json as _json
    geom_json = _json.dumps(ev.get("geometry", {}))
    print_btn = "<button onclick='window.print()' class='btn'>Print / Save as PDF</button>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"/>
<title>GEOINT Briefing — {ev.get('change_type', 'Change')} · {ev.get('id', '')[:8]}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Chivo:wght@500;700;900&family=IBM+Plex+Sans:wght@400;500&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
  body {{
    background: #050505; color: #F8FAFC; font-family: 'IBM Plex Sans', sans-serif;
    margin: 0; padding: 40px; max-width: 900px; margin: 0 auto;
  }}
  .head {{ border-bottom: 1px solid rgba(255,255,255,0.12); padding-bottom: 20px; margin-bottom: 24px; display: flex; justify-content: space-between; align-items: end; }}
  .head h1 {{ font-family: 'Chivo', sans-serif; letter-spacing: 0.14em; text-transform: uppercase; margin: 0; font-size: 22px; color: {conf_color}; }}
  .head .sub {{ font-family: 'JetBrains Mono', monospace; color: #94A3B8; font-size: 11px; margin-top: 6px; letter-spacing: 0.12em; }}
  .stamp {{ font-family: 'Chivo', sans-serif; text-transform: uppercase; letter-spacing: 0.2em; font-size: 10px; padding: 4px 10px; border: 1px solid {conf_color}; color: {conf_color}; }}
  .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 24px; }}
  .stat {{ border: 1px solid rgba(255,255,255,0.12); padding: 12px; }}
  .stat .k {{ font-family: 'Chivo', sans-serif; font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: #94A3B8; }}
  .stat .v {{ font-family: 'JetBrains Mono', monospace; font-size: 18px; margin-top: 6px; color: #F8FAFC; }}
  .stat.accent .v {{ color: {conf_color}; }}
  h2 {{ font-family: 'Chivo', sans-serif; letter-spacing: 0.16em; font-size: 12px; text-transform: uppercase; color: #94A3B8; margin: 24px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  table td {{ padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.08); font-size: 12px; }}
  table td:first-child {{ font-family: 'Chivo', sans-serif; text-transform: uppercase; letter-spacing: 0.1em; font-size: 10px; color: #94A3B8; width: 45%; }}
  .mono {{ font-family: 'JetBrains Mono', monospace; }}
  .foot {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.08); color: #475569; font-size: 10px; font-family: 'JetBrains Mono', monospace; }}
  .btn {{ background: transparent; border: 1px solid #22D3EE; color: #22D3EE; padding: 8px 14px; font-family: 'Chivo', sans-serif; text-transform: uppercase; letter-spacing: 0.14em; font-size: 11px; cursor: pointer; }}
  .btn:hover {{ background: #22D3EE; color: #000; }}
  @media print {{ body {{ background: #fff; color: #000; }} .stat, .head, table td {{ border-color: rgba(0,0,0,0.15) !important; }} .btn {{ display: none; }} .stat .v, table td, .head h1, .head .sub, .stamp {{ color: #000 !important; }} }}
</style></head>
<body>
  <div class="head">
    <div>
      <h1>{ev.get('change_type', 'Detected Change')}</h1>
      <div class="sub">ID {ev.get('id', '—')[:12]} · AOI {aoi.get('name', '—')} · {ev.get('last_seen', '')[:10]}</div>
    </div>
    <div class="stamp">CONFIDENCE {conf_pct}</div>
  </div>
  <div class="grid">
    <div class="stat"><div class="k">Change Type</div><div class="v">{ev.get('change_type', '—')}</div></div>
    <div class="stat accent"><div class="k">Area</div><div class="v">{area_str}</div></div>
    <div class="stat"><div class="k">Sensors</div><div class="v">{sensors or '—'}</div></div>
    <div class="stat"><div class="k">Score</div><div class="v">{ev.get('change_score', 0):.3f}</div></div>
    <div class="stat accent"><div class="k">Confidence</div><div class="v">{conf_pct}</div></div>
    <div class="stat"><div class="k">Status</div><div class="v">{ev.get('status', '—').upper()}</div></div>
  </div>

  <h2>Temporal Range</h2>
  <table>
    <tr><td>First Seen</td><td class="mono">{ev.get('first_seen', '—')}</td></tr>
    <tr><td>Last Seen</td><td class="mono">{ev.get('last_seen', '—')}</td></tr>
    <tr><td>Before Imagery</td><td class="mono">{ev.get('before_imagery_id', '—')}</td></tr>
    <tr><td>After Imagery</td><td class="mono">{ev.get('after_imagery_id', '—')}</td></tr>
  </table>

  <h2>Confidence Components</h2>
  <table>{metric_rows}</table>

  <h2>Geometry (GeoJSON)</h2>
  <div class="mono" style="font-size:10px;color:#94A3B8;word-break:break-all;background:#0a0a0a;padding:12px;border:1px solid rgba(255,255,255,0.08);">{geom_json}</div>

  <div class="foot">
    Prepared by GEOINT Change-Detection Platform · {ev.get('created_at', '')[:19]} UTC · This is a heuristic detection, not verified ground truth.
  </div>

  <div style="margin-top: 24px;">{print_btn}</div>
</body></html>"""


@api.get("/aois/{aoi_id}/dashboard")
async def dashboard(aoi_id: str) -> dict[str, Any]:
    aoi = await db.aois.find_one({"id": aoi_id}, {"_id": 0})
    if not aoi:
        raise HTTPException(404, "AOI not found")
    obs_count = await db.observations.count_documents({"aoi_id": aoi_id})
    obs_s2 = await db.observations.count_documents({"aoi_id": aoi_id, "collection": "sentinel-2-l2a"})
    obs_s1 = await db.observations.count_documents({"aoi_id": aoi_id, "collection": "sentinel-1-grd"})
    change_count = await db.change_events.count_documents({"aoi_id": aoi_id})
    high_conf = await db.change_events.count_documents({"aoi_id": aoi_id, "confidence": {"$gte": 0.75}})
    largest = await db.change_events.find({"aoi_id": aoi_id}, {"_id": 0}).sort("area_m2", -1).limit(1).to_list(1)
    latest_obs = await db.observations.find({"aoi_id": aoi_id}, {"_id": 0}).sort("observation_datetime", -1).limit(1).to_list(1)
    return {
        "aoi": aoi,
        "observations": {"total": obs_count, "sentinel2": obs_s2, "sentinel1": obs_s1},
        "changes": {"total": change_count, "high_confidence": high_conf},
        "largest_change": largest[0] if largest else None,
        "latest_observation": latest_obs[0] if latest_obs else None,
    }


@api.get("/aois/{aoi_id}/timeline")
async def timeline(aoi_id: str) -> dict[str, Any]:
    obs = await db.observations.find({"aoi_id": aoi_id}, {"_id": 0}).sort("observation_datetime", 1).to_list(2000)
    changes = await db.change_events.find({"aoi_id": aoi_id}, {"_id": 0}).sort("first_seen", 1).to_list(1000)
    return {"observations": obs, "changes": changes}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_start() -> None:
    logger.info("GEOINT backend starting. DEMO_MODE=%s", not cdse_client.is_configured)
    try:
        await db.aois.create_index([("geometry", "2dsphere")])
        await db.change_events.create_index([("geometry", "2dsphere")])
    except Exception:
        logger.exception("Failed to create geospatial indexes")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    client.close()
