"""
Who may change a document, the sample gallery, and per-visitor playground copies of the samples.

Each document folder may hold `access.json`:
  {"owner": "<email>", "filename": "...", "created_at": ...}     -- uploaded by a signed-in user
  {"sandbox_of": "<sample id>", "created_at": ..., "touched_at": ...} -- a visitor's playground copy
Folders without it (documents from before sign-in existed) belong to the admins.

A playground copy is a new document id whose result.json is copied and whose other files (page images, the
PDF, crops) are HARD-LINKED to the sample's -- no extra disk -- so a visitor can move, confirm and ask the AI
anything on it while the sample itself never changes. Copies expire after SANDBOX_TTL_S without use.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

DOCUMENT_ROOT = Path("data/documents")
SAMPLES_FILE = Path("data/samples.json")
SANDBOX_TTL_S = 6 * 3600
MAX_SANDBOXES = 60


def _access_path(document_id: str) -> Path:
    return DOCUMENT_ROOT / document_id / "access.json"


def access(document_id: str) -> dict:
    path = _access_path(document_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_access(document_id: str, data: dict) -> None:
    path = _access_path(document_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def samples() -> list[dict]:
    try:
        return json.loads(SAMPLES_FILE.read_text(encoding="utf-8")).get("samples", [])
    except (OSError, json.JSONDecodeError):
        return []


def is_sample(document_id: str) -> bool:
    return any(s.get("id") == document_id for s in samples())


def can_write(document_id: str, user: dict | None, admin: bool) -> bool:
    """A sample: admins only. A playground copy: whoever holds its id. An upload: its owner (and admins)."""

    if is_sample(document_id):
        return admin
    meta = access(document_id)
    if meta.get("sandbox_of"):
        return True
    if meta.get("owner"):
        return admin or (user is not None and user.get("email") == meta["owner"])
    return admin


def touch(document_id: str) -> None:
    meta = access(document_id)
    if meta.get("sandbox_of") and time.time() - meta.get("touched_at", 0) > 60:
        meta["touched_at"] = time.time()
        write_access(document_id, meta)


def cleanup_sandboxes() -> int:
    """Deletes playground copies unused for SANDBOX_TTL_S. Returns how many are left."""

    left = 0
    if not DOCUMENT_ROOT.exists():
        return 0
    for folder in DOCUMENT_ROOT.iterdir():
        meta = access(folder.name) if folder.is_dir() else {}
        if not meta.get("sandbox_of"):
            continue
        if time.time() - meta.get("touched_at", meta.get("created_at", 0)) > SANDBOX_TTL_S:
            shutil.rmtree(folder, ignore_errors=True)
        else:
            left += 1
    return left


# A playground copy shares these with its sample instead of copying them: they never change, and
# copying ~20-40 MB of page images made opening a sample slow on storage without hard links.
SHARED_WITH_SAMPLE = ("pages", "page_thumbs", "review_crops", "original.pdf")


def doc_file(document_id: str, *parts: str) -> Path:
    """A document's file; for a playground copy, the sample's when the copy has no own version."""

    own = DOCUMENT_ROOT.joinpath(document_id, *parts)
    if own.exists():
        return own
    parent = access(document_id).get("sandbox_of")
    return DOCUMENT_ROOT.joinpath(parent, *parts) if parent else own


def _link_tree(src: Path, dst: Path) -> None:
    for item in src.iterdir():
        if item.name in ("result.json", "access.json", "report_cache", "thumb.jpg", *SHARED_WITH_SAMPLE) or item.name.endswith(".tmp"):
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(exist_ok=True)
            _link_tree(item, target)
        else:
            try:
                os.link(item, target)  # same file, no copy
            except OSError:
                shutil.copy2(item, target)  # a filesystem without hard links


def open_sandbox(sample_id: str) -> str:
    """A fresh playground copy of a sample; returns its document id."""

    if not is_sample(sample_id):
        raise KeyError(sample_id)
    src = DOCUMENT_ROOT / sample_id
    if not (src / "result.json").exists():
        raise FileNotFoundError(sample_id)
    if cleanup_sandboxes() >= MAX_SANDBOXES:
        raise RuntimeError("too many playground copies open right now -- try again in a little while")
    new_id = str(uuid.uuid4())
    dst = DOCUMENT_ROOT / new_id
    dst.mkdir(parents=True)
    _link_tree(src, dst)
    result = json.loads((src / "result.json").read_text(encoding="utf-8"))
    result["document_id"] = new_id
    result.pop("agent_proposals", None)
    (dst / "result.json").write_text(json.dumps(result), encoding="utf-8")
    now = time.time()
    write_access(new_id, {"sandbox_of": sample_id, "created_at": now, "touched_at": now})
    return new_id


def owned_documents(email: str) -> list[dict]:
    out = []
    if not DOCUMENT_ROOT.exists():
        return out
    for folder in DOCUMENT_ROOT.iterdir():
        if not folder.is_dir():
            continue
        meta = access(folder.name)
        if meta.get("owner") == email:
            out.append({"id": folder.name, "filename": meta.get("filename"), "created_at": meta.get("created_at")})
    return sorted(out, key=lambda d: d.get("created_at") or 0, reverse=True)
