"""Rendering helpers: turn cached GeoTIFFs into PNGs for map preview and
change-event before/after/difference crops.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
from rasterio.windows import from_bounds
from PIL import Image

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))


def obs_tif(aoi_id: str, obs: dict) -> Path:
    from pipelines import raw_dir
    return raw_dir(aoi_id, obs["observation_id"]) / f"{obs['collection']}.tif"


def _stretch(a: np.ndarray, lo_p: float = 2.0, hi_p: float = 98.0) -> np.ndarray:
    """Percentile stretch → uint8."""
    a = np.asarray(a, dtype=np.float32)
    valid = a[np.isfinite(a) & (a > 0)]
    if valid.size < 50:
        return np.zeros(a.shape, dtype=np.uint8)
    lo, hi = np.percentile(valid, [lo_p, hi_p])
    if hi <= lo:
        hi = lo + 1e-6
    out = np.clip((a - lo) / (hi - lo), 0, 1) * 255.0
    return out.astype(np.uint8)


def _crop_read(tif: Path, bbox_4326: Optional[tuple] = None) -> tuple[np.ndarray, tuple]:
    """Read (bands, h, w) from a TIF, optionally clipped to a WGS84 bbox.

    Returns (arr, bounds_in_tif_crs).
    """
    with rasterio.open(tif) as ds:
        if bbox_4326:
            # Assume tif is already EPSG:4326 (that's what Sentinel Hub returns)
            try:
                win = from_bounds(*bbox_4326, transform=ds.transform)
                arr = ds.read(window=win, boundless=True, fill_value=0)
                bounds = bbox_4326
            except Exception:
                arr = ds.read()
                bounds = ds.bounds
        else:
            arr = ds.read()
            bounds = tuple(ds.bounds)
    return arr, bounds


def _resize_min(img: Image.Image, min_side: int = 256) -> Image.Image:
    """Upscale (with BICUBIC) so both dimensions are at least `min_side`."""
    w, h = img.size
    if w >= min_side and h >= min_side:
        return img
    scale = max(min_side / max(w, 1), min_side / max(h, 1))
    new_size = (max(min_side, int(round(w * scale))), max(min_side, int(round(h * scale))))
    return img.resize(new_size, Image.BICUBIC)


def render_observation_png(tif: Path, collection: str, bbox_4326: Optional[tuple] = None, min_side: int = 256) -> bytes:
    arr, _ = _crop_read(tif, bbox_4326)
    if collection == "sentinel-2-l2a":
        r = _stretch(arr[2])
        g = _stretch(arr[1])
        b = _stretch(arr[0])
        rgb = np.stack([r, g, b], axis=-1)
    else:
        vv = arr[0].astype(np.float32)
        db = 10.0 * np.log10(np.clip(vv, 1e-6, None))
        gray = np.clip((db - (-25.0)) / 30.0, 0, 1) * 255.0
        gray = gray.astype(np.uint8)
        rgb = np.stack([gray, gray, gray], axis=-1)
    img = Image.fromarray(rgb, mode="RGB")
    img = _resize_min(img, min_side)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_difference_png(bef: Path, aft: Path, collection: str, bbox_4326: Optional[tuple] = None, min_side: int = 256) -> bytes:
    a_arr, _ = _crop_read(aft, bbox_4326)
    b_arr, _ = _crop_read(bef, bbox_4326)
    h = min(a_arr.shape[1], b_arr.shape[1])
    w = min(a_arr.shape[2], b_arr.shape[2])
    if h < 2 or w < 2:
        return _blank_png()
    a_arr = a_arr[:, :h, :w].astype(np.float32)
    b_arr = b_arr[:, :h, :w].astype(np.float32)
    if collection == "sentinel-2-l2a":
        ndvi_a = (a_arr[3] - a_arr[2]) / (a_arr[3] + a_arr[2] + 1e-6)
        ndvi_b = (b_arr[3] - b_arr[2]) / (b_arr[3] + b_arr[2] + 1e-6)
        diff = np.abs(ndvi_a - ndvi_b)
        maxv = float(np.percentile(diff, 98)) or 1.0
    else:
        db_a = 10 * np.log10(np.clip(a_arr[0], 1e-6, None))
        db_b = 10 * np.log10(np.clip(b_arr[0], 1e-6, None))
        diff = np.abs(db_a - db_b)
        maxv = float(np.percentile(diff, 98)) or 1.0
    norm = np.clip(diff / (maxv + 1e-6), 0, 1)
    # Amber → red heatmap on dark background
    r = (255 * norm).astype(np.uint8)
    g = (176 * (1 - norm ** 0.6)).astype(np.uint8)
    b = (30 * (1 - norm)).astype(np.uint8)
    rgb = np.stack([r, g, b], axis=-1)
    img = Image.fromarray(rgb, mode="RGB")
    img = _resize_min(img, min_side)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _blank_png() -> bytes:
    img = Image.new("RGB", (128, 128), (16, 16, 20))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def polygon_bbox_padded(geometry: dict, pad_frac: float = 0.4) -> tuple:
    ring = geometry["coordinates"][0]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    dx = (maxx - minx) * pad_frac
    dy = (maxy - miny) * pad_frac
    return (minx - dx, miny - dy, maxx + dx, maxy + dy)
