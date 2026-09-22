# Build from the repository root:
#   docker build -f app/Dockerfile .
FROM python:3.12

WORKDIR /code

# opencv-python (pulled in transitively by doclayout_yolo) needs libGL
# at import time even though we never render anything graphical --
# "ImportError: libGL.so.1: cannot open shared object file" without it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Only the deployed ONNX weights -- not the full models/ dir (which also
# holds the 144MB PyTorch checkpoint used solely by the dev-only
# /documents/annotate endpoint, and other training artifacts not needed
# at serve time).
COPY models/roam_layout_v1/roam_layout_best.int8.onnx models/roam_layout_v1/roam_layout_best.int8.onnx

ENV PYTHONPATH=/code

# Bake PaddleOCR's mobile det/rec models into the image at build time.
# Without this, every fresh container (and on Modal, every parallel
# OCR container fanned out via .map()) has to fetch them from
# PaddleX's model hosters at request time -- slower, and paddlex's own
# pre-download connectivity check is flaky under Modal's networking
# ("No model hoster is available!" even though the download itself
# succeeds right after). PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK skips
# that flaky check as a second layer of defense.
ENV PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
RUN python -c "\
from PIL import Image; \
from app.services.ocr import run_page_ocr; \
run_page_ocr(Image.new('RGB', (64, 64), 'white'))"

EXPOSE 7860

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
