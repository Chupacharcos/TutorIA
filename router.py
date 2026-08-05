import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

import knowledge_base as kb
from tutor import chat, get_session_summary, list_profiles

router = APIRouter()


class ProfileIn(BaseModel):
    name: str = "Alumno"
    age: int = 10
    curso: str = "5º de Primaria"
    type: str = "normal"   # normal | tdah | dislexia | bajo_rendimiento


class ChatIn(BaseModel):
    session_id: Optional[str] = None
    message: str
    profile: ProfileIn = ProfileIn()
    # Identifica la organización/curso (ver knowledge_base.py). En un launch
    # LTI (lti.py) se rellena solo con el context_id de Moodle; en uso directo
    # por API lo indica el integrador. None = comportamiento público de siempre.
    namespace: Optional[str] = None


@router.post("/chat")
def tutoria_chat(body: ChatIn):
    session_id = body.session_id or str(uuid.uuid4())
    result = chat(session_id, body.message, body.profile.model_dump(), namespace=body.namespace)
    return result


@router.get("/session/{session_id}/summary")
def session_summary(session_id: str):
    return get_session_summary(session_id)


@router.get("/profiles")
def profiles():
    return list_profiles()


@router.post("/session/new")
def new_session(profile: ProfileIn = ProfileIn()):
    session_id = str(uuid.uuid4())
    return {"session_id": session_id, "profile": profile.model_dump()}


# ── Base de conocimiento multi-tenant (Integración — Inserver 2026-07-28) ────
# MVP de autenticación: un secreto compartido por despliegue (KB_ADMIN_KEY en
# .env), no claves por-organización. Suficiente para un despliegue propio
# self-hosted (que es el caso de uso real: cada organización despliega SU
# instancia); NO se ofrece como SaaS multi-cliente con aislamiento de claves
# — quien necesite eso debe poner su propio proxy de auth delante.
_api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def _require_admin(key: str = Depends(_api_key_header)):
    expected = os.getenv("KB_ADMIN_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="KB_ADMIN_KEY no configurada en el servidor — gestión de documentos desactivada.")
    if key != expected:
        raise HTTPException(status_code=401, detail="X-Admin-Key inválida o ausente.")
    return True


@router.post("/kb/{namespace}/upload", dependencies=[Depends(_require_admin)])
async def kb_upload(namespace: str, file: UploadFile = File(...), source_name: Optional[str] = Form(None)):
    """Indexa un documento (.pdf/.txt/.md) en la base de conocimiento del
    namespace (organización/curso). Requiere X-Admin-Key."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in kb.ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Tipo no soportado: {suffix}. Admitidos: {sorted(kb.ALLOWED_SUFFIXES)}")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    try:
        result = kb.ingest_document(namespace, tmp_path, source_name or file.filename)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return {"namespace": namespace, **result}


@router.get("/kb/{namespace}/sources")
def kb_list_sources(namespace: str):
    return {"namespace": namespace, "sources": kb.list_sources(namespace)}


@router.delete("/kb/{namespace}/sources/{source_id}", dependencies=[Depends(_require_admin)])
def kb_delete_source(namespace: str, source_id: str):
    ok = kb.delete_source(namespace, source_id)
    if not ok:
        raise HTTPException(status_code=404, detail="source_id no encontrado en ese namespace")
    return {"deleted": True, "namespace": namespace, "source_id": source_id}
