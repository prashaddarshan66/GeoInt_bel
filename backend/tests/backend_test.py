"""GEOINT platform Phase 2 backend tests.

Covers status, real CDSE imagery search, change-detection (optical + SAR),
and HTML briefing report endpoint.
"""
import os
import re
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://geoint-detect.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api/v1"

# Live AOI seeded by main agent
LIVE_AOI_ID = "b53996b3-51bb-47e6-b50e-c6f190c9ef16"
ROME_BBOX = [12.48, 41.89, 12.51, 41.91]


@pytest.fixture(scope="session")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------- Status ----------
class TestStatus:
    def test_status_live_mode(self, client):
        r = client.get(f"{API}/status", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["demo_mode"] is False, f"expected demo_mode=false, got {d}"
        assert d["cdse_configured"] is True, f"expected cdse_configured=true, got {d}"


# ---------- Imagery search ----------
class TestImagerySearch:
    def test_search_returns_real_products(self, client):
        payload = {
            "aoi_id": LIVE_AOI_ID,
            "bbox": ROME_BBOX,
            "start_date": "2024-06-01",
            "end_date": "2024-08-31",
            "collections": ["sentinel-2-l2a"],
            "cloud_cover_max": 30,
        }
        r = client.post(f"{API}/imagery/search", json=payload, timeout=90)
        assert r.status_code == 200, r.text
        data = r.json()
        obs = data.get("observations", data) if isinstance(data, dict) else data
        assert isinstance(obs, list) and len(obs) > 0, f"no observations: {data}"
        # At least one product id must be a real S2 product id
        real = [o for o in obs if re.match(r"^S2[AB]_MSIL2A_", str(o.get("product_id", "")))]
        assert len(real) > 0, f"no real S2 product IDs in {[o.get('product_id') for o in obs[:5]]}"
        for o in real[:3]:
            assert not str(o["product_id"]).startswith("DEMO_")


# ---------- Existing change events (avoid extra CDSE quota use) ----------
class TestExistingChanges:
    def test_aoi_changes_geojson(self, client):
        r = client.get(f"{API}/aois/{LIVE_AOI_ID}/changes", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d["type"] == "FeatureCollection"
        feats = d["features"]
        assert len(feats) >= 1
        p = feats[0]["properties"]
        for k in ("id", "change_type", "confidence", "area_m2",
                  "before_imagery_id", "after_imagery_id"):
            assert k in p, f"missing {k}"

    def test_change_detail(self, client):
        r = client.get(f"{API}/aois/{LIVE_AOI_ID}/changes", timeout=30)
        cid = r.json()["features"][0]["properties"]["id"]
        rr = client.get(f"{API}/changes/{cid}", timeout=30)
        assert rr.status_code == 200
        ev = rr.json()
        assert ev["id"] == cid
        assert ev.get("area_m2", 0) > 0
        assert 0 <= ev.get("confidence", -1) <= 1
        assert ev.get("change_type")


# ---------- Jobs diagnostics (existing jobs) ----------
class TestJobsDiagnostics:
    def test_optical_and_sar_jobs_real_mode(self, client):
        r = client.get(f"{API}/aois/{LIVE_AOI_ID}/jobs", timeout=30)
        assert r.status_code == 200
        jobs = r.json()
        assert len(jobs) >= 1
        has_optical_real = False
        has_sar_real = False
        for j in jobs:
            out = (j.get("output") or {})
            diag = out.get("diagnostics", {})
            ip = j.get("input_parameters", {})
            if diag.get("mode") == "real":
                if ip.get("use_sar"):
                    if diag.get("sar_polygons") is not None and diag.get("sar_threshold") is not None:
                        has_sar_real = True
                else:
                    has_optical_real = True
        assert has_optical_real, "no optical job with diagnostics.mode=real"
        assert has_sar_real, "no SAR job with sar_polygons/sar_threshold diagnostics"


# ---------- HTML briefing report ----------
class TestBriefingReport:
    def test_report_html(self, client):
        r = client.get(f"{API}/aois/{LIVE_AOI_ID}/changes", timeout=30)
        cid = r.json()["features"][0]["properties"]["id"]
        rr = client.get(f"{API}/changes/{cid}/report", timeout=30)
        assert rr.status_code == 200
        assert "text/html" in rr.headers.get("content-type", "").lower()
        html = rr.text
        assert "GEOINT Briefing" in html, "title missing"
        # change_type / confidence percentage present
        assert re.search(r"\d{1,3}%", html), "no confidence percentage"
        # before/after product ids
        assert "Before Imagery" in html and "After Imagery" in html
        # confidence-component / metrics table
        assert re.search(r"<table", html, re.I), "no table"
        # ensure real product id appears (S1 or S2)
        assert re.search(r"S[12][AB]?_", html), "no product id in report"


# ---------- Optional: real change-detection run (skipped by default to save quota) ----------
RUN_LIVE_CD = os.environ.get("RUN_LIVE_CD") == "1"


@pytest.mark.skipif(not RUN_LIVE_CD, reason="Set RUN_LIVE_CD=1 to run live change-detection (consumes CDSE quota)")
class TestLiveChangeDetection:
    def _pick_pair(self, client, collection):
        payload = {
            "aoi_id": LIVE_AOI_ID,
            "bbox": ROME_BBOX,
            "start_date": "2024-06-01",
            "end_date": "2024-08-31",
            "collections": [collection],
            "cloud_cover_max": 30,
        }
        r = client.post(f"{API}/imagery/search", json=payload, timeout=90)
        obs = r.json().get("observations", [])
        assert len(obs) >= 2
        return obs[0]["product_id"], obs[-1]["product_id"]

    def test_optical_cd(self, client):
        before, after = self._pick_pair(client, "sentinel-2-l2a")
        r = client.post(f"{API}/change-detection", json={
            "aoi_id": LIVE_AOI_ID,
            "before_product_id": before,
            "after_product_id": after,
            "use_sar": False,
        }, timeout=180)
        assert r.status_code in (200, 202), r.text
        job = r.json()
        job_id = job.get("job_id") or job.get("id")
        # poll
        for _ in range(60):
            jr = client.get(f"{API}/jobs/{job_id}", timeout=30).json()
            if jr.get("status") in ("completed", "failed"):
                break
            time.sleep(3)
        assert jr["status"] == "completed", jr
        diag = jr.get("diagnostics", {})
        assert diag.get("mode") == "real", diag
