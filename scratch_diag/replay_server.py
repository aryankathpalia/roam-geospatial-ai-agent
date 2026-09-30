"""
DIAGNOSTIC ONLY. Runs the real app but replaces the live Gemini call with the
candidates previously captured from real Gemini runs (file named by
$REPLAY_CANDIDATES), so calibrate() / confirm-boundary / PaddleOCR are all real.
Use when a live Gemini key isn't available. Does NOT sample Gemini variance.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.main import app  # noqa: E402
from app.services import gemini_edge_association as gea  # noqa: E402


def _replay(page_img, crop_box, polygon_crop_px):
    with open(os.environ["REPLAY_CANDIDATES"]) as f:
        return json.load(f)


gea.associate_edges = _replay
