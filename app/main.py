from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes.ask import router as ask_router
from app.routes.documents import router as documents_router

app = FastAPI(title="ROAM API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "*",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ask_router)
app.include_router(documents_router)

@app.get("/")
def health():
    return {"status": "ROAM backend running"}


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "ROAM"}
