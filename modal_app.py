"""
Modal deployment entrypoint for the ROAM FastAPI backend.

Builds the EXACT same image as app/Dockerfile (no duplicate build
logic) and exposes it as a scale-to-zero ASGI app. The FastAPI app
itself (app/main.py) is unchanged -- this is only glue between our
existing Docker image and Modal's serverless runtime.

Deploy with:
    modal deploy modal_app.py
"""

import modal

app = modal.App("roam-backend")

image = modal.Image.from_dockerfile(
    "app/Dockerfile",
    context_dir=".",
)

# GPU was ruled out: Modal requires a payment method on file to use ANY
# GPU function, even within the free monthly credit -- confirmed via a
# real deploy attempt ("Please add a payment method to use T4 GPU
# functions"). That violates the no-card requirement this deployment
# was chosen for, so this stays CPU-only.
#
# The real fix for per-document latency: OCR is the expensive step
# (~15-25s/page), detection is cheap (~1-2s/page). Rather than one
# container OCR'ing every page in sequence, each OCR-eligible page is
# fanned out to its own container via .map() below -- N pages in
# parallel containers costs roughly the slowest page's time, not the
# sum of all of them. This is the same principle production OCR
# services (Textract, etc.) use to hit ~200 pages/2min: parallelism
# across workers, not a faster single-threaded engine.


@app.function(
    image=image,
    cpu=1.0,
    memory=2048,
    timeout=180,
)
def ocr_page_remote(page_bytes: bytes, regions: list[dict]) -> list[dict]:
    from app.pipeline.page_ocr import ocr_and_match_page

    return ocr_and_match_page(page_bytes, regions)


async def _modal_ocr_dispatcher(jobs: list[tuple[bytes, list[dict]]]) -> list[list[dict]]:
    page_bytes_list = [job[0] for job in jobs]
    regions_list = [job[1] for job in jobs]

    results = []
    async for result in ocr_page_remote.map.aio(page_bytes_list, regions_list):
        results.append(result)
    return results


@app.function(
    image=image,
    cpu=2.0,
    # Measured floor with both models loaded is ~1.13GB (see
    # models/roam_layout_v1/metrics_summary.json and this session's
    # profiling) -- 4GB gives real headroom without over-provisioning
    # credit usage.
    memory=4096,
    timeout=600,
    # Scale to zero shortly after the last request so idle time never
    # burns the free monthly credit; a cold start costs ~5-10s.
    scaledown_window=60,
    min_containers=0,
)
@modal.asgi_app()
def fastapi_app():
    from app.main import app as web_app
    from app.pipeline import document_pipeline

    # Swap in the parallel fan-out dispatcher for this deployment --
    # process_document() reads this module attribute fresh on every
    # call, so setting it once here at container startup is enough.
    document_pipeline.DEFAULT_OCR_DISPATCHER = _modal_ocr_dispatcher

    return web_app
