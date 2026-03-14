from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import uuid
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


@router.post("/chat")
def tutoria_chat(body: ChatIn):
    session_id = body.session_id or str(uuid.uuid4())
    result = chat(session_id, body.message, body.profile.model_dump())
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
