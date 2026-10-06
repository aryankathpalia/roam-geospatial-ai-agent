"""
Static map figures for the report: parcels drawn over satellite imagery, and a zoomed-out location map --
the "where is it on the ground" figures a deliverable carries. Same imagery as the app's map (Esri World
Imagery), tiles cached on disk, attribution printed on every figure.
"""

from __future__ import annotations

import io
import math
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

_TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
_LABEL_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
)
ATTRIBUTION = "Imagery: Esri, Maxar, Earthstar Geographics"
_CACHE = Path(__file__).resolve().parents[2] / "data" / "tile_cache"
_TILE = 256
COLOURS = ["#ff2d55", "#ffcc00", "#00e5ff", "#7cff4f", "#ff9500", "#bf5af2", "#ffffff", "#ff6b6b"]


def _px(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = _TILE * 2**z
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(max(-85.05, min(85.05, lat))))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def _tile(client: httpx.Client, url: str, z: int, x: int, y: int) -> Image.Image | None:
    key = _CACHE / ("labels" if "Reference" in url else "imagery") / str(z) / str(x) / f"{y}.png"
    if key.exists():
        try:
            return Image.open(key).convert("RGBA")
        except OSError:
            key.unlink(missing_ok=True)
    try:
        r = client.get(url.format(z=z, x=x, y=y), timeout=20)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGBA")
    except (httpx.HTTPError, OSError):
        return None
    key.parent.mkdir(parents=True, exist_ok=True)
    img.save(key)
    return img


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _zoom_for(bounds: tuple[float, float, float, float], width: int, height: int, fill: float, max_z: int) -> int:
    w, s, e, n = bounds
    for z in range(max_z, 1, -1):
        x0, y0 = _px(w, n, z)
        x1, y1 = _px(e, s, z)
        if (x1 - x0) <= width * fill and (y1 - y0) <= height * fill:
            return z
    return 2


def render(
    rings: list[list[tuple[float, float]]],
    labels: list[str] | None = None,
    *,
    width: int = 1400,
    height: int = 1000,
    fill: float = 0.6,
    max_zoom: int = 20,
    zoom_out: int = 0,
    marker_only: bool = False,
    place_labels: bool = False,
) -> bytes:
    """A PNG of the rings over imagery. `zoom_out` > 0 widens the view (a location map); `marker_only` draws
    the site as a highlighted box with a pin instead of the outlines."""

    pts = [p for r in rings for p in r]
    bounds = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    z = max(2, _zoom_for(bounds, width, height, fill, max_zoom) - zoom_out)
    cx, cy = _px((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2, z)
    left, top = cx - width / 2, cy - height / 2

    canvas = Image.new("RGBA", (width, height), (40, 40, 40, 255))
    with httpx.Client(headers={"User-Agent": "ROAM-report/1.0"}) as client:
        layers = [_TILE_URL] + ([_LABEL_URL] if place_labels else [])
        for url in layers:
            for tx in range(int(left // _TILE), int((left + width) // _TILE) + 1):
                for ty in range(int(top // _TILE), int((top + height) // _TILE) + 1):
                    if not (0 <= ty < 2**z):
                        continue
                    img = _tile(client, url, z, tx % 2**z, ty)
                    if img is not None:
                        canvas.alpha_composite(img, (int(tx * _TILE - left), int(ty * _TILE - top)))

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    def to_xy(lon: float, lat: float) -> tuple[float, float]:
        x, y = _px(lon, lat, z)
        return x - left, y - top

    if marker_only:
        x0, y1 = to_xy(bounds[0], bounds[1])
        x1, y0 = to_xy(bounds[2], bounds[3])
        pad = 6
        d.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], outline=(255, 45, 85, 255), width=4)
        mx, my = (x0 + x1) / 2, y0 - pad
        d.polygon([(mx, my), (mx - 14, my - 34), (mx + 14, my - 34)], fill=(255, 45, 85, 255))
        d.ellipse([mx - 15, my - 56, mx + 15, my - 26], fill=(255, 45, 85, 255), outline=(255, 255, 255, 255), width=3)
    else:
        font = _font(max(14, width // 70))
        for i, ring in enumerate(rings):
            colour = COLOURS[i % len(COLOURS)]
            xy = [to_xy(*p) for p in ring]
            rgb = tuple(int(colour[k : k + 2], 16) for k in (1, 3, 5))
            d.polygon(xy, fill=rgb + (45,))
            d.line(xy + [xy[0]], fill=rgb + (255,), width=max(3, width // 450))
            if labels and i < len(labels):
                lx = sum(p[0] for p in xy[:-1]) / max(1, len(xy) - 1)
                ly = sum(p[1] for p in xy[:-1]) / max(1, len(xy) - 1)
                text = labels[i]
                tw, th = d.textbbox((0, 0), text, font=font)[2:]
                d.rectangle([lx - tw / 2 - 5, ly - th / 2 - 3, lx + tw / 2 + 5, ly + th / 2 + 4], fill=(0, 0, 0, 160))
                d.text((lx - tw / 2, ly - th / 2), text, font=font, fill=(255, 255, 255, 255))
    canvas.alpha_composite(overlay)

    _decorate(canvas, z, (bounds[1] + bounds[3]) / 2)
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _decorate(img: Image.Image, z: int, lat: float) -> None:
    """North arrow, scale bar and attribution."""

    d = ImageDraw.Draw(img)
    w, h = img.size
    font = _font(max(12, w // 95))
    # north arrow (Web Mercator: north is up)
    ax, ay = w - 50, 30
    d.polygon([(ax, ay), (ax - 14, ay + 40), (ax, ay + 30), (ax + 14, ay + 40)], fill=(255, 255, 255, 235), outline=(0, 0, 0))
    d.text((ax - 6, ay + 42), "N", font=font, fill=(255, 255, 255))
    # scale bar: a round ground length near 1/5 of the width
    m_per_px = 156543.03392 * math.cos(math.radians(lat)) / 2**z
    target = w / 5 * m_per_px
    nice = max(v for v in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000) if v <= max(1, target))
    bar = nice / m_per_px
    bx, by = 24, h - 46
    label = f"{nice:g} m" if nice < 1000 else f"{nice / 1000:g} km"
    text = f"{label}  ({nice * 3.28084:,.0f} ft)"
    box_w = max(bar, d.textbbox((0, 0), text, font=font)[2])
    d.rectangle([bx - 8, by - 26, bx + box_w + 10, by + 16], fill=(0, 0, 0, 150))
    d.rectangle([bx, by, bx + bar, by + 8], fill=(255, 255, 255))
    d.rectangle([bx, by, bx + bar / 2, by + 8], fill=(0, 0, 0), outline=(255, 255, 255))
    d.text((bx, by - 22), text, font=font, fill=(255, 255, 255))
    tw = d.textbbox((0, 0), ATTRIBUTION, font=font)[2]
    d.rectangle([w - tw - 16, h - 26, w, h], fill=(0, 0, 0, 150))
    d.text((w - tw - 8, h - 22), ATTRIBUTION, font=font, fill=(235, 235, 235))
