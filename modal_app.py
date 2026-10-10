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

# Everything the app keeps on disk lives under data/ (relative to /code):
# uploaded documents and their results, the sample gallery, the session
# signing secret, map tile cache. A Volume keeps it across restarts and
# redeploys; the API container commits it every few seconds.
data_volume = modal.Volume.from_name("roam-data", create_if_missing=True)
DATA_DIR = "/code/data"

image = modal.Image.from_dockerfile(
    "app/Dockerfile",
    context_dir=".",
)

# OCR workers run on a GPU: the same image with Paddle's CUDA build in place of the CPU one.
# OCR is the expensive step (tens of seconds per dense page on CPU); everything else stays on CPU.
# Dense pages are split into horizontal bands (app/pipeline/page_ocr.py) and every band of the
# whole document is fanned out at once via .map() below, so no single page dominates.
gpu_image = image.run_commands(
    # PyTorch (pulled in by doclayout_yolo for the dev-only annotation tool) ships its own NCCL,
    # which clashes with Paddle's CUDA libraries; the OCR workers never use it.
    "pip uninstall -y paddlepaddle torch torchvision doclayout_yolo",
    "pip install --no-cache-dir paddlepaddle-gpu==3.2.2 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/",
).env({"ROAM_OCR_DEVICE": "gpu"})


@app.function(
    image=gpu_image,
    gpu="T4",
    cpu=1.0,
    # Bumped from 2048: measured floor with just the PaddleOCR engine
    # loaded is 1.13-1.27GB, leaving too little headroom at 2GB for a
    # dense band's actual working memory. 3GB matches the plan agreed
    # once a spend-capped card is on the account.
    memory=3072,
    timeout=180,
    # At most two GPUs at once (a T4 reads a band in well under a second, so the bands of one
    # document queue briefly instead of each starting its own GPU), and none left idling.
    max_containers=2,
    scaledown_window=60,
)
def ocr_band_remote(band_bytes: bytes, y_offset: float) -> list:
    from app.pipeline.page_ocr import ocr_band

    return ocr_band(band_bytes, y_offset)


async def _modal_ocr_dispatcher(jobs: list[tuple[bytes, float]]) -> list[list]:
    band_bytes_list = [job[0] for job in jobs]
    y_offset_list = [job[1] for job in jobs]

    try:
        results = []
        async for result in ocr_band_remote.map.aio(band_bytes_list, y_offset_list):
            results.append(result)
        return results
    except Exception as exc:
        # GPU workers unavailable (for example the workspace spend limit was reached):
        # read the bands on this container's CPU instead, slower but the upload still finishes.
        print(f"GPU OCR unavailable ({exc!r}); falling back to CPU")
        from app.pipeline.document_pipeline import _default_ocr_dispatcher

        return await _default_ocr_dispatcher(jobs)


@app.function(
    image=image,
    cpu=2.0,
    # Measured floor with both models loaded is ~1.13GB (see
    # models/roam_layout_v1/metrics_summary.json and this session's
    # profiling) -- 4GB gives real headroom without over-provisioning
    # credit usage.
    memory=4096,
    timeout=600,
    # Scale to zero when idle so the free monthly credit isn't burnt, but
    # stay up 20 minutes after the last request: a visitor waits for one
    # cold start, then the rest of the visit is fast.
    scaledown_window=1200,
    min_containers=0,
    # One API container: the documents' state lives on the Volume and
    # background jobs run inside this container, so a second copy would
    # see stale files. It serves many requests at once (below).
    max_containers=1,
    volumes={DATA_DIR: data_volume},
    # GEMINI_API_KEY (vision escalation) -- created via:
    #   modal secret create roam-secrets --from-dotenv .env
    # pydantic-settings reads real process env vars regardless of
    # whether a local .env file is present (it isn't, in the deployed
    # image -- .dockerignore excludes it), so no code change needed
    # beyond this.
    secrets=[modal.Secret.from_name("roam-secrets")],
)
@modal.concurrent(max_inputs=32)
@modal.asgi_app()
def fastapi_app():
    from app.main import app as web_app
    from app.pipeline import document_pipeline

    # Swap in the parallel fan-out dispatcher for this deployment --
    # process_document() reads this module attribute fresh on every
    # call, so setting it once here at container startup is enough.
    document_pipeline.DEFAULT_OCR_DISPATCHER = _modal_ocr_dispatcher

    # Persist data/ to the Volume every few seconds (uploads, results,
    # confirmations, playground copies).
    import threading
    import time

    def _commit_loop():
        while True:
            time.sleep(5)
            try:
                data_volume.commit()
            except Exception:
                pass

    threading.Thread(target=_commit_loop, daemon=True).start()

    return web_app
