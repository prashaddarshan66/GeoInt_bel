"""Real change-detection pipelines.

Optical (Sentinel-2 L2A):
  1. Read AOI-clipped GeoTIFF (6 bands: B02, B03, B04, B08, B11, SCL).
  2. Mask out cloud / shadow / snow / no-data via SCL classes.
  3. Compute NDVI = (B08-B04)/(B08+B04) and NDBI = (B11-B08)/(B11+B08).
  4. Change Vector Analysis magnitude across [NDVI, NDBI, red, nir] between dates.
  5. Otsu threshold on the change-magnitude distribution.
  6. Morphological opening/closing → connected components → polygons.

SAR (Sentinel-1 GRD, VV/VH terrain-corrected):
  1. Read AOI-clipped GeoTIFF (2 bands: VV, VH).
  2. Convert to dB, apply speckle filter (median 5x5, a fast Lee-family filter).
  3. log-ratio = 10*log10(after/before) — robust to calibration drift.
  4. Otsu on |log-ratio|.

Multi-sensor fusion: polygons detected by both optical and SAR (spatial overlap
via IoU > 0.15) get a confidence boost.

Confidence formula per polygon:
    conf = 0.40 * magnitude_z_norm       # normalised change magnitude
         + 0.25 * valid_pixel_fraction   # SCL-valid pixels inside polygon
         + 0.20 * persistence            # placeholder; 0.8 default for single pair
         + 0.15 * multi_sensor           # 1.0 if optical & SAR agree
"""
from __future__ import annotations

import io
import os
import math
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio import features as rio_features
from rasterio.transform import Affine
from skimage.filters import threshold_otsu
from skimage.morphology import binary_opening, binary_closing, disk, remove_small_objects
from skimage.measure import label

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))

# SCL classes to keep (per Sentinel-2 L2A spec):
#   4 = vegetation, 5 = bare soil, 6 = water, 7 = unclassified, 11 = snow (borderline)
# Excluded: 0 no-data, 1 saturated, 2 dark, 3 shadow, 8 cloud med, 9 cloud high,
# 10 thin cirrus
SCL_VALID = {4, 5, 6, 7}


def raw_dir(aoi_id: str, observation_id: str) -> Path:
    p = DATA_DIR / "raw" / aoi_id / observation_id.replace(":", "_").replace("/", "_")
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_tiff(path: Path) -> tuple[np.ndarray, Any, Any]:
    with rasterio.open(path) as ds:
        arr = ds.read().astype(np.float32)
        transform = ds.transform
        crs = ds.crs
    return arr, transform, crs


