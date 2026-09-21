from app.services.pdf_inspector import inspect_pdf


PDF_PATH = "data/documents/1a038393-0a3f-4eb0-bc5f-ae06ef77b75f/original.pdf"


result = inspect_pdf(PDF_PATH)

print("\n=== PDF INSPECTION ===")
print(f"Filename: {result['filename']}")
print(f"Pages: {result['page_count']}")
print(f"Metadata: {result['metadata']}")

print("\n=== PAGE INVENTORY ===")

for page in result["pages"]:
    print(
        f"Page {page['page_number']:02d} | "
        f"{page['width']:.0f} x {page['height']:.0f} | "
        f"text={page['has_text']} | "
        f"text_length={page['text_length']} | "
        f"images={page['image_count']}"
    )