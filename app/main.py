import re

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core import auth
from app.routes.account import router as account_router
from app.routes.ask import router as ask_router
from app.routes.documents import router as documents_router
from app.services import doc_access

app = FastAPI(title="ROAM API")

_DOC_PATH = re.compile(r"^/documents/([0-9a-f-]{36})(/.*)?$")


@app.middleware("http")
async def document_access(request: Request, call_next):
    """
    One gate for every document route (see services/doc_access.py):
    - reading is open to whoever has the document's id (ids are unguessable; it is how samples are shared);
    - uploading needs a signed-in user;
    - changing a document needs the right to: its owner, anyone holding a playground copy, admins for the
      shared samples;
    - reprocessing (a full, paid pipeline run) needs a signed-in owner, never a playground copy.
    """

    path, method = request.url.path, request.method
    user = auth.current_user(request)
    request.state.user = user
    if method in ("GET", "HEAD", "OPTIONS") or not path.startswith("/documents"):
        m = _DOC_PATH.match(path)
        if m and method == "GET":
            doc_access.touch(m.group(1))
        return await call_next(request)
    if path == "/documents/upload":
        if not user:
            return JSONResponse({"detail": "Sign in with Google to upload your own documents."}, status_code=401)
        return await call_next(request)
    m = _DOC_PATH.match(path)
    if m:
        document_id, rest = m.group(1), m.group(2) or ""
        admin = auth.is_admin(user)
        if rest == "/reprocess" and (doc_access.access(document_id).get("sandbox_of") or not user):
            return JSONResponse({"detail": "Reprocessing runs the whole pipeline again: sign in and upload your own "
                                           "copy of the document to do that."}, status_code=403)
        if not doc_access.can_write(document_id, user, admin):
            if doc_access.is_sample(document_id):
                detail = "This is a shared sample: open it from the gallery to get your own copy to change."
            elif not user:
                detail = "Sign in with Google to change this document."
            else:
                detail = "This document belongs to another account."
            return JSONResponse({"detail": detail}, status_code=401 if not user else 403)
        doc_access.touch(document_id)
    return await call_next(request)


# Added after the gate so CORS headers also reach the gate's own refusals (the outermost middleware runs first).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # the session travels in the Authorization header, not a cookie
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(account_router)
app.include_router(ask_router)
app.include_router(documents_router)


@app.get("/")
def health():
    return {"status": "ROAM backend running"}


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "ROAM"}