def _safe_div(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    out = np.zeros_like(num, dtype=np.float32)
    mask = np.abs(den) > 1e-6
    out[mask] = num[mask] / den[mask]
    return out


# ============================ OPTICAL PIPELINE ==============================
def _s2_valid_mask(scl: np.ndarray) -> np.ndarray:
    return np.isin(scl.astype(np.int32), list(SCL_VALID))


def _polygons_from_binary(
    binary: np.ndarray,
    transform: Affine,
    change_map: np.ndarray,
    valid_mask: np.ndarray,
    min_pixels: int = 60,
) -> list[dict[str, Any]]:
    """Convert a binary change mask to polygon dicts with per-polygon stats."""
    # Cleanup
    binary = binary_opening(binary, disk(1))
    binary = binary_closing(binary, disk(2))
    binary = remove_small_objects(binary, min_size=min_pixels)

    labels = label(binary, connectivity=2)
    n_labels = int(labels.max())
    polys: list[dict[str, Any]] = []
    for lid in range(1, n_labels + 1):
        comp_mask = labels == lid
        # Skip tiny components
        n_pix = int(comp_mask.sum())
        if n_pix < min_pixels:
            continue
        # rasterio shapes on the specific component
        comp_uint = comp_mask.astype(np.uint8)
        shapes = list(rio_features.shapes(comp_uint, mask=comp_mask, transform=transform))
        if not shapes:
            continue
        # Take the largest polygon shape for this component
        geom, _val = max(shapes, key=lambda s: len(s[0].get("coordinates", [[]])[0]))
        magnitude_mean = float(change_map[comp_mask].mean())
        magnitude_z = float(
            (change_map[comp_mask].mean() - change_map[valid_mask].mean())
            / (change_map[valid_mask].std() + 1e-6)
        )
        vpx = float(valid_mask[comp_mask].mean())
        polys.append(
            {
                "geometry": geom,
                "pixel_count": n_pix,
                "change_magnitude_mean": magnitude_mean,
                "magnitude_z": magnitude_z,
                "valid_pixel_fraction": vpx,
            }
        )
    return polys


def detect_optical_changes(
    before_tif: bytes | Path,
    after_tif: bytes | Path,
) -> dict[str, Any]:
    """Return {'polygons': [...], 'change_map': ndarray, 'transform': Affine}."""
    def _load(src: bytes | Path):
        if isinstance(src, (bytes, bytearray)):
            with MemoryFile(bytes(src)) as mf, mf.open() as ds:
                return ds.read().astype(np.float32), ds.transform, ds.crs
        return read_tiff(src)

    a, ta, _ = _load(before_tif)
    b, tb, _ = _load(after_tif)

    # Align shape if slight mismatch
    min_h = min(a.shape[1], b.shape[1])
    min_w = min(a.shape[2], b.shape[2])
    a = a[:, :min_h, :min_w]
    b = b[:, :min_h, :min_w]
    transform = ta

    # Bands: 0=B02, 1=B03, 2=B04, 3=B08, 4=B11, 5=SCL
    b04_a, b08_a, b11_a, scl_a = a[2], a[3], a[4], a[5]
    b04_b, b08_b, b11_b, scl_b = b[2], b[3], b[4], b[5]

    ndvi_a = _safe_div(b08_a - b04_a, b08_a + b04_a)
    ndvi_b = _safe_div(b08_b - b04_b, b08_b + b04_b)
    ndbi_a = _safe_div(b11_a - b08_a, b11_a + b08_a)
    ndbi_b = _safe_div(b11_b - b08_b, b11_b + b08_b)

    valid = _s2_valid_mask(scl_a) & _s2_valid_mask(scl_b)
    if valid.sum() < 100:
        return {"polygons": [], "transform": transform, "note": "insufficient valid pixels"}

    # CVA magnitude across (NDVI, NDBI, red, nir), with radiometric normalization
    # via z-scoring each layer over valid pixels.
    def _z(x: np.ndarray) -> np.ndarray:
        m = float(x[valid].mean())
        s = float(x[valid].std() + 1e-6)
        return (x - m) / s

    da = np.stack([_z(ndvi_a), _z(ndbi_a), _z(b04_a), _z(b08_a)])
    db = np.stack([_z(ndvi_b), _z(ndbi_b), _z(b04_b), _z(b08_b)])
    diff = db - da
    change_map = np.sqrt((diff ** 2).sum(axis=0))
    change_map = np.where(valid, change_map, 0.0)

    # Otsu threshold on the change magnitude, restricted to valid pixels
    try:
        thresh = float(threshold_otsu(change_map[valid]))
    except Exception:
        thresh = float(change_map[valid].mean() + change_map[valid].std())
    thresh = max(thresh, 1.2)  # avoid over-triggering on flat scenes

    binary = (change_map > thresh) & valid
    polys = _polygons_from_binary(binary, transform, change_map, valid)
    return {
        "polygons": polys,
        "transform": transform,
        "threshold": thresh,
        "valid_fraction": float(valid.mean()),
    }


# ============================ SAR PIPELINE =================================
def _speckle_filter(x: np.ndarray, size: int = 5) -> np.ndarray:
    """Fast median filter (a member of the Lee-family behaviour) for speckle."""
    from scipy.ndimage import median_filter
    return median_filter(x, size=size).astype(np.float32)


def _to_db(power: np.ndarray) -> np.ndarray:
    return 10.0 * np.log10(np.clip(power, 1e-6, None)).astype(np.float32)


def detect_sar_changes(
    before_tif: bytes | Path,
    after_tif: bytes | Path,
) -> dict[str, Any]:
    def _load(src: bytes | Path):
        if isinstance(src, (bytes, bytearray)):
            with MemoryFile(bytes(src)) as mf, mf.open() as ds:
                return ds.read().astype(np.float32), ds.transform
        with rasterio.open(src) as ds:
            return ds.read().astype(np.float32), ds.transform

    a, ta = _load(before_tif)
    b, tb = _load(after_tif)
    min_h = min(a.shape[1], b.shape[1])
    min_w = min(a.shape[2], b.shape[2])
    a = a[:, :min_h, :min_w]
    b = b[:, :min_h, :min_w]

    # Bands: 0=VV, 1=VH (as gamma0 backscatter, linear power)
    vv_a, vh_a = _speckle_filter(a[0]), _speckle_filter(a[1])
    vv_b, vh_b = _speckle_filter(b[0]), _speckle_filter(b[1])

    valid = (vv_a > 1e-5) & (vv_b > 1e-5) & (vh_a > 1e-5) & (vh_b > 1e-5)
    if valid.sum() < 100:
        return {"polygons": [], "transform": ta, "note": "insufficient valid SAR pixels"}

    # log-ratio (dB): positive == increase (e.g. bare concrete/metal)
    lr_vv = _to_db(vv_b) - _to_db(vv_a)
    lr_vh = _to_db(vh_b) - _to_db(vh_a)
    change_map = np.sqrt(lr_vv ** 2 + lr_vh ** 2)
    change_map = np.where(valid, change_map, 0.0)

    try:
        thresh = float(threshold_otsu(change_map[valid]))
    except Exception:
        thresh = float(change_map[valid].mean() + change_map[valid].std())
    thresh = max(thresh, 2.0)  # dB units

    binary = (change_map > thresh) & valid
    polys = _polygons_from_binary(binary, ta, change_map, valid, min_pixels=40)
    return {
        "polygons": polys,
        "transform": ta,
        "threshold": thresh,
        "valid_fraction": float(valid.mean()),
    }


# ============================ FUSION & CLASSIFY =============================
def polygon_iou(a: dict, b: dict) -> float:
    """Approximate IoU via shapely if available; else 0."""
    try:
        from shapely.geometry import shape
        ga = shape(a["geometry"])
        gb = shape(b["geometry"])
        if not ga.is_valid or not gb.is_valid:
            return 0.0
        inter = ga.intersection(gb).area
        union = ga.union(gb).area
        return float(inter / union) if union > 0 else 0.0
    except Exception:
        return 0.0


def polygon_area_m2(geom: dict) -> float:
    try:
        from shapely.geometry import shape
        g = shape(geom)
        ring = list(g.exterior.coords)
    except Exception:
        ring = geom["coordinates"][0]
    if len(ring) < 4:
        return 0.0
    mean_lat = sum(p[1] for p in ring) / len(ring)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mean_lat))
    area = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        area += (x1 * m_per_deg_lon) * (y2 * m_per_deg_lat) - (x2 * m_per_deg_lon) * (y1 * m_per_deg_lat)
    return abs(area) / 2.0


