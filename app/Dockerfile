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

EXPOSE 7860

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
