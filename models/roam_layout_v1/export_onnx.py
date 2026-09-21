"""
Exports roam_layout_best.pt to ONNX, then to an INT8 dynamically
quantized ONNX model for CPU deployment.

INT8 was chosen over shrinking the input image (1024px -> 640px) because
downsizing measurably hurt accuracy (Seal confidence 0.92 -> 0.28, lost
Table/Picture detections). INT8 at full 1024px resolution preserved
detections almost exactly versus FP32 across every test page, while
cutting CPU inference cost.

torch 2.x defaults to its newer "dynamo" ONNX exporter, which produced
an invalid graph for this model (an opset-18-only Split attribute
leaking into an opset-17 graph). Forcing the legacy exporter (dynamo=False)
avoids that.

Run from the repo root: python models/roam_layout_v1/export_onnx.py
"""

import torch

_original_torch_load = torch.load


def _torch_load_weights_only_false(*args, **kwargs):
    kwargs.setdefault("weights_only", False)
    return _original_torch_load(*args, **kwargs)


torch.load = _torch_load_weights_only_false

_original_onnx_export = torch.onnx.export


def _onnx_export_legacy(*args, **kwargs):
    kwargs.setdefault("dynamo", False)
    return _original_onnx_export(*args, **kwargs)


torch.onnx.export = _onnx_export_legacy

from doclayout_yolo import YOLOv10  # noqa: E402
from onnxruntime.quantization import QuantType, quantize_dynamic  # noqa: E402

WEIGHTS = "models/roam_layout_v1/roam_layout_best.pt"
FP32_ONNX = "models/roam_layout_v1/roam_layout_best.onnx"
INT8_ONNX = "models/roam_layout_v1/roam_layout_best.int8.onnx"

if __name__ == "__main__":
    model = YOLOv10(WEIGHTS)
    model.export(format="onnx", imgsz=1024, opset=17, simplify=True)
    print(f"FP32 ONNX exported to {FP32_ONNX}")

    quantize_dynamic(
        model_input=FP32_ONNX,
        model_output=INT8_ONNX,
        weight_type=QuantType.QUInt8,
    )
    print(f"INT8 ONNX exported to {INT8_ONNX}")
