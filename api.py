from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from router import router
from lti import router as lti_router

app = FastAPI(title="TutorIA", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://adrianmoreno-dev.com", "http://127.0.0.1", "http://localhost"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/tutoria")
# LTI 1.3 (Moodle External Tool) — ver lti.py y README sección Integración
app.include_router(lti_router, prefix="/tutoria")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "tutoria",
        "version": "2.0.0",
        "features": ["adaptive_tutoring", "rag_multi_tenant", "lti_1p3"],
    }
