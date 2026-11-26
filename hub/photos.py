"""Zone photos: how green each sprinkler zone looks, and what to change.

Every photo is analysed on the Mac with no service involved: each pixel is
classed as green grass, brown/straw, or neither (sky, pavement, shade),
giving a green share = green / (green + brown). Optionally, with an Anthropic
API key in Settings, Claude also looks at the photo for signs of under- or
over-watering (dry patches, wilting, fungus, mushrooms, standing water).

zone_advice() then combines the photo with what the zone got vs what the
lawn needed and the season: warm-season Texas grass goes dormant and brown
when it's cold, whatever the watering, so brown in winter never means
"water more".
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from datetime import datetime
from pathlib import Path

from .models import HubError

log = logging.getLogger(__name__)
MAX_UPLOAD = 15 * 1024 * 1024
DORMANT_BELOW_F = 60.0
CLAUDE_MODEL = "claude-opus-5-5"
PHOTO_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")   # cheapest first; Opus is the default

try:
    from PIL import Image, ImageOps
    HAVE_PIL = True
except ImportError:  # pragma: no cover
    HAVE_PIL = False


def greenness(image_bytes: bytes) -> dict:
    """{"green_share", "coverage", "vari"}: share of grass pixels that are green, how much of the
    photo is grass at all, and the mean visible-atmospherically-resistant index of the green pixels."""
    if not HAVE_PIL:
        raise HubError(501, "Photo analysis needs Pillow. Run pip install -r requirements.txt.")
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - any decoding problem means "not a usable photo"
        raise HubError(400, f"That file isn't a photo the hub can read: {exc}") from exc
    img.thumbnail((512, 512))
    green = brown = valid = 0
    vari_sum = 0.0
    pixels = img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata()  # Pillow 13+ / older
    for r, g, b in pixels:
        if max(r, g, b) < 35 or min(r, g, b) > 235:
            continue  # deep shadow or blown highlight: no colour information
        valid += 1
        total = r + g + b
        exg = (2 * g - r - b) / total
        if exg > 0.06 and g > r:
            green += 1
            denom = g + r - b
            if denom:
                vari_sum += (g - r) / denom
        elif r >= g > b and r - b > 25:
            brown += 1  # straw, tan and brown turf (red at least green, both well above blue)
    grass = green + brown
    if valid == 0 or grass / valid < 0.15:
        return {"green_share": None, "coverage": round(grass / valid, 2) if valid else 0.0, "vari": None}
    return {"green_share": round(green / grass, 3), "coverage": round(grass / valid, 2),
            "vari": round(vari_sum / green, 3) if green else None}


def to_jpeg(image_bytes: bytes, max_side: int = 1568) -> bytes:
    """Re-encode as a JPEG no larger than Claude's recommended image size."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


ASSESS_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["increase", "reduce", "keep", "not_lawn", "unclear"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "dormant": {"type": "boolean"},
        "observations": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["verdict", "confidence", "dormant", "observations", "summary"],
    "additionalProperties": False,
}

ASSESS_SYSTEM = (
    "You assess residential lawn and garden photos for irrigation decisions in North Texas "
    "(warm-season turf such as Bermuda, St. Augustine or Zoysia unless the photo shows otherwise). "
    "Judge only from what is visible. Signs of too little water: grayish or blue-green cast, footprints "
    "that stay, curled or folded blades, straw-colored patches in sunny or elevated spots, dry spots in "
    "the shape of sprinkler gaps. Signs of too much: mushrooms, fungus rings or brown patch, moss or algae, "
    "soggy or puddled soil, very lush shallow growth next to runoff. Warm-season grass goes dormant and "
    "evenly tan in cool weather; that is normal and not a reason to water more. If the photo isn't of turf "
    "or planting beds, say not_lawn; if it can't be judged (too dark, too far), say unclear. "
    "Observations are short phrases about what you see. The summary is one or two plain sentences."
)


