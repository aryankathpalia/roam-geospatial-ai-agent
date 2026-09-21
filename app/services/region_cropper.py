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

from app.services.layout_detector import Detection

# Small margin so OCR/vision models get a little breathing room around
# the detected box instead of a hard crop against the text/drawing edge.
CROP_MARGIN_PX = 6


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
            x1 = max(0, detection.x - CROP_MARGIN_PX)
            y1 = max(0, detection.y - CROP_MARGIN_PX)
            x2 = min(page_width, detection.x + detection.width + CROP_MARGIN_PX)
            y2 = min(page_height, detection.y + detection.height + CROP_MARGIN_PX)

            crop_image = page_image.crop((x1, y1, x2, y2))

            crop = RegionCrop(
                roam_class=detection.roam_class,
                confidence=detection.confidence,
                needs_review=detection.needs_review,
                bbox=(detection.x, detection.y, detection.width, detection.height),
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
