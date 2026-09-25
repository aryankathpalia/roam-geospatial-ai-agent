"""
PaddleOCR-based text extraction, one call per PAGE (not per detected
region). See app/pipeline/page_ocr.py for why: detecting+reading every
region separately (up to 185 calls for one document) took ~150s in
production; running one full-page OCR pass and matching the resulting
lines back to ROAM's layout regions geometrically cuts that to one call
per page, fanned out across pages in parallel (see modal_app.py).

Two things had to be fixed empirically before this was usable, not
assumed from docs:

1. PaddlePaddle 3.3.0+ has a regression where the default
   enable_mkldnn=True crashes with "ConvertPirAttribute2RuntimeAttribute
   not support" on CPU inference (confirmed: PaddlePaddle/Paddle#77340).
   requirements.txt pins paddlepaddle==3.2.2, which predates it.

2. PaddleOCR's default "medium" model preset (PP-OCRv6_medium_*) took
   ~105s for a single mid-sized crop -- unusable. Switching to the
   "mobile" preset (PP-OCRv5_mobile_det/rec) cut that dramatically, with
   no meaningful accuracy loss (0.96-1.00 confidence on the same
   content). Always use the mobile preset here.

GPU was investigated and ruled out: Modal requires a payment method on
file for any GPU function, even within the free credit, which conflicts
with this deployment's no-card requirement. A CPU-only alternative
engine (RapidOCR) was also tested directly against our real documents
and was slower than this setup even after tuning (16-22s vs 6-8s on the
same crop) -- not used.

ParcelMap crops specifically use a SEPARATE engine (get_parcelmap_engine
/ run_parcelmap_ocr below), with orientation detection turned back ON --
scoped narrowly to ParcelMap crops rather than flipping it on for
get_engine() (used for every other page's OCR in production). Confirmed
directly on real documents: with orientation detection off, a rotated
survey page (a real, common case -- confirmed while annotating this
corpus that ParcelMap sheets are rotated far more often than the rest
of a document) reads as pure noise (0.00-0.60 confidence, no real
words at all); with it on, the same page reads real, mostly-correct
text (0.7-1.0 confidence on most lines). It doesn't fully solve
everything -- a dimension label drawn at its own diagonal angle WITHIN
an otherwise right-side-up drawing (not the whole page being rotated)
can still come out garbled, since 4-way page/textline orientation
classification doesn't correct for arbitrary in-drawing angles. Still a
large, confirmed improvement over no orientation handling at all.
"""

import re
import threading
from dataclasses import dataclass

import numpy as np
from paddleocr import PaddleOCR
from PIL import Image

_engine: PaddleOCR | None = None
_parcelmap_engine: PaddleOCR | None = None
_parcelmap_engine_lock = threading.Lock()

# Below this mean confidence, treat the OCR pass as unreliable rather
# than trusting garbled text -- callers should fall back to Gemini
# vision reading the raw image directly. Chosen from real data: the
# rotated-page failure case averaged well under this (many 0.00-0.20
# lines dragging the mean down); a correctly-oriented real page (NVZ,
# and the same document after orientation correction) averaged well
# above it. Not a precisely tuned threshold -- a coarse gate to catch
# the "this OCR pass was garbage" case, not a quality score.
PARCELMAP_OCR_CONFIDENCE_FLOOR = 0.6

# PaddleOCR's underlying predictor is not thread-safe -- calling
# .predict() on the same engine from two threads at once crashes with
# "PreconditionNotMetError: Tensor holds no memory." Each Modal
# container running ocr_page_remote has its own process (and therefore
# its own engine instance), so this only serializes calls *within* one
# container, not across the parallel fan-out.
_engine_lock = threading.Lock()


@dataclass
class OCRLine:
    text: str
    confidence: float
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2, page coordinates


def get_engine() -> PaddleOCR:
    """Load (and cache) the PaddleOCR engine, mobile preset."""

    global _engine

    if _engine is None:
        _engine = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            cpu_threads=8,
        )

    return _engine


def run_page_ocr(image: Image.Image) -> list[OCRLine]:
    """
    Run OCR once on a whole rendered page, returning every detected
    text line with its bounding box -- callers match lines back to
    ROAM's layout regions geometrically (app/pipeline/page_ocr.py).
    """

    arr = np.array(image.convert("RGB"))
    with _engine_lock:
        result = get_engine().predict(arr)

    lines: list[OCRLine] = []
    for page_result in result:
        texts = page_result.get("rec_texts", [])
        scores = page_result.get("rec_scores", [])
        boxes = page_result.get("rec_boxes", [])
        for text, score, box in zip(texts, scores, boxes):
            x1, y1, x2, y2 = (float(v) for v in box)
            lines.append(
                OCRLine(
                    text=text,
                    confidence=round(float(score), 3),
                    bbox=(x1, y1, x2, y2),
                )
            )

    return lines


