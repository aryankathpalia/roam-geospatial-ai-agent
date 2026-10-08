"""Sign-in, the sample gallery and playground copies, and the signed-in user's documents."""

from __future__ import annotations

import io
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.core import auth
from app.core.config import settings
from app.services import doc_access

router = APIRouter(tags=["account"])


class GoogleSignIn(BaseModel):
    credential: str


@router.get("/auth/config")
def auth_config():
    """What the frontend needs to show the Google button (the client id is public by design)."""

    return {"google_client_id": settings.GOOGLE_CLIENT_ID or None}


@router.post("/auth/google")
def sign_in_with_google(body: GoogleSignIn):
    try:
        user = auth.verify_google(body.credential)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=f"Google sign-in failed: {exc}")
    return {"token": auth.issue_token(user), "user": {**user, "admin": auth.is_admin(user)}}


@router.get("/auth/me")
def me(request: Request):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return {k: user.get(k) for k in ("email", "name", "picture")} | {"admin": auth.is_admin(user)}


@router.get("/me/documents")
def my_documents(request: Request):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return {"documents": doc_access.owned_documents(user["email"])}


@router.get("/samples")
def list_samples():
    return {"samples": [{**s, "thumbnail": f"/samples/{s['id']}/thumb.jpg"} for s in doc_access.samples()
                        if (doc_access.DOCUMENT_ROOT / s["id"] / "result.json").exists()]}


@router.get("/samples/{sample_id}/thumb.jpg")
def sample_thumbnail(sample_id: str):
    """The sample's parcel-map page, shrunk (made once, cached)."""

    sample = next((s for s in doc_access.samples() if s.get("id") == sample_id), None)
    if sample is None:
        raise HTTPException(status_code=404, detail="Not a sample")
    folder = doc_access.DOCUMENT_ROOT / sample_id
    thumb = folder / "thumb.jpg"
    if not thumb.exists():
        from PIL import Image

        page = sample.get("page")
        if not page:
            result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
            page = next((p["page_number"] for p in result.get("pages", []) for r in p.get("regions", [])
                         for pa in r.get("parcels") or [] if pa.get("human_confirmed")), 1)
        src = folder / "pages" / f"page_{int(page):03d}.png"
        if not src.exists():
            raise HTTPException(status_code=404, detail="No page image")
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((640, 640))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
        thumb.write_bytes(buf.getvalue())
    return Response(content=thumb.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.post("/samples/{sample_id}/open")
def open_sample(sample_id: str):
    """A private playground copy of a sample for this visitor: change anything, nothing is kept."""

    try:
        new_id = doc_access.open_sandbox(sample_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Not a sample")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sample data missing on this server")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"document_id": new_id, "sandbox_of": sample_id}
