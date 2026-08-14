# GEOINT Satellite Change Detection Platform — PRD

## Problem Statement
Real, working web application for GEOINT analysts to draw an AOI, search Sentinel-2 optical and Sentinel-1 radar imagery from CDSE, preprocess, run change detection, generate polygons/heatmaps, and browse a historical timeline. See original spec in initial task brief.

## User Choices (2026-08-14)
- Adapt to environment: MongoDB with GeoJSON, FastAPI BackgroundTasks
- DEMO mode fallback (CDSE creds pluggable via .env)
- Ship Phase 1 + skeleton of Phases 2-4
- Professional GEOINT/defense dark tactical theme

## Architecture
- Backend: FastAPI + Motor(MongoDB). Files: server.py (routes), cdse.py (OAuth2 + STAC + demo synth), change_detection.py (heuristic detection with confidence formula).
- Frontend: React + OpenLayers 10 (CartoDB dark_matter base). Files: App.js (dashboard shell), MapView.jsx, api.js, index.css (Chivo + IBM Plex + JetBrains Mono).
- Storage: MongoDB collections aois / observations / jobs / change_events. GeoJSON 2dsphere indexes.

## Implemented (2026-08-14)
- CDSE OAuth2 client-credentials auth w/ token caching + STAC search for Sentinel-2 L2A and Sentinel-1 GRD
- Deterministic DEMO-mode synth observations (5-day S2 cadence, 6-day S1)
- AOI CRUD with area validation, bbox derivation, 2dsphere index
- Imagery search + persist observations (upsert)
- Async change-detection jobs via BackgroundTasks with 6-stage progress (SEARCHING→DOWNLOADING→PREPROCESSING→DETECTING→HEATMAP→POLYGONS)
- Change detection with explicit confidence formula: magnitude Z + valid-pixel fraction + persistence + multi-sensor bonus
- Change events with classification, area (m²), score, confidence, sensor list, metrics
- Dashboard endpoint (AOI, observation counts by sensor, change counts, high-confidence, largest)
- Timeline endpoint
- Frontend: full-screen OpenLayers map, AOI draw/edit, dense left panel (search + filters), right panel (dashboard + observations + change events + detail), bottom timeline with markers
- DEMO DATA badge, tactical amber/cyan accent, monospace readouts, corner brackets on thumbnails, scanline overlays

## Backlog / Next
- P1: Real Sentinel Hub Process API integration for AOI-clipped GeoTIFFs (currently DEMO)
- P1: Real optical pipeline (SCL masking, CVA/IR-MAD, Otsu thresholding)
- P1: Real SAR pipeline (speckle filter + log-ratio)
- P2: Sentinel-2 tile preview on map (WMS/WMTS layer)
- P2: Multi-date temporal analysis (first_seen / last_seen across N observations)
- P2: Job cancellation, WebSocket progress

## 2026-08-14 (Iteration 2) — LIVE CDSE + Real Pipelines
- Real CDSE OAuth2 client credentials wired into `/app/backend/.env`; STAC URL corrected to `/api/v1/catalog/1.0.0/search`
- Bug fixed: `load_dotenv()` moved BEFORE `from cdse import cdse_client` so module-level env reads work
- Real optical pipeline (`pipelines.detect_optical_changes`): SCL cloud/shadow/snow masking (classes {4,5,6,7}), CVA magnitude on z-scored [NDVI, NDBI, red, NIR] between dates, Otsu adaptive threshold, morphological opening/closing + remove_small_objects, connected-component labeling, `rasterio.features.shapes` polygonization
- Real SAR pipeline (`pipelines.detect_sar_changes`): median speckle filter (5×5), 10·log10 conversion, log-ratio magnitude across VV+VH, Otsu, morphology → polygons
- Multi-sensor fusion (`pipelines.fuse_and_finalize`): shapely IoU > 0.15 pairs optical+SAR polygons; explicit confidence formula: 0.40·mag_z + 0.25·valid_pixel_frac + 0.20·persistence + 0.15·multi_sensor
- Rule-based classifier (Construction / Building / Land Clearing / Infrastructure / Vegetation / Other) based on magnitude, area, and sensor agreement
- Sentinel Hub Process API integration (`cdse.fetch_geotiff`): S2 evalscript for B02,B03,B04,B08,B11,SCL (default DN units to satisfy SCL constraint); S1 evalscript for VV,VH with `orthorectify + GAMMA0_TERRAIN + COPERNICUS_30` terrain correction
- Raster caching to `/app/data/raw/{aoi_id}/{obs}/{collection}.tif` — reuses on repeat pairs
- Shareable HTML briefing card at `GET /api/v1/changes/{id}/report` with tactical dark styling and print-to-PDF button; JSON geometry embedded safely via `json.dumps`
- Frontend: added "Export Briefing Card" button (`data-testid=export-report-btn`) in change detail panel
- Verified: MODE indicator flips to LIVE, real change polygons (Land Clearing 4.30 ha, 75% confidence, SAR-detected) render on the map with real Rome imagery
