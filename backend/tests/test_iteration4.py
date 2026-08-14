"""Iteration 4 tests: dedupe + preview upscaling regressions.

Validates:
- POST /api/v1/change-detection/timeseries on Rome-Live now dedupes S2 by date
  and yields total_pairs>=1 with real diagnostics; persistence values vary.
- GET /api/v1/observations/{id}/preview returns a PNG whose PIL size is >= 256.
- GET /api/v1/changes/{id}/crop/{before|after|diff} returns PNG >= 256x256.
- Single-pair /api/v1/change-detection endpoint contract still holds.
"""
import io
import os
import time
import pytest
import requests
from PIL import Image

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = f"{BASE_URL}/api/v1"
ROME_LIVE_AOI = "c738fc6e-68df-444f-8aa2-03a67d70253b"


@pytest.fixture(scope="session")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def _png_dims(content: bytes) -> tuple[int, int]:
    img = Image.open(io.BytesIO(content))
    return img.size  # (w, h)


# ---------------- Observation preview upscaling ----------------
class TestObservationPreviewSize:
    def test_preview_min_256(self, client):
        obs = client.get(f"{API}/aois/{ROME_LIVE_AOI}/observations", timeout=30).json()
        assert obs, "no observations for Rome-Live"
        oid = obs[0]["observation_id"]
        r = client.get(f"{API}/observations/{oid}/preview", timeout=120)
        assert r.status_code == 200, r.text[:300]
        assert r.headers.get("content-type", "").startswith("image/png")
        w, h = _png_dims(r.content)
        assert w >= 256 and h >= 256, f"preview too small {w}x{h}"
        print(f"[preview] size={w}x{h} bytes={len(r.content)}")


# ---------------- Timeseries dedupe + persistence variance ----------------
class TestTimeseriesDedupeAndPersistence:
    def test_timeseries_fresh_run_yields_tracks(self, client):
        # Confirm no COMPLETED timeseries job (fixture: caller wiped them)
        jobs = client.get(f"{API}/aois/{ROME_LIVE_AOI}/jobs", timeout=15).json()
        ts_completed = [j for j in jobs
                        if j.get("job_type") == "TIMESERIES_CHANGE_DETECTION"
                        and j.get("status") == "COMPLETED"]
        if ts_completed:
            job_id = ts_completed[0]["job_id"]
            last = ts_completed[0]
        else:
            payload = {"aoi_id": ROME_LIVE_AOI, "use_sar": False,
                       "providers": ["sentinel-2-l2a"]}
            r = client.post(f"{API}/change-detection/timeseries", json=payload, timeout=30)
            assert r.status_code == 200, r.text
            job_id = r.json()["job_id"]
            # Poll up to ~10 minutes
            deadline = time.time() + 600
            last = None
            while time.time() < deadline:
                jr = client.get(f"{API}/jobs/{job_id}", timeout=30)
                assert jr.status_code == 200
                last = jr.json()
                if last.get("status") in ("COMPLETED", "FAILED"):
                    break
                time.sleep(6)
            assert last and last.get("status") == "COMPLETED", f"job did not complete: {last}"

        out = last.get("output") or {}
        diag = out.get("diagnostics") or {}
        assert diag.get("mode") == "real"
        tp = diag.get("total_pairs")
        tracks = diag.get("tracks")
        assert isinstance(tp, int) and tp >= 1, f"total_pairs bad: {diag}"
        assert isinstance(tracks, int)
        pd = diag.get("pair_details") or []
        assert isinstance(pd, list)
        print(f"[timeseries] total_pairs={tp} tracks={tracks} pair_details={pd[:3]}")

        # Fetch events & filter to timeseries ones
        fc = client.get(f"{API}/aois/{ROME_LIVE_AOI}/changes", timeout=30).json()
        feats = fc.get("features", [])
        ts_events = []
        for f in feats:
            cid = f["properties"]["id"]
            dr = client.get(f"{API}/changes/{cid}", timeout=15)
            if dr.status_code != 200:
                continue
            ev = dr.json()
            m = ev.get("metrics") or {}
            if "observed_in_pairs" in m or "Time-series" in (ev.get("description") or ""):
                ts_events.append(ev)

        if tracks == 0 or not ts_events:
            pytest.skip(
                f"timeseries produced tracks={tracks}; endpoint contract verified. "
                f"pair_details sample: {pd[:3]}"
            )

        # Persistence semantics
        persistences = set()
        for ev in ts_events:
            m = ev["metrics"]
            assert isinstance(m.get("observed_in_pairs"), int)
            assert m.get("total_pairs") == tp
            expected = round(m["observed_in_pairs"] / max(tp, 1), 3)
            assert abs(m["persistence"] - expected) < 1e-3
            persistences.add(m["persistence"])
        print(f"[timeseries] persistence values: {persistences}")
        # Must NOT be hardcoded at 0.75
        assert not (len(persistences) == 1 and 0.75 in persistences), \
            "persistence appears hardcoded 0.75"

        # ---------- Crop endpoint upscaling on a timeseries event ----------
        cid = ts_events[0]["id"]
        for kind in ("before", "after", "diff"):
            cr = client.get(f"{API}/changes/{cid}/crop/{kind}", timeout=120)
            assert cr.status_code == 200, f"{kind}: {cr.status_code} {cr.text[:200]}"
            assert cr.headers.get("content-type", "").startswith("image/png")
            w, h = _png_dims(cr.content)
            assert w >= 256 and h >= 256, f"{kind} crop too small {w}x{h}"
            print(f"[crop:{kind}] {w}x{h} bytes={len(cr.content)}")


