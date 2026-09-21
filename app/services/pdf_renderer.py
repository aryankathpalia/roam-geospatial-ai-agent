from pathlib import Path

import pymupdf


def render_page(
    pdf_path: str,
    page_number: int,
    output_path: str,
    dpi: int = 200,
) -> str:
    """
    Render one PDF page into a PNG image.

    page_number is 1-based, so page_number=1 means the first page.
    """

    pdf = pymupdf.open(pdf_path)

    try:
        if page_number < 1 or page_number > len(pdf):
            raise ValueError(
                f"Page number must be between 1 and {len(pdf)}."
            )

        page = pdf[page_number - 1]

        scale = dpi / 72
        matrix = pymupdf.Matrix(scale, scale)

        pixmap = page.get_pixmap(matrix=matrix, alpha=False)

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        pixmap.save(str(output))

        return str(output)

    finally:
        pdf.close()