"""Change detection pipeline.

DEMO mode: deterministic synthetic change polygons derived from the AOI bbox and
image-pair IDs. Real mode would use Rasterio + NDVI/NDBI/SCL + CVA/IR-MAD +
Otsu thresholding (Section 4 of the spec). We expose the same output contract so
callers don't change when a real backend is plugged in.
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime, timezone
from typing import Any

CHANGE_TYPES = [
    "Construction",
    "Land Clearing",
    "Building",
    "Road",
    "Vegetation",
    "Infrastructure",
    "Other",
]


def _rng_from_pair(before_id: str, after_id: str, aoi_id: str) -> random.Random:
    seed_int = int(
        hashlib.md5(f"{before_id}|{after_id}|{aoi_id}".encode()).hexdigest()[:12], 16
    )
    return random.Random(seed_int)


def _poly_from_center(cx: float, cy: float, size_deg: float, sides: int = 6) -> list[list[float]]:
    coords: list[list[float]] = []
    for i in range(sides):
        a = (2 * math.pi * i) / sides
        coords.append([cx + math.cos(a) * size_deg, cy + math.sin(a) * size_deg])
    coords.append(coords[0])
    return coords


def _area_m2_from_polygon(coords: list[list[float]]) -> float:
    """Approximate polygon area in m^2 using equirectangular projection."""
    if len(coords) < 4:
        return 0.0
    mean_lat = sum(c[1] for c in coords) / len(coords)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mean_lat))
    # shoelace
    area = 0.0
    for i in range(len(coords) - 1):
        x1, y1 = coords[i]
        x2, y2 = coords[i + 1]
        area += (x1 * m_per_deg_lon) * (y2 * m_per_deg_lat) - (x2 * m_per_deg_lon) * (
            y1 * m_per_deg_lat
        )
    return abs(area) / 2.0


def detect_changes(
    aoi_id: str,
    aoi_bbox: list[float],
    before_observation: dict[str, Any],
    after_observation: dict[str, Any],
    sensors_used: list[str],
) -> list[dict[str, Any]]:
    """Return a list of candidate change-event dicts.

    Each dict is ready to persist into the `change_events` collection.
    """
    rng = _rng_from_pair(
        before_observation["product_id"], after_observation["product_id"], aoi_id
    )
    min_lon, min_lat, max_lon, max_lat = aoi_bbox
    width = max_lon - min_lon
    height = max_lat - min_lat

    n_polys = rng.randint(2, 6)
    events: list[dict[str, Any]] = []
    both_sensors = "optical" in sensors_used and "sar" in sensors_used
    for _ in range(n_polys):
        cx = min_lon + rng.uniform(0.15, 0.85) * width
        cy = min_lat + rng.uniform(0.15, 0.85) * height
        size_deg = rng.uniform(0.0008, 0.004) * max(width, height) / 0.02
        coords = _poly_from_center(cx, cy, size_deg)
        area_m2 = _area_m2_from_polygon(coords)

        change_score = round(rng.uniform(0.35, 0.95), 3)
        # confidence formula: magnitude, valid-pixel fraction, persistence, multi-sensor
        magnitude_z = min(1.0, change_score / 0.9)
        valid_pixel_frac = round(rng.uniform(0.7, 1.0), 3)
        persistence = round(rng.uniform(0.6, 1.0), 3)
        multi_sensor_bonus = 0.15 if both_sensors and rng.random() > 0.4 else 0.0
        confidence = round(
            min(
                0.99,
                0.4 * magnitude_z
                + 0.25 * valid_pixel_frac
                + 0.2 * persistence
                + multi_sensor_bonus,
            ),
            3,
        )
        detected_by = ["optical", "sar"] if multi_sensor_bonus > 0 else [rng.choice(sensors_used)]
        change_type = rng.choice(CHANGE_TYPES)

        events.append(
            {
                "aoi_id": aoi_id,
                "before_imagery_id": before_observation["product_id"],
                "after_imagery_id": after_observation["product_id"],
                "first_seen": before_observation["observation_datetime"],
                "last_seen": after_observation["observation_datetime"],
                "change_type": change_type,
                "change_score": change_score,
                "confidence": confidence,
                "area_m2": round(area_m2, 2),
                "geometry": {"type": "Polygon", "coordinates": [coords]},
                "status": "new",
                "description": f"Heuristic detection between {before_observation['product_id'][:20]}… and {after_observation['product_id'][:20]}…",
                "detected_by_sensors": detected_by,
                "metrics": {
                    "magnitude_z": magnitude_z,
                    "valid_pixel_fraction": valid_pixel_frac,
                    "persistence": persistence,
                    "multi_sensor": multi_sensor_bonus > 0,
                },
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    # sort descending by confidence
    events.sort(key=lambda e: e["confidence"], reverse=True)
    return events
