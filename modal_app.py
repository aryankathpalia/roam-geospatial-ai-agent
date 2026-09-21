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

    return web_app