def get_parcelmap_engine() -> PaddleOCR:
    """
    Separate PaddleOCR instance for ParcelMap crops only, with
    orientation detection enabled (see module docstring for why this
    is scoped here rather than changed on get_engine()). A distinct
    engine instance because PaddleOCR bakes preprocessing config in at
    construction time -- there's no way to toggle orientation
    detection per-call on a shared engine.
    """

    global _parcelmap_engine

    if _parcelmap_engine is None:
        _parcelmap_engine = PaddleOCR(
            use_doc_orientation_classify=True,
            use_doc_unwarping=False,
            use_textline_orientation=True,
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            cpu_threads=8,
        )

    return _parcelmap_engine


_orientation_model = None
_orientation_lock = threading.Lock()

# Measured on the 23 hand-labeled plats in tests/regression: every
# truly rotated plat scored 0.84-0.92, every wrong guess on an upright
# plat scored <= 0.69. Acting only above 0.8 caught 5/5 rotated plats
# with 0/18 false rotations.
ORIENTATION_MIN_CONFIDENCE = 0.8


def upright(image: Image.Image) -> Image.Image:
    """
    Returns `image` rotated upright if PaddleOCR's page-orientation
    classifier is confident it's turned 90/180/270 degrees, otherwise
    `image` unchanged. The classifier's label is the counter-clockwise
    rotation that fixes the page (verified: a crop labeled "270" reads
    "0" after PIL rotate(270)).
    """

    global _orientation_model
    from paddleocr import DocImgOrientationClassification

    with _orientation_lock:
        if _orientation_model is None:
            _orientation_model = DocImgOrientationClassification(
                model_name="PP-LCNet_x1_0_doc_ori"
            )
        result = list(_orientation_model.predict(np.array(image.convert("RGB"))))[0]

    angle = int(result["label_names"][0])
    if angle == 0 or float(result["scores"][0]) < ORIENTATION_MIN_CONFIDENCE:
        return image
    return image.rotate(angle, expand=True)


# PaddleOCR silently downscales any image over 4000px on a side before
# reading it (confirmed via a real diagnostic: a 4637x3615 crop, auto-
# resized to fit 4000px, recovered only 2 usable bearing/distance
# values from the whole page; the SAME crop split into 2000-ish px
# tiles first -- so no downscale ever happens -- recovered 18, with no
# increase in total detected line count, meaning the downscale wasn't
# missing text outright, it was blurring away the FINE detail on small
# dimension numbers specifically while larger labels stayed readable
# either way). Large real survey sheets are common enough (a full
# section map, a multi-parcel exhibit) that this isn't an edge case.
_MAX_OCR_TILE_PX = 2500
_OCR_TILE_OVERLAP_PX = 150


