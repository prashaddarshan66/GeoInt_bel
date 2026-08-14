"""Orchestrator: fetch imagery via CDSE Process API, run real change detection,
fall back to synthetic detection if fetching/processing fails (e.g. in DEMO).
"""
from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any

from cdse import cdse_client
from pipelines import detect_optical_changes, detect_sar_changes, fuse_and_finalize, raw_dir
from change_detection import detect_changes as detect_changes_synth

logger = logging.getLogger(__name__)


async def _fetch_and_cache(aoi_id: str, obs: dict, bbox: list[float]) -> Path | None:
    """Return path to cached GeoTIFF or None if fetch fails."""
    d = raw_dir(aoi_id, obs["observation_id"])
    tif_path = d / f"{obs['collection']}.tif"
    if tif_path.exists() and tif_path.stat().st_size > 1000:
        return tif_path
    try:
        data = await cdse_client.fetch_geotiff(
            collection=obs["collection"],
            bbox=bbox,
            acquisition_datetime=obs["observation_datetime"],
        )
        tif_path.write_bytes(data)
        return tif_path
    except Exception:
        logger.exception("Process API fetch failed for %s", obs.get("product_id"))
        return None


async def run_change_detection(
    aoi_id: str,
    aoi_bbox: list[float],
    before_obs: dict,
    after_obs: dict,
    use_sar: bool,
    progress_cb=None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (events_list, diagnostics_dict)."""
    async def _prog(stage: str, p: float):
        if progress_cb:
            await progress_cb(stage, p)

    diag: dict[str, Any] = {"mode": "real" if cdse_client.is_configured else "demo"}

    if not cdse_client.is_configured:
        # DEMO fallback — use existing synthetic engine
        await _prog("SEARCHING", 0.1)
        await _prog("DOWNLOADING", 0.3)
        await _prog("PREPROCESSING", 0.5)
        await _prog("DETECTING", 0.75)
        sensors = ["optical"] + (["sar"] if use_sar else [])
        events = detect_changes_synth(aoi_id, aoi_bbox, before_obs, after_obs, sensors)
        await _prog("POLYGONS", 0.98)
        return events, diag

    # Real pipeline
    await _prog("SEARCHING", 0.05)
    # Optical fetch (only if before/after are S2)
    optical_result: dict[str, Any] | None = None
    sar_result: dict[str, Any] | None = None

    is_s2_pair = (
        before_obs["collection"] == "sentinel-2-l2a"
        and after_obs["collection"] == "sentinel-2-l2a"
    )
    is_s1_pair = (
        before_obs["collection"] == "sentinel-1-grd"
        and after_obs["collection"] == "sentinel-1-grd"
    )

    if is_s2_pair:
        await _prog("DOWNLOADING", 0.2)
        bpath = await _fetch_and_cache(aoi_id, before_obs, aoi_bbox)
        apath = await _fetch_and_cache(aoi_id, after_obs, aoi_bbox)
        if bpath and apath:
            await _prog("PREPROCESSING", 0.45)
            try:
                optical_result = detect_optical_changes(bpath, apath)
                diag["optical_polygons"] = len(optical_result.get("polygons", []))
                diag["optical_threshold"] = optical_result.get("threshold")
                diag["optical_valid_fraction"] = optical_result.get("valid_fraction")
            except Exception as exc:
                logger.exception("Optical pipeline failed")
                diag["optical_error"] = str(exc)

        # Ancillary SAR (find same-day-ish S1 observation IF requested)
        # For MVP: only run SAR if the analyst explicitly picked S1 pair.

    if is_s1_pair and use_sar:
        await _prog("DOWNLOADING", 0.5)
        bpath = await _fetch_and_cache(aoi_id, before_obs, aoi_bbox)
        apath = await _fetch_and_cache(aoi_id, after_obs, aoi_bbox)
        if bpath and apath:
            await _prog("PREPROCESSING", 0.7)
            try:
                sar_result = detect_sar_changes(bpath, apath)
                diag["sar_polygons"] = len(sar_result.get("polygons", []))
                diag["sar_threshold"] = sar_result.get("threshold")
            except Exception as exc:
                logger.exception("SAR pipeline failed")
                diag["sar_error"] = str(exc)

    await _prog("DETECTING", 0.85)
    events = fuse_and_finalize(aoi_id, before_obs, after_obs, optical_result, sar_result)
    await _prog("POLYGONS", 0.98)

    if not events and (optical_result is None and sar_result is None):
        # Nothing worked — surface as failure via caller
        diag["error"] = "No imagery could be retrieved; check CDSE quota and acquisition dates"
    return events, diag


# ============================ TIME-SERIES ==================================
async def run_timeseries_detection(
    aoi_id: str,
    aoi_bbox: list[float],
    observations: list[dict],
    use_sar: bool,
    progress_cb=None,
) -> tuple[list[dict], dict]:
    """Run change detection on every consecutive pair of the sorted observation
    series, then aggregate polygons across pairs by IoU matching so `persistence`
    reflects real recurrence (0..1 = fraction of pairs where the polygon was
    seen). Also update first_seen / last_seen and confidence accordingly.
    """
    from pipelines import (
        detect_optical_changes, detect_sar_changes,
        polygon_iou as _polygon_iou,
        polygon_area_m2 as _polygon_area_m2,
        classify as _classify,
        confidence as _confidence,
    )
    from datetime import datetime, timezone

    # Filter to a single provider stream for clean pairwise iteration.
    # Dedupe by acquisition datetime (keep lowest cloud % for S2) so
    # tile-neighbour pairs from the same overpass don't dominate the series.
    def _dedupe(obs_list: list[dict], key: str) -> list[dict]:
        by_date: dict[str, dict] = {}
        for o in obs_list:
            k = (o.get("observation_datetime") or "")[:10]  # date only
            cur = by_date.get(k)
            if not cur:
                by_date[k] = o
                continue
            # prefer lower cloud cover; None treated as high
            cc_new = o.get("cloud_percentage")
            cc_old = cur.get("cloud_percentage")
            cn = 999 if cc_new is None else cc_new
            co = 999 if cc_old is None else cc_old
            if cn < co:
                by_date[k] = o
        return sorted(by_date.values(), key=lambda x: x.get("observation_datetime") or "")

    s2 = _dedupe([o for o in observations if o["collection"] == "sentinel-2-l2a"], "s2")
    s1 = _dedupe([o for o in observations if o["collection"] == "sentinel-1-grd"], "s1")

    diag: dict = {"mode": "real" if cdse_client.is_configured else "demo",
                  "s2_pairs": max(0, len(s2) - 1),
                  "s1_pairs": max(0, len(s1) - 1)}

    if not cdse_client.is_configured:
        # Fallback: synthesize using existing single-pair engine on first/last only
        from change_detection import detect_changes as _synth
        sensors = ["optical"] + (["sar"] if use_sar else [])
        if len(s2) >= 2:
            evts = _synth(aoi_id, aoi_bbox, s2[0], s2[-1], sensors)
        elif len(s1) >= 2:
            evts = _synth(aoi_id, aoi_bbox, s1[0], s1[-1], sensors)
        else:
            evts = []
        return evts, diag

    # Cap pairs to protect CDSE quota
    MAX_PAIRS = 4
    s2 = s2[: MAX_PAIRS + 1]
    s1 = s1[: MAX_PAIRS + 1] if use_sar else []

    async def _prog(stage: str, p: float) -> None:
        if progress_cb:
            await progress_cb(stage, p)

    aggregated: list[dict] = []  # list of tracks
    # Track schema:
    #   { 'geometry': dict, 'observed_in': set[int], 'first_seen': iso,
    #     'last_seen': iso, 'best_magnitude_z': float, 'valid_pixel_frac_last': float,
    #     'sensors': set[str], 'before_last': product_id, 'after_last': product_id }

    total_pairs = max(0, len(s2) - 1) + (max(0, len(s1) - 1) if use_sar else 0)
    if total_pairs == 0:
        diag["error"] = "Not enough consecutive observations for timeseries"
        return [], diag
    done = 0

    async def _fetch_pair(bef, aft):
        await _prog("DOWNLOADING", 0.05 + 0.5 * (done / max(total_pairs, 1)))
        bpath = await _fetch_and_cache(aoi_id, bef, aoi_bbox)
        apath = await _fetch_and_cache(aoi_id, aft, aoi_bbox)
        return bpath, apath

    def _merge_polys(polys: list[dict], sensor: str, pair_idx: int, bef: dict, aft: dict) -> None:
        for op in polys:
            best_iou, best_i = 0.0, -1
            for i, tr in enumerate(aggregated):
                iou = _polygon_iou(op, tr)
                if iou > best_iou:
                    best_iou, best_i = iou, i
            if best_iou > 0.3 and best_i >= 0:
                tr = aggregated[best_i]
                tr["observed_in"].add(pair_idx)
                tr["last_seen"] = aft["observation_datetime"]
                tr["best_magnitude_z"] = max(tr["best_magnitude_z"], float(op["magnitude_z"]))
                tr["valid_pixel_frac_last"] = float(op["valid_pixel_fraction"])
                tr["sensors"].add(sensor)
                tr["after_last"] = aft["product_id"]
                tr["change_magnitude_mean"] = max(tr["change_magnitude_mean"], float(op.get("change_magnitude_mean", 0.0)))
            else:
                aggregated.append({
                    "geometry": op["geometry"],
                    "observed_in": {pair_idx},
                    "first_seen": bef["observation_datetime"],
                    "last_seen": aft["observation_datetime"],
                    "best_magnitude_z": float(op["magnitude_z"]),
                    "valid_pixel_frac_last": float(op["valid_pixel_fraction"]),
                    "sensors": {sensor},
                    "before_first": bef["product_id"],
                    "after_last": aft["product_id"],
                    "change_magnitude_mean": float(op.get("change_magnitude_mean", 0.0)),
                })

    pair_idx = 0
    # ---- Optical pairs ----
    for i in range(len(s2) - 1):
        bef, aft = s2[i], s2[i + 1]
        try:
            bpath, apath = await _fetch_pair(bef, aft)
            if not bpath or not apath:
                pair_idx += 1
                done += 1
                continue
            await _prog("PREPROCESSING", 0.55 + 0.35 * (done / max(total_pairs, 1)))
            res = detect_optical_changes(bpath, apath)
            _merge_polys(res.get("polygons", []), "optical", pair_idx, bef, aft)
            diag.setdefault("pair_details", []).append(
                {"idx": pair_idx, "sensor": "optical",
                 "polygons": len(res.get("polygons", [])),
                 "threshold": res.get("threshold"),
                 "before": bef["product_id"], "after": aft["product_id"]}
            )
        except Exception as exc:
            diag.setdefault("errors", []).append(f"optical pair {pair_idx}: {exc}")
        pair_idx += 1
        done += 1

    # ---- SAR pairs ----
    if use_sar:
        for i in range(len(s1) - 1):
            bef, aft = s1[i], s1[i + 1]
            try:
                bpath, apath = await _fetch_pair(bef, aft)
                if not bpath or not apath:
                    pair_idx += 1
                    done += 1
                    continue
                await _prog("PREPROCESSING", 0.55 + 0.35 * (done / max(total_pairs, 1)))
                res = detect_sar_changes(bpath, apath)
                _merge_polys(res.get("polygons", []), "sar", pair_idx, bef, aft)
                diag.setdefault("pair_details", []).append(
                    {"idx": pair_idx, "sensor": "sar",
                     "polygons": len(res.get("polygons", [])),
                     "threshold": res.get("threshold"),
                     "before": bef["product_id"], "after": aft["product_id"]}
                )
            except Exception as exc:
                diag.setdefault("errors", []).append(f"sar pair {pair_idx}: {exc}")
            pair_idx += 1
            done += 1

    await _prog("DETECTING", 0.94)
    # Finalize
    now = datetime.now(timezone.utc).isoformat()
    events: list[dict] = []
    for tr in aggregated:
        n_obs = len(tr["observed_in"])
        persistence = round(n_obs / max(total_pairs, 1), 3)
        multi = len(tr["sensors"]) >= 2
        area_m2 = _polygon_area_m2(tr["geometry"])
        conf = _confidence(tr["best_magnitude_z"], tr["valid_pixel_frac_last"], persistence, multi)
        ev = {
            "geometry": tr["geometry"],
            "change_type": _classify(tr["change_magnitude_mean"], multi, area_m2),
            "change_score": round(min(1.0, tr["change_magnitude_mean"] / 6.0), 3),
            "confidence": conf,
            "area_m2": round(area_m2, 2),
            "detected_by_sensors": sorted(tr["sensors"]),
            "aoi_id": aoi_id,
            "before_imagery_id": tr.get("before_first"),
            "after_imagery_id": tr.get("after_last"),
            "first_seen": tr["first_seen"],
            "last_seen": tr["last_seen"],
            "status": "new",
            "description": f"Time-series detection · seen in {n_obs}/{total_pairs} pairs",
            "created_at": now,
            "metrics": {
                "magnitude_z": round(tr["best_magnitude_z"], 3),
                "valid_pixel_fraction": round(tr["valid_pixel_frac_last"], 3),
                "persistence": persistence,
                "multi_sensor": multi,
                "observed_in_pairs": n_obs,
                "total_pairs": total_pairs,
            },
        }
        events.append(ev)

    events.sort(key=lambda e: (e["metrics"]["persistence"], e["confidence"]), reverse=True)
    diag["total_pairs"] = total_pairs
    diag["tracks"] = len(events)
    await _prog("POLYGONS", 0.99)
    return events, diag
