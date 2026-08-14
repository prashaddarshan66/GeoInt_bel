"""CDSE client — OAuth2, STAC search, and Sentinel Hub Process API.

Fetches AOI-clipped multi-band GeoTIFFs directly from CDSE Process API when
credentials are configured. Falls back to deterministic synthetic observations
in DEMO mode (no creds).
"""
from __future__ import annotations

import os
import time
import hashlib
import logging
from typing import Any
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)


S2_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B02","B03","B04","B08","B11","SCL"] }],
    output: { bands: 6, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(s) { return [s.B02, s.B03, s.B04, s.B08, s.B11, s.SCL]; }
"""

S1_EVALSCRIPT = """//VERSION=3
function setup() {
  return { input: ["VV","VH"], output: { bands: 2, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) { return [s.VV, s.VH]; }
"""


class CDSEClient:
    def __init__(self) -> None:
        self.client_id = os.environ.get("CDSE_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("CDSE_CLIENT_SECRET", "").strip()
        self.token_url = os.environ.get(
            "CDSE_TOKEN_URL",
            "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
        )
        self.stac_url = os.environ.get(
            "CDSE_STAC_URL", "https://sh.dataspace.copernicus.eu/catalog/v1/search"
        )
        self.process_url = os.environ.get(
            "CDSE_PROCESS_URL", "https://sh.dataspace.copernicus.eu/process/v1"
        )
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        if not self.is_configured:
            raise RuntimeError("CDSE credentials not configured")
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            r.raise_for_status()
            payload = r.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + int(payload.get("expires_in", 600))
        return self._token

    # -------------------------- Catalog search -----------------------------
    async def search(
        self,
        collection: str,
        bbox: list[float],
        start_date: str,
        end_date: str,
        cloud_cover_max: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not self.is_configured:
            return self._demo_search(collection, bbox, start_date, end_date, cloud_cover_max)

        token = await self._get_token()
        body: dict[str, Any] = {
            "collections": [collection],
            "bbox": bbox,
            "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
            "limit": limit,
        }
        # Note: Sentinel Hub catalog does not accept the STAC 'query' extension —
        # we filter cloud cover client-side below.
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                self.stac_url, json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code >= 400:
                logger.error("CDSE STAC %s failed %s: %s", self.stac_url, r.status_code, r.text[:500])
                r.raise_for_status()
            data = r.json()
        feats = data.get("features", [])
        if collection == "sentinel-2-l2a" and cloud_cover_max is not None:
            feats = [
                f for f in feats
                if (f.get("properties", {}).get("eo:cloud_cover") or 0) <= cloud_cover_max
            ]
        return feats

    # -------------------------- Process API --------------------------------
    async def fetch_geotiff(
        self,
        collection: str,
        bbox: list[float],
        acquisition_datetime: str,
        window_hours: int = 12,
        max_pixels: int = 768,
    ) -> bytes:
        """Fetch an AOI-clipped multi-band GeoTIFF via Sentinel Hub Process API.

        collection: 'sentinel-2-l2a' or 'sentinel-1-grd'
        acquisition_datetime: ISO string; we search a small window around it.
        Returns raw TIFF bytes.
        """
        if not self.is_configured:
            raise RuntimeError("CDSE credentials not configured — cannot fetch imagery")

        token = await self._get_token()
        try:
            dt = datetime.fromisoformat(acquisition_datetime.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.strptime(acquisition_datetime[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        t_from = (dt - timedelta(hours=window_hours)).isoformat().replace("+00:00", "Z")
        t_to = (dt + timedelta(hours=window_hours)).isoformat().replace("+00:00", "Z")

        # Compute width/height keeping aspect ratio, clamped to max_pixels
        min_lon, min_lat, max_lon, max_lat = bbox
        dlon = max_lon - min_lon
        dlat = max_lat - min_lat
        # At mid-lat, 1 deg lon ~ cos(lat)*111km. We want ~10 m/px for S2.
        import math
        mid_lat = (min_lat + max_lat) / 2
        m_per_deg_lon = 111_320 * math.cos(math.radians(mid_lat))
        m_per_deg_lat = 111_320
        target_res_m = 10.0 if collection == "sentinel-2-l2a" else 20.0
        w = int(dlon * m_per_deg_lon / target_res_m)
        h = int(dlat * m_per_deg_lat / target_res_m)
        # cap
        scale = max(1.0, max(w, h) / max_pixels)
        w = max(64, int(w / scale))
        h = max(64, int(h / scale))

        is_s1 = collection == "sentinel-1-grd"
        data_item: dict[str, Any] = {
            "type": "sentinel-1-grd" if is_s1 else "sentinel-2-l2a",
            "dataFilter": {"timeRange": {"from": t_from, "to": t_to}},
        }
        if is_s1:
            data_item["dataFilter"].update(
                {"acquisitionMode": "IW", "polarization": "DV"}
            )
            data_item["processing"] = {
                "orthorectify": True,
                "backCoeff": "GAMMA0_TERRAIN",
                "demInstance": "COPERNICUS_30",
            }
        else:
            data_item["dataFilter"]["mosaickingOrder"] = "leastCC"

        body = {
            "input": {
                "bounds": {
                    "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
                    "bbox": bbox,
                },
                "data": [data_item],
            },
            "output": {
                "width": w,
                "height": h,
                "responses": [
                    {"identifier": "default", "format": {"type": "image/tiff"}}
                ],
            },
            "evalscript": S1_EVALSCRIPT if is_s1 else S2_EVALSCRIPT,
        }

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                self.process_url,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "image/tiff",
                },
            )
        if r.status_code >= 400:
            snippet = r.text[:400]
            raise RuntimeError(f"CDSE Process API {r.status_code}: {snippet}")
        return r.content

    # ------------------------------ DEMO MODE ------------------------------
    def _demo_search(
        self,
        collection: str,
        bbox: list[float],
        start_date: str,
        end_date: str,
        cloud_cover_max: float | None,
    ) -> list[dict[str, Any]]:
        cadence_days = 5 if collection == "sentinel-2-l2a" else 6
        d0 = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        d1 = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        features: list[dict[str, Any]] = []
        current = d0
        idx = 0
        seed = hashlib.md5(f"{collection}|{bbox}".encode()).hexdigest()
        while current <= d1:
            h = int(hashlib.md5(f"{seed}|{current.isoformat()}".encode()).hexdigest()[:6], 16)
            cloud = (h % 90) if collection == "sentinel-2-l2a" else 0
            if cloud_cover_max is None or cloud <= cloud_cover_max or collection != "sentinel-2-l2a":
                prod_prefix = "S2B_MSIL2A" if collection == "sentinel-2-l2a" else "S1A_IW_GRDH"
                product_id = (
                    f"{prod_prefix}_{current.strftime('%Y%m%dT%H%M%S')}_DEMO_"
                    f"{hashlib.md5(f'{seed}{idx}'.encode()).hexdigest()[:8].upper()}"
                )
                features.append({
                    "id": product_id, "type": "Feature", "collection": collection, "bbox": bbox,
                    "geometry": {"type": "Polygon", "coordinates": [[
                        [bbox[0], bbox[1]], [bbox[2], bbox[1]],
                        [bbox[2], bbox[3]], [bbox[0], bbox[3]], [bbox[0], bbox[1]]]]},
                    "properties": {
                        "datetime": current.isoformat().replace("+00:00", "Z"),
                        "eo:cloud_cover": cloud if collection == "sentinel-2-l2a" else None,
                        "platform": "sentinel-2b" if collection == "sentinel-2-l2a" else "sentinel-1a",
                        "instruments": ["msi"] if collection == "sentinel-2-l2a" else ["c-sar"],
                        "sar:polarizations": None if collection == "sentinel-2-l2a" else ["VV", "VH"],
                        "product_level": "L2A" if collection == "sentinel-2-l2a" else "GRD",
                        "demo": True,
                    },
                })
            current += timedelta(days=cadence_days)
            idx += 1
        return features


cdse_client = CDSEClient()
