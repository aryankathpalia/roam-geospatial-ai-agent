from pathlib import Path
from typing import Any

import pymupdf


def inspect_pdf(pdf_path: str) -> dict[str, Any]:
    """
    Inspect the structural properties of a PDF.

    This function does not perform OCR.
    It only examines the PDF's existing structure.
    """

    path = Path(pdf_path)

    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    document = pymupdf.open(pdf_path)

    pages = []

    for page_number, page in enumerate(document, start=1):
        text = page.get_text("text")
        images = page.get_images(full=True)

        pages.append(
            {
                "page_number": page_number,
                "width": page.rect.width,
                "height": page.rect.height,
                "has_text": bool(text.strip()),
                "text_length": len(text.strip()),
                "image_count": len(images),
            }
        )

    result = {
        "filename": path.name,
        "page_count": len(document),
        "metadata": document.metadata,
        "pages": pages,
    }

    document.close()

    return result