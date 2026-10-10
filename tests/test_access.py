"""Sign-in tokens, the access gate, and playground copies of samples."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from app.core import auth
from app.main import app
from app.services import doc_access


@pytest.fixture()
def world(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    sample = root / "11111111-1111-1111-1111-111111111111"
    (sample / "pages").mkdir(parents=True)
    (sample / "pages" / "page_001.png").write_bytes(b"png")
    (sample / "result.json").write_text(json.dumps({"document_id": sample.name, "pages": []}))
    samples = tmp_path / "samples.json"
    samples.write_text(json.dumps({"samples": [{"id": sample.name, "title": "S", "page": 1}]}))
    monkeypatch.setattr(doc_access, "DOCUMENT_ROOT", root)
    monkeypatch.setattr(doc_access, "SAMPLES_FILE", samples)
    monkeypatch.setattr(auth, "_SECRET_FILE", tmp_path / ".secret")
    return sample


def test_tokens_round_trip_and_reject_tampering(world):
    tok = auth.issue_token({"email": "a@x.com", "name": "A"})
    assert auth.read_token(tok)["email"] == "a@x.com"
    payload, sig = tok.split(".")
    assert auth.read_token(payload + "." + sig[:-2] + "AA") is None
    assert auth.read_token("garbage") is None


def test_gate_and_playground_copy(world):
    c = TestClient(app)
    sid = world.name
    assert c.post(f"/documents/{sid}/move-parcels", json={}).status_code == 401  # guests never change a sample
    new = c.post(f"/samples/{sid}/open").json()["document_id"]
    copy = doc_access.DOCUMENT_ROOT / new
    assert json.loads((copy / "result.json").read_text())["document_id"] == new
    assert not (copy / "pages").exists()  # page images are shared with the sample, not copied
    assert doc_access.doc_file(new, "pages", "page_001.png").read_bytes() == b"png"
    assert doc_access.can_write(new, None, False) and not doc_access.can_write(sid, None, False)
    assert c.post(f"/documents/{new}/reprocess").status_code == 403
    assert c.post("/documents/upload", files={"file": ("a.pdf", b"%PDF", "application/pdf")}).status_code == 401
    # expiry
    meta = doc_access.access(new)
    meta["touched_at"] = meta["created_at"] = 0
    doc_access.write_access(new, meta)
    assert doc_access.cleanup_sandboxes() == 0 and not copy.exists()


def test_owners_and_admins(world, monkeypatch):
    doc = doc_access.DOCUMENT_ROOT / "22222222-2222-2222-2222-222222222222"
    doc.mkdir()
    doc_access.write_access(doc.name, {"owner": "a@x.com", "created_at": 1})
    assert doc_access.can_write(doc.name, {"email": "a@x.com"}, False)
    assert not doc_access.can_write(doc.name, {"email": "b@x.com"}, False)
    assert doc_access.can_write(doc.name, {"email": "b@x.com"}, True)
    assert doc_access.can_write(world.name, {"email": "b@x.com"}, True)  # admins curate the samples
    assert [d["id"] for d in doc_access.owned_documents("a@x.com")] == [doc.name]