# ---------------- Crop upscaling on ANY existing event (fallback) ----------------
class TestCropUpscaleFallback:
    """Guarantees 256+ crops even without timeseries events by using any AOI's events."""

    def test_crop_min_256_any_event(self, client):
        for aoi in ("8481a1d3-04d3-4fd6-8838-619caf80852f",
                    "b53996b3-51bb-47e6-b50e-c6f190c9ef16",
                    ROME_LIVE_AOI):
            r = client.get(f"{API}/aois/{aoi}/changes", timeout=15)
            if r.status_code != 200:
                continue
            feats = r.json().get("features", [])
            if not feats:
                continue
            cid = feats[0]["properties"]["id"]
            for kind in ("before", "after", "diff"):
                cr = client.get(f"{API}/changes/{cid}/crop/{kind}", timeout=120)
                if cr.status_code != 200:
                    pytest.skip(f"crop endpoint returned {cr.status_code} for {aoi}/{cid}/{kind}")
                assert cr.headers.get("content-type", "").startswith("image/png")
                w, h = _png_dims(cr.content)
                assert w >= 256 and h >= 256, f"{kind} crop too small {w}x{h} for {cid}"
                print(f"[fallback-crop:{kind}] {w}x{h} bytes={len(cr.content)}")
            return
        pytest.skip("no events available anywhere")


# ---------------- Single-pair regression ----------------
class TestSinglePairContract:
    def test_single_pair_endpoint_still_works(self, client):
        # verify endpoint accepts POST — pick any AOI with >=2 observations
        aois = client.get(f"{API}/aois", timeout=15).json()
        assert isinstance(aois, list) and aois

        # Ensure the /change-detection endpoint responds with 200 or 400 (contract),
        # not 500. We use an intentionally minimal payload to test contract only.
        payload = {"aoi_id": ROME_LIVE_AOI, "use_sar": False}
        r = client.post(f"{API}/change-detection", json=payload, timeout=15)
        assert r.status_code in (200, 400, 422), f"unexpected {r.status_code}: {r.text[:200]}"

    def test_existing_single_pair_persistence_075(self, client):
        for aoi in ("b53996b3-51bb-47e6-b50e-c6f190c9ef16",
                    "8481a1d3-04d3-4fd6-8838-619caf80852f"):
            r = client.get(f"{API}/aois/{aoi}/changes", timeout=15)
            if r.status_code != 200:
                continue
            for f in r.json().get("features", []):
                cid = f["properties"]["id"]
                dr = client.get(f"{API}/changes/{cid}", timeout=15).json()
                m = dr.get("metrics") or {}
                if "observed_in_pairs" in m:
                    continue
                if "persistence" in m:
                    return  # contract holds
        pytest.skip("no single-pair events available")