def _tile_for_ocr(image: Image.Image) -> list[tuple[Image.Image, int, int]]:
    """
    Grid-splits an image into ~_MAX_OCR_TILE_PX pieces (well under
    PaddleOCR's 4000px auto-downscale threshold) if needed, else
    returns the image as a single "tile" at (0, 0). Returns
    (tile_image, x_offset, y_offset) so callers can translate each
    tile's detected bounding boxes back into the original crop's
    coordinate space. Overlap on internal edges only, same rationale
    as vision.py's _tile_image: a dimension label sitting on a seam
    would otherwise be split and lost.
    """

    width, height = image.size
    if width <= _MAX_OCR_TILE_PX and height <= _MAX_OCR_TILE_PX:
        return [(image, 0, 0)]

    cols = max(1, -(-width // _MAX_OCR_TILE_PX))  # ceil division
    rows = max(1, -(-height // _MAX_OCR_TILE_PX))
    tile_w, tile_h = width / cols, height / rows

    tiles = []
    for row in range(rows):
        for col in range(cols):
            x0, y0 = int(col * tile_w), int(row * tile_h)
            x1 = width if col == cols - 1 else int((col + 1) * tile_w)
            y1 = height if row == rows - 1 else int((row + 1) * tile_h)

            ox0 = max(0, x0 - _OCR_TILE_OVERLAP_PX)
            oy0 = max(0, y0 - _OCR_TILE_OVERLAP_PX)
            ox1 = min(width, x1 + _OCR_TILE_OVERLAP_PX)
            oy1 = min(height, y1 + _OCR_TILE_OVERLAP_PX)
            tiles.append((image.crop((ox0, oy0, ox1, oy1)), ox0, oy0))

    return tiles


def _dedupe_overlap_lines(lines: list[OCRLine]) -> list[OCRLine]:
    """
    Tile overlap can detect the same real text twice (once per
    overlapping tile). Drops a later duplicate whose bbox center sits
    within a few px of an earlier line's -- generous enough to catch
    the same detection re-run through OCR (which is not pixel-exact
    even on identical content), tight enough not to merge two
    genuinely different nearby labels.
    """

    kept: list[OCRLine] = []
    for line in lines:
        cx = (line.bbox[0] + line.bbox[2]) / 2
        cy = (line.bbox[1] + line.bbox[3]) / 2
        if any(
            abs(cx - (k.bbox[0] + k.bbox[2]) / 2) < 20 and abs(cy - (k.bbox[1] + k.bbox[3]) / 2) < 20
            for k in kept
        ):
            continue
        kept.append(line)
    return kept


# A bearing with NO trailing distance -- the whole (stripped) line ends
# right after the E/W letter. Confirmed on a real document: the
# bearing and its distance are sometimes two SEPARATE detected lines
# (not one bearing+distance string like the common case), so these
# need to be paired back together by position, not found as one match.
_BEARING_ONLY_RE = re.compile(
    r"^[NSns]\s*\d+(?:\.\d+)?\s*[°*ov°]?\s*"
    r"(?:\d+(?:\.\d+)?\s*[\'′‘’]?\s*)?"
    r"(?:\d+(?:\.\d+)?\s*[\"″“”]?\s*)?"
    r"[EWew]\s*$"
)
# A bare distance -- a number, optionally with a foot mark, and
# nothing that looks like a bearing letter anywhere in the line.
_BARE_DISTANCE_RE = re.compile(r"^\d+(?:\.\d+)?\s*[\'′‘’]?\s*$")
_MAX_PAIRING_DISTANCE_PX = 250


def _pair_split_bearings(lines: list[OCRLine]) -> list[OCRLine]:
    """
    When a bearing and its distance were detected as two separate
    lines (confirmed real case, not garbling -- both values individually
    correct, just not on the same text line), merge the nearest
    unclaimed bare-distance line into each bearing-only line by
    bounding-box-center proximity, capped at _MAX_PAIRING_DISTANCE_PX
    so an unrelated distant number never gets wrongly attached.
    Bearing+distance lines that already arrived combined (the more
    common case) are untouched.
    """

    bearing_only = [l for l in lines if _BEARING_ONLY_RE.match(l.text.strip())]
    bare_distances = [l for l in lines if _BARE_DISTANCE_RE.match(l.text.strip())]
    if not bearing_only or not bare_distances:
        return lines

    claimed: set[int] = set()
    merged_ids: set[int] = set()
    extra: list[OCRLine] = []

    def center(l: OCRLine) -> tuple[float, float]:
        return (l.bbox[0] + l.bbox[2]) / 2, (l.bbox[1] + l.bbox[3]) / 2

    for bearing in bearing_only:
        bx, by = center(bearing)
        best_idx, best_dist = None, _MAX_PAIRING_DISTANCE_PX
        for i, dist_line in enumerate(bare_distances):
            if id(dist_line) in claimed:
                continue
            dx, dy = center(dist_line)
            d = ((bx - dx) ** 2 + (by - dy) ** 2) ** 0.5
            if d < best_dist:
                best_idx, best_dist = i, d
        if best_idx is not None:
            match = bare_distances[best_idx]
            claimed.add(id(match))
            merged_ids.add(id(bearing))
            merged_ids.add(id(match))
            x1 = min(bearing.bbox[0], match.bbox[0])
            y1 = min(bearing.bbox[1], match.bbox[1])
            x2 = max(bearing.bbox[2], match.bbox[2])
            y2 = max(bearing.bbox[3], match.bbox[3])
            extra.append(
                OCRLine(
                    text=f"{bearing.text.strip()} {match.text.strip()}",
                    confidence=round(min(bearing.confidence, match.confidence), 3),
                    bbox=(x1, y1, x2, y2),
                )
            )

    kept = [l for l in lines if id(l) not in merged_ids]
    return kept + extra


def run_parcelmap_ocr(image: Image.Image) -> tuple[list[OCRLine], bool]:
    """
    OCR for a single ParcelMap crop, orientation-corrected, tiled if
    oversized (see _tile_for_ocr), with split bearing/distance pairs
    reunited (see _pair_split_bearings). Returns (lines, reliable) --
    `reliable` is False when the mean confidence falls below
    PARCELMAP_OCR_CONFIDENCE_FLOOR, the caller's signal to fall back to
    Gemini reading the raw image directly rather than trusting a
    garbled OCR pass (see module docstring: orientation correction
    fixes whole-page rotation but not every in-drawing angle, so this
    can still legitimately fail on some crops).
    """

    image = image.convert("RGB")
    tiles = _tile_for_ocr(image)

    lines: list[OCRLine] = []
    with _parcelmap_engine_lock:
        for tile_img, x_off, y_off in tiles:
            arr = np.array(tile_img)
            result = get_parcelmap_engine().predict(arr)
            for page_result in result:
                texts = page_result.get("rec_texts", [])
                scores = page_result.get("rec_scores", [])
                boxes = page_result.get("rec_boxes", [])
                for text, score, box in zip(texts, scores, boxes):
                    if not text.strip():
                        continue
                    x1, y1, x2, y2 = (float(v) for v in box)
                    lines.append(
                        OCRLine(
                            text=text,
                            confidence=round(float(score), 3),
                            bbox=(x1 + x_off, y1 + y_off, x2 + x_off, y2 + y_off),
                        )
                    )

    if len(tiles) > 1:
        lines = _dedupe_overlap_lines(lines)
    lines = _pair_split_bearings(lines)

    mean_confidence = sum(l.confidence for l in lines) / len(lines) if lines else 0.0
    reliable = mean_confidence >= PARCELMAP_OCR_CONFIDENCE_FLOOR
    return lines, reliable
