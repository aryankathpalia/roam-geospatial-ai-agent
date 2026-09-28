"""
In-memory pipeline progress tracker, polled by the review UI while a
document processes. Process-local and not persisted -- fine for a
single-server dev/demo deployment (the same scope result.json
persistence already assumes); a multi-worker deployment would need
this backed by something shared (Redis, a DB row) instead.

Stages are fixed and ordered (STAGES below) so the frontend can render
a checklist that fills in top-to-bottom, rather than free-form status
text. `detail` is a short human string filled in with real counts as
they become known ("29 pages" -> "29 pages, 3 ParcelMap regions") --
the frontend shows a generic label with placeholder dashes until this
arrives, then replaces the dashes with it.
"""

import time

STAGES = [
    "rendering",
    "layout_detection",
    "ocr",
    "georeferencing",
    "vision_extraction",
    "done",
]

_progress: dict[str, dict] = {}


def start(document_id: str) -> None:
    _progress[document_id] = {
        "stage": STAGES[0],
        "detail": None,
        "error": None,
        "history": [],
        "updated_at": time.time(),
    }


def update(document_id: str, stage: str, detail: str | None = None) -> None:
    """
    Sets the current stage/detail, AND appends to `history` -- the
    review UI shows each completed step's own detail text ("Rendering
    -- 29 pages") even after later steps have started, not just a
    checkmark with the text gone, so the history list (not just the
    latest stage) is what it actually reads to render each row.
    """

    if stage not in STAGES:
        raise ValueError(f"Unknown pipeline stage: {stage}")
    entry = _progress.setdefault(document_id, {"error": None, "history": []})
    entry["stage"] = stage
    entry["detail"] = detail
    entry.setdefault("history", []).append({"stage": stage, "detail": detail})
    entry["updated_at"] = time.time()


def fail(document_id: str, error: str) -> None:
    entry = _progress.setdefault(document_id, {"stage": "rendering", "detail": None})
    entry["error"] = error
    entry["updated_at"] = time.time()


def get(document_id: str) -> dict | None:
    return _progress.get(document_id)


def clear(document_id: str) -> None:
    _progress.pop(document_id, None)