def assess_with_claude(api_key: str, model: str, image_bytes: bytes, context: str) -> dict:
    """Blocking call to Claude with the photo. Returns the ASSESS_SCHEMA dict plus "model"."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    data = base64.standard_b64encode(to_jpeg(image_bytes)).decode()
    try:
        response = client.beta.messages.create(
            model=model if model in PHOTO_MODELS else CLAUDE_MODEL,
            max_tokens=16000,
            # If a safety classifier declines, Anthropic re-runs it on its recommended model (not offered for Haiku).
            **({} if "haiku" in model else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}),
            system=ASSESS_SYSTEM,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": ASSESS_SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                {"type": "text", "text": context},
            ]}],
        )
    except anthropic.AuthenticationError as exc:
        raise HubError(401, "Anthropic rejected the API key. Check it in Settings → Photo analysis.") from exc
    except anthropic.RateLimitError as exc:
        raise HubError(429, "Claude is busy right now. Try the photo again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        raise HubError(502, f"Claude couldn't analyse the photo ({exc.status_code}).") from exc
    except anthropic.APIConnectionError as exc:
        raise HubError(504, "Couldn't reach Claude. Check the Mac's internet connection.") from exc
    if response.stop_reason == "refusal":
        raise HubError(422, "Claude declined to analyse this photo.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    result = json.loads(text)
    result["model"] = response.model
    return result


def zone_advice(green_share: float | None, previous_green: float | None, ai: dict | None,
                applied_in: float | None, need_in: float | None, tmean7: float | None) -> dict:
    """{"action": increase|reduce|keep|inspect|wait, "change_pct": int, "reason": str}."""
    if tmean7 is not None and tmean7 < DORMANT_BELOW_F:
        heavy = applied_in is not None and applied_in > 0.25
        return {"action": "reduce" if heavy else "keep", "change_pct": -50 if heavy else 0,
                "reason": f"It's been cool (about {tmean7:.0f}°F on average), so warm-season grass is likely dormant and "
                          "tan is normal. Water only every 3–4 weeks without rain"
                          + (", so cut back." if heavy else ".")}
    trend = ""
    if green_share is not None and previous_green is not None:
        delta = green_share - previous_green
        if abs(delta) >= 0.05:
            trend = f" Greener by {delta:.0%} since the last photo." if delta > 0 else f" {-delta:.0%} less green since the last photo."
    if ai and ai.get("verdict") in ("increase", "reduce") and ai.get("confidence") in ("medium", "high"):
        if ai["verdict"] == "increase" and applied_in is not None and need_in and applied_in >= need_in * 1.25:
            return {"action": "inspect", "change_pct": 0,
                    "reason": f"Claude sees signs of dryness ({'; '.join(ai['observations'][:2])}), but the zone already got "
                              f"{applied_in:.2f} in vs {need_in:.2f} in needed. Check for clogged or misaligned heads before adding time." + trend}
        pct = 15 if ai["confidence"] == "medium" else 25
        return {"action": ai["verdict"], "change_pct": pct if ai["verdict"] == "increase" else -pct,
                "reason": f"Claude: {ai['summary']}" + trend}
    if green_share is None:
        return {"action": "wait", "change_pct": 0, "reason": "Take a photo of this zone's grass, in daylight, from the same spot each time."}
    if green_share < 0.6:
        if applied_in is not None and need_in and applied_in >= need_in * 1.25:
            return {"action": "inspect", "change_pct": 0,
                    "reason": f"Only {green_share:.0%} green even though it got {applied_in:.2f} in vs {need_in:.2f} in needed. "
                              "More water won't fix uneven coverage: check heads and look for brown patch." + trend}
        pct = 25 if green_share < 0.4 else 15
        return {"action": "increase", "change_pct": pct, "reason": f"{green_share:.0%} of the grass is green." + trend}
    if green_share >= 0.85 and applied_in is not None and need_in and applied_in > need_in * 1.25:
        return {"action": "reduce", "change_pct": -20,
                "reason": f"{green_share:.0%} green and it got {applied_in:.2f} in vs {need_in:.2f} in needed: there's room to cut back." + trend}
    return {"action": "keep", "change_pct": 0, "reason": f"{green_share:.0%} green." + trend}


class PhotoStore:
    def __init__(self, db, folder: Path) -> None:
        self.db, self.folder = db, folder
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        db.execute("""CREATE TABLE IF NOT EXISTS zone_photos (
            id INTEGER PRIMARY KEY, zone INTEGER NOT NULL, taken TEXT NOT NULL, file TEXT NOT NULL,
            green_share REAL, coverage REAL, vari REAL, ai TEXT)""")
        db.commit()

    async def add(self, zone: int, image_bytes: bytes, now: datetime) -> dict:
        if len(image_bytes) > MAX_UPLOAD:
            raise HubError(413, "That photo is larger than 15 MB.")
        metrics = await asyncio.to_thread(greenness, image_bytes)
        name = f"zone{zone}-{now.strftime('%Y%m%d-%H%M%S')}.jpg"
        jpeg = await asyncio.to_thread(to_jpeg, image_bytes, 2048)
        (self.folder / name).write_bytes(jpeg)
        cur = self.db.execute("INSERT INTO zone_photos (zone, taken, file, green_share, coverage, vari) VALUES (?, ?, ?, ?, ?, ?)",
                              (zone, now.isoformat(timespec="minutes"), name, metrics["green_share"], metrics["coverage"], metrics["vari"]))
        self.db.commit()
        return self.get(cur.lastrowid)

    def set_ai(self, photo_id: int, ai: dict) -> None:
        self.db.execute("UPDATE zone_photos SET ai = ? WHERE id = ?", (json.dumps(ai), photo_id))
        self.db.commit()

    def _row(self, r) -> dict:
        d = dict(r)
        d["ai"] = json.loads(d["ai"]) if d["ai"] else None
        return d

    def get(self, photo_id: int) -> dict:
        r = self.db.execute("SELECT * FROM zone_photos WHERE id = ?", (photo_id,)).fetchone()
        if r is None:
            raise HubError(404, "Photo not found")
        return self._row(r)

    def path(self, photo_id: int) -> Path:
        return self.folder / self.get(photo_id)["file"]

    def for_zone(self, zone: int, limit: int = 12) -> list[dict]:
        rows = self.db.execute("SELECT * FROM zone_photos WHERE zone = ? ORDER BY taken DESC LIMIT ?", (zone, limit))
        return [self._row(r) for r in rows]

    def delete(self, photo_id: int) -> None:
        path = self.path(photo_id)
        self.db.execute("DELETE FROM zone_photos WHERE id = ?", (photo_id,))
        self.db.commit()
        path.unlink(missing_ok=True)
