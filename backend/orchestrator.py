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
