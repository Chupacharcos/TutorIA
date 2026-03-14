from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from router import router

app = FastAPI(title="TutorIA", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://adrianmoreno-dev.com", "http://127.0.0.1", "http://localhost"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/tutoria")


@app.get("/health")
def health():
    return {"status": "ok", "service": "tutoria", "version": "1.0.0"}
