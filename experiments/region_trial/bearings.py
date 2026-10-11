"""Throwaway: read each lot's boundary from the NZ plan sheets, parse whole-circle bearings, check closure + area."""
import json
import sys
from pathlib import Path

from google.genai import types
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from trial import ROOT, walk  # noqa: E402
from app.services.vision import _generate_with_fallback, _get_client  # noqa: E402

DOC = "785f7100-e54f-4e28-aaeb-4cf72a0f6621"  # NZ DP 514130
P = """This is a New Zealand survey plan. For {lot} only, list its boundary in order around the lot,
starting at any corner and returning to it. Return JSON:
{{"calls": [{{"bearing": str exactly as printed (e.g. "123°45'20\\""), "distance_m": number}}],
  "stated_area_m2": number|null}}
Use only the bearings and distances printed along {lot}'s own edges. [] if they are not all printed."""

out = []
for page in (10, 17):
    img = Image.open(ROOT / "data/documents" / DOC / "pages" / f"page_{page:03d}.png").convert("RGB")
    img.thumbnail((3200, 3200))
    for lot in ("Lot 1", "Lot 2", "Lot 7"):
        r = _generate_with_fallback(_get_client(), contents=[img, P.format(lot=lot)],
                                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
        d = json.loads(r.text)
        w = walk(d.get("calls") or [])
        out.append({"page": page, "lot": lot, "stated_area_m2": d.get("stated_area_m2"), "walk": w,
                    "sample_bearings": [c.get("bearing") for c in (d.get("calls") or [])[:3]]})
        print(json.dumps(out[-1], ensure_ascii=False))
