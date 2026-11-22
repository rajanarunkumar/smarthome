"""Zone photos, photo advice, Claude request shape (with a fake client), and flow tests."""

import asyncio
import io
import json
import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from PIL import Image

from hub import photos
from hub.flowtest import FlowTests, measure, plan
from hub.models import HubError
from hub.photos import PhotoStore, assess_with_claude, greenness, zone_advice

GRASS, STRAW, SKY = (70, 140, 50), (180, 150, 90), (120, 170, 235)


def image(*colors, size=(200, 100)) -> bytes:
    """An image split into equal vertical bands of the given colours."""
    img = Image.new("RGB", size)
    band = size[0] // len(colors)
    for i, c in enumerate(colors):
        img.paste(c, (i * band, 0, (i + 1) * band, size[1]))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=95)
    return out.getvalue()


def test_greenness_classes_grass_straw_and_other():
    assert greenness(image(GRASS))["green_share"] == 1.0
    assert greenness(image(STRAW))["green_share"] == 0.0
    half = greenness(image(GRASS, STRAW, SKY, SKY))
    assert abs(half["green_share"] - 0.5) < 0.02 and abs(half["coverage"] - 0.5) < 0.02
    assert greenness(image(SKY))["green_share"] is None  # no grass in the photo
    with pytest.raises(HubError):
        greenness(b"not a picture")


def test_advice_rules():
    dormant = zone_advice(0.2, None, None, 0.5, 0.1, tmean7=52)
    assert dormant["action"] == "reduce" and "dormant" in dormant["reason"]
    assert zone_advice(0.35, None, None, 0.4, 0.8, 80)["change_pct"] == 25
    assert zone_advice(0.55, None, None, None, None, 80)["action"] == "increase"
    # Brown even though it got plenty: coverage problem, not too little water.
    assert zone_advice(0.45, None, None, 1.4, 0.8, 85)["action"] == "inspect"
    reduce = zone_advice(0.9, 0.8, None, 1.3, 0.8, 85)
    assert reduce["action"] == "reduce" and "Greener by 10%" in reduce["reason"]
    assert zone_advice(None, None, None, None, None, 80)["action"] == "wait"
    ai = {"verdict": "reduce", "confidence": "high", "observations": ["mushrooms"], "summary": "Mushrooms and soggy soil."}
    assert zone_advice(0.95, None, ai, None, None, 80) == {"action": "reduce", "change_pct": -25,
                                                           "reason": "Claude: Mushrooms and soggy soil."}
    dry = {"verdict": "increase", "confidence": "medium", "observations": ["dry spots in arcs"], "summary": "Dry arcs."}
    assert zone_advice(0.7, None, dry, 1.2, 0.8, 85)["action"] == "inspect"  # dry arcs + plenty of water = heads


def test_claude_request_shape(monkeypatch):
    sent = {}

    class FakeMessages:
        def create(self, **kw):
            sent.update(kw)
            body = {"verdict": "keep", "confidence": "high", "dormant": False, "observations": ["even color"], "summary": "Healthy."}
            return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5",
                                   content=[SimpleNamespace(type="text", text=json.dumps(body))])

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages())))
    out = assess_with_claude("sk-ant-test", image(GRASS), "Zone: Zone 1 (Front lawn)")
    assert out["verdict"] == "keep" and out["model"] == "claude-opus-5-5"
    assert sent["model"] == "claude-opus-5-5" and sent["fallbacks"] == "default"
    assert sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert sent["output_config"]["format"]["schema"]["additionalProperties"] is False
    img, text = sent["messages"][0]["content"]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/jpeg" and text["text"].startswith("Zone:")


def test_photo_store(tmp_path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PhotoStore(db, tmp_path / "photos")
    p = asyncio.run(store.add(3, image(GRASS, STRAW), datetime(2026, 11, 21, 10, 0)))
    assert p["zone"] == 3 and abs(p["green_share"] - 0.5) < 0.02 and store.path(p["id"]).exists()
    store.set_ai(p["id"], {"verdict": "keep"})
    assert store.for_zone(3)[0]["ai"] == {"verdict": "keep"}
    store.delete(p["id"])
    assert store.for_zone(3) == [] and not any((tmp_path / "photos").iterdir())


def test_flow_test_plan_runs_one_zone_per_hour_six_per_night():
    rows = plan([1, 2, 3, 4, 5, 6, 7, 8], 10, date(2026, 11, 22))
    assert [r["start"] for r in rows] == [
        "2026-11-22T00:05", "2026-11-22T01:05", "2026-11-22T02:05", "2026-11-22T03:05", "2026-11-22T04:05",
        "2026-11-22T05:05", "2026-11-23T00:05", "2026-11-23T01:05"]


def test_flow_test_measurement_subtracts_overnight_use():
    hourly = {f"2026-11-{d:02d}T02": 1.0 for d in range(10, 22)}
    hourly["2026-11-22T02"] = 1.0 + 10 * 7.5  # zone ran 10 min at 7.5 gpm
    result = measure({"start": "2026-11-22T02:05", "minutes": 10}, hourly, set())
    assert result == {"gallons": 75.0, "gpm": 7.5, "baseline": 1.0}
    assert measure({"start": "2026-11-23T02:05", "minutes": 10}, hourly, set()) is None  # not posted yet


class FakeRainBird:
    def __init__(self):
        self.calls = []

    async def set_state(self, device_id, state):
        self.calls.append((state.zone, state.minutes))


def test_flow_tests_run_on_time_and_skip_when_late():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    rb = FakeRainBird()
    ft = FlowTests(db, rb)
    ft.schedule([1, 2], 10, datetime(2026, 11, 21, 20, 0))
    with pytest.raises(HubError):  # one plan at a time
        ft.schedule([3], 10, datetime(2026, 11, 21, 20, 0))
    asyncio.run(ft.run_due(datetime(2026, 11, 22, 0, 5)))
    asyncio.run(ft.run_due(datetime(2026, 11, 22, 1, 40)))  # zone 2 was due at 1:05: the Mac slept, so it's skipped
    assert rb.calls == [(1, 10)]
    status = {t["zone"]: t["status"] for t in ft.tests()}
    assert status == {1: "ran", 2: "failed"}
    hourly = {f"2026-11-{d:02d}T00": 0.5 for d in range(10, 22)} | {"2026-11-22T00": 0.5 + 10 * 9.2}
    assert ft.update_measurements(hourly, set(), datetime(2026, 11, 22, 9, 0)) == 1
    assert ft.measured_gpm() == {1: {"gpm": 9.2, "tests": 1, "last": "2026-11-22T00:05"}}


def test_photo_upload_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        h = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        r = client.post("/api/rainbird/zones/2/photos", content=image(GRASS), headers={**h, "Content-Type": "image/jpeg"})
        assert r.status_code == 200 and r.json()["photo"]["green_share"] == 1.0 and r.json()["photo"]["ai"] is None
        pid = r.json()["photo"]["id"]
        assert client.get(f"/api/photos/{pid}", headers=h).headers["content-type"] == "image/jpeg"
        assert client.post("/api/rainbird/zones/2/photos", content=b"x", headers={**h, "Content-Type": "text/plain"}).status_code == 415
        assert client.post(f"/api/photos/{pid}/assess", headers=h).status_code == 409  # no API key set
