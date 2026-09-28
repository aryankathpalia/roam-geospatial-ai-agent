"""
Crops detected layout regions out of a rendered page, ready to hand to a
per-class downstream extractor (OCR, table parser, vision model, ...).

Crops are in-memory PIL Images by default -- nothing is written to disk
unless the caller explicitly asks for it (e.g. to save a low-confidence
detection for human review). A multi-hundred-page document should not
leave hundreds of stray crop files behind just from running detection.
"""

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app.services.layout_detector_onnx import Detection

# Small margin so OCR/vision models get a little breathing room around
# the detected box instead of a hard crop against the text/drawing edge.
CROP_MARGIN_PX = 6

# ParcelMap boxes need a much bigger margin than other classes: the
# layout model's box is a bounding estimate, not a guaranteed-tight
# fit, and for every other class a few missed pixels cost nothing --
# for ParcelMap a miss can crop off the actual boundary line or a
# corner coordinate, corrupting the geometry read from it. Confirmed
# on a real document where the model's box fell ~120px (~7.5% of the
# box's own height) short of the drawing's real bottom edge, visibly
# truncating a parcel's boundary in both the vision-facing crop and
# the review UI. Proportional (not a fixed pixel count) so it scales
# with how large the drawing itself is.
PARCELMAP_CROP_MARGIN_FRAC = 0.08
PARCELMAP_CROP_MARGIN_MIN_PX = 40


@dataclass
class RegionCrop:
    roam_class: str
    confidence: float
    needs_review: bool
    bbox: tuple[float, float, float, float]  # x, y, width, height (original page coords)
    image: Image.Image
    saved_path: str | None = None


def extract_region_crops(
    page_image_path: str,
    detections: list[Detection],
    save_dir: str | None = None,
    save_only_needs_review: bool = True,
) -> list[RegionCrop]:
    """
    Crop every detected region out of a rendered page image.

    By default this returns in-memory crops only. Pass `save_dir` to also
    persist crops to disk -- e.g. for human review or debugging. With
    `save_only_needs_review=True` (default), only detections flagged
    needs_review are written to disk, keeping the on-disk footprint small;
    set it False to persist every crop.
    """

    with Image.open(page_image_path) as page_image:
        page_image = page_image.convert("RGB")
        page_width, page_height = page_image.size

        crops: list[RegionCrop] = []

        for i, detection in enumerate(detections):
            if detection.roam_class == "ParcelMap":
                margin_x = max(PARCELMAP_CROP_MARGIN_MIN_PX, detection.width * PARCELMAP_CROP_MARGIN_FRAC)
                margin_y = max(PARCELMAP_CROP_MARGIN_MIN_PX, detection.height * PARCELMAP_CROP_MARGIN_FRAC)
            else:
                margin_x = margin_y = CROP_MARGIN_PX

            x1 = max(0, detection.x - margin_x)
            y1 = max(0, detection.y - margin_y)
            x2 = min(page_width, detection.x + detection.width + margin_x)
            y2 = min(page_height, detection.y + detection.height + margin_y)

            crop_image = page_image.crop((x1, y1, x2, y2))

            # bbox reflects the ACTUAL padded crop, not the raw detection
            # box -- this is what every downstream consumer (recompute,
            # the review UI's own re-crop endpoint, boundary-review)
            # keys off, so it needs to match what vision itself read
            # from, not a narrower box that silently disagrees with it.
            crop = RegionCrop(
                roam_class=detection.roam_class,
                confidence=detection.confidence,
                needs_review=detection.needs_review,
                bbox=(x1, y1, x2 - x1, y2 - y1),
                image=crop_image,
            )

            if save_dir is not None and (detection.needs_review or not save_only_needs_review):
                out_dir = Path(save_dir)
                out_dir.mkdir(parents=True, exist_ok=True)
                page_stem = Path(page_image_path).stem
                out_path = out_dir / f"{page_stem}_{i:02d}_{detection.roam_class}.png"
                crop_image.save(out_path)
                crop.saved_path = str(out_path)

            crops.append(crop)

    return crops