def classify(magnitude: float, sar_agree: bool, area_m2: float) -> str:
    if sar_agree and area_m2 > 2000 and magnitude > 3.0:
        return "Construction"
    if sar_agree and magnitude > 3.0:
        return "Building"
    if magnitude > 3.0 and area_m2 > 5000:
        return "Land Clearing"
    if magnitude > 2.0 and area_m2 > 1500:
        return "Infrastructure"
    if magnitude > 1.5:
        return "Vegetation"
    return "Other"


def confidence(
    magnitude_z: float,
    valid_pixel_frac: float,
    persistence: float,
    multi_sensor: bool,
) -> float:
    mag_norm = min(1.0, max(0.0, magnitude_z / 4.0))
    conf = (
        0.40 * mag_norm
        + 0.25 * float(valid_pixel_frac)
        + 0.20 * float(persistence)
        + 0.15 * (1.0 if multi_sensor else 0.0)
    )
    return round(min(0.99, max(0.05, conf)), 3)


# Backwards-compat aliases (internal helpers used in fuse_and_finalize below)
_polygon_iou = polygon_iou
_polygon_area_m2 = polygon_area_m2
_classify = classify
_confidence = confidence


def fuse_and_finalize(
    aoi_id: str,
    before_obs: dict,
    after_obs: dict,
    optical_result: dict | None,
    sar_result: dict | None,
) -> list[dict[str, Any]]:
    """Combine optical + SAR polygons into final change_event docs."""
    opt_polys = (optical_result or {}).get("polygons", [])
    sar_polys = (sar_result or {}).get("polygons", [])

    used_sar = set()
    events: list[dict[str, Any]] = []
    persistence = 0.75  # single-pair default; will be replaced by real multi-date

    for op in opt_polys:
        matched_sar = None
        best_iou = 0.0
        for i, sp in enumerate(sar_polys):
            if i in used_sar:
                continue
            iou = _polygon_iou(op, sp)
            if iou > best_iou:
                best_iou = iou
                matched_sar = i
        multi = best_iou > 0.15 and matched_sar is not None
        if multi and matched_sar is not None:
            used_sar.add(matched_sar)

        area_m2 = _polygon_area_m2(op["geometry"])
        mag = float(op.get("change_magnitude_mean", 0.0))
        events.append({
            "geometry": op["geometry"],
            "change_type": _classify(mag, multi, area_m2),
            "change_score": round(min(1.0, mag / 6.0), 3),
            "confidence": _confidence(op["magnitude_z"], op["valid_pixel_fraction"], persistence, multi),
            "area_m2": round(area_m2, 2),
            "detected_by_sensors": ["optical", "sar"] if multi else ["optical"],
            "metrics": {
                "magnitude_z": round(float(op["magnitude_z"]), 3),
                "valid_pixel_fraction": round(float(op["valid_pixel_fraction"]), 3),
                "persistence": persistence,
                "multi_sensor": multi,
                "sar_iou": round(best_iou, 3) if multi else 0.0,
            },
        })

    # SAR-only polygons (no matching optical) at lower confidence
    for i, sp in enumerate(sar_polys):
        if i in used_sar:
            continue
        area_m2 = _polygon_area_m2(sp["geometry"])
        mag = float(sp.get("change_magnitude_mean", 0.0))
        events.append({
            "geometry": sp["geometry"],
            "change_type": _classify(mag, False, area_m2),
            "change_score": round(min(1.0, mag / 8.0), 3),
            "confidence": _confidence(sp["magnitude_z"], sp["valid_pixel_fraction"], persistence, False),
            "area_m2": round(area_m2, 2),
            "detected_by_sensors": ["sar"],
            "metrics": {
                "magnitude_z": round(float(sp["magnitude_z"]), 3),
                "valid_pixel_fraction": round(float(sp["valid_pixel_fraction"]), 3),
                "persistence": persistence,
                "multi_sensor": False,
            },
        })

    # Sort by confidence descending
    events.sort(key=lambda e: e["confidence"], reverse=True)

    # Stamp metadata common fields
    now = datetime.now(timezone.utc).isoformat()
    for ev in events:
        ev.update({
            "aoi_id": aoi_id,
            "before_imagery_id": before_obs["product_id"],
            "after_imagery_id": after_obs["product_id"],
            "first_seen": before_obs["observation_datetime"],
            "last_seen": after_obs["observation_datetime"],
            "status": "new",
            "description": f"Detected between {before_obs['product_id'][:20]}… and {after_obs['product_id'][:20]}…",
            "created_at": now,
        })
    return events
