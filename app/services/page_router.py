from typing import Any


def route_page(
    inspection: dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """
    Decide which processing path a document page should take.

    This is the first deterministic routing layer of ROAM.

    It combines:
    - PDF-level structure from inspect_pdf()
    - visual characteristics from analyze_page()

    It does NOT perform OCR or Vision itself.
    It only decides what should happen next.
    """

    has_text = inspection["has_text"]
    image_count = inspection["image_count"]

    composition = analysis["composition"]
    brightness = analysis["brightness"]
    dark_pixel_ratio = analysis["dark_pixel_ratio"]
    edge_density = analysis["edge_density"]

    # ---------------------------------------------------------
    # 1. Native text + no image
    # ---------------------------------------------------------
    if has_text and image_count == 0:
        return {
            "route": "OCR_NOT_NEEDED",
            "reason": "Page already contains native PDF text.",
        }

    # ---------------------------------------------------------
    # 2. Obvious degraded page
    # ---------------------------------------------------------
    if composition == "DEGRADED":
        return {
            "route": "VISION",
            "reason": "Page is severely degraded and requires visual interpretation.",
        }

    # ---------------------------------------------------------
    # 3. Text + image
    # ---------------------------------------------------------
    if has_text and image_count > 0:
        return {
            "route": "OCR_AND_VISION",
            "reason": "Page contains both text and embedded images.",
        }

    # ---------------------------------------------------------
    # 4. Image-only but visually complex
    # ---------------------------------------------------------
    if image_count > 0 and composition == "VISUAL_COMPLEX":
        return {
            "route": "OCR_AND_VISION",
            "reason": "Image-only page has complex visual structure.",
        }

    # ---------------------------------------------------------
    # 5. Image-only page
    # ---------------------------------------------------------
    if image_count > 0:
        return {
            "route": "OCR",
            "reason": "Image-only page requires text extraction.",
        }

    # ---------------------------------------------------------
    # 6. Fallback
    # ---------------------------------------------------------
    return {
        "route": "REVIEW",
        "reason": "Page characteristics do not match a known routing rule.",
    }