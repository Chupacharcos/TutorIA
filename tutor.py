"""
TutorIA — Motor de tutoría adaptativa con LangChain + Groq/LLaMA
"""
import os
import re
from langchain_groq import ChatGroq
from langchain_classic.memory import ConversationSummaryBufferMemory
from langchain_classic.chains import ConversationChain
from langchain_core.prompts import PromptTemplate

# ── Detector de frustración ───────────────────────────────────────────────────

FRUSTRATION_KEYWORDS = {
    "no entiendo", "no comprendo", "es muy difícil", "me rindo", "no sé",
    "me perdí", "no puedo", "imposible", "qué difícil", "no me sale",
    "no lo pillo", "no me entero", "estoy confundido", "estoy confundida",
    "no tengo ni idea", "ayuda", "socorro", "ugh", "grr", "🙁", "😩", "😤",
}

def _detect_frustration(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in FRUSTRATION_KEYWORDS)


# ── Extractor de keywords de concepto ──────────────────────────────────────────

_STOPWORDS = {"el", "la", "los", "las", "un", "una", "de", "que", "en", "es",
              "y", "a", "me", "se", "no", "qué", "cómo", "por", "para", "con",
              "si", "lo", "al", "del", "mi", "su", "son", "soy", "hay", "tengo"}

def _extract_keywords(text: str) -> set:
    words = re.findall(r'\b\w{4,}\b', text.lower())
    return {w for w in words if w not in _STOPWORDS}

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# ── Perfiles de alumno ────────────────────────────────────────────────────────

PROFILES = {
    "normal": {
        "description": "alumno estándar",
        "style": "Explica con claridad, usa ejemplos cotidianos, da ánimos.",
        "response_length": "Respuestas de 3-5 frases.",
    },
    "tdah": {
        "description": "alumno con TDAH",
        "style": "Respuestas muy cortas. Usa emojis. Divide en pasos numerados muy pequeños. Celebra cada logro con entusiasmo.",
        "response_length": "Máximo 3 frases por respuesta.",
    },
    "dislexia": {
        "description": "alumno con dislexia",
        "style": "Usa frases cortas y simples. Evita palabras difíciles. Ofrece recordatorios visuales (listas con •). Nunca corrijas la ortografía directamente.",
        "response_length": "Respuestas breves, con listas simples.",
    },
    "bajo_rendimiento": {
        "description": "alumno con bajo rendimiento general",
        "style": "Sé muy paciente. Empieza siempre desde lo más básico. Refuerza la confianza. Divide los conceptos en trozos muy pequeños.",
        "response_length": "Respuestas claras, paso a paso.",
    },
}

PROMPT_TEMPLATE = """Eres TutorIA, un profesor particular virtual especializado en ayudar a niños y jóvenes de {age} años ({curso}) con sus deberes y dudas escolares.

Perfil del alumno: {profile_description}
Estilo de enseñanza: {style}
{response_length}

Normas importantes:
- Nunca hagas el ejercicio completo: guía al alumno para que lo descubra él.
- Si el alumno falla 2 veces lo mismo, cambia el enfoque de explicación.
- Usa el nombre del alumno ({name}) para personalizar.
- Adapta el lenguaje a la edad: {age} años.
- Si detectas frustración, primero valida el sentimiento, luego redirige.

Conversación anterior resumida:
{history}

Alumno ({name}): {input}
TutorIA:"""

# ── Sesiones en memoria ───────────────────────────────────────────────────────

_sessions: dict[str, dict] = {}


def get_or_create_session(session_id: str, profile: dict) -> dict:
    if session_id not in _sessions:
        llm = ChatGroq(
            api_key=GROQ_API_KEY,
            model="llama-3.1-8b-instant",
            temperature=0.7,
            max_tokens=512,
        )
        memory = ConversationSummaryBufferMemory(
            llm=llm,
            max_token_limit=800,
            return_messages=False,
        )
        prof = PROFILES.get(profile.get("type", "normal"), PROFILES["normal"])
        prompt = PromptTemplate(
            input_variables=["history", "input"],
            template=PROMPT_TEMPLATE.format(
                age=profile.get("age", 10),
                curso=profile.get("curso", "5º de Primaria"),
                name=profile.get("name", "alumno"),
                profile_description=prof["description"],
                style=prof["style"],
                response_length=prof["response_length"],
                history="{history}",
                input="{input}",
            ),
        )
        chain = ConversationChain(llm=llm, memory=memory, prompt=prompt, verbose=False)
        _sessions[session_id] = {
            "chain": chain,
            "memory": memory,
            "profile": profile,
            "error_tracker": {},     # keyword → consecutive_error_count
            "turn_count": 0,
            "frustration_streak": 0, # consecutive turns with frustration
            "topics_covered": [],    # list of keyword sets per turn
        }
    return _sessions[session_id]


def chat(session_id: str, message: str, profile: dict) -> dict:
    session = get_or_create_session(session_id, profile)
    session["turn_count"] += 1

    # ── Detector de frustración ────────────────────────────────────────────
    is_frustrated = _detect_frustration(message)
    if is_frustrated:
        session["frustration_streak"] += 1
    else:
        session["frustration_streak"] = 0

    frustration_active = session["frustration_streak"] >= 2

    # ── Error tracker por concepto ─────────────────────────────────────────
    keywords = _extract_keywords(message)
    session["topics_covered"].append(keywords)
    error_topic = None

    if is_frustrated and len(session["topics_covered"]) >= 2:
        # Buscar keywords que se repiten en los últimos 2 turnos con frustración
        prev = session["topics_covered"][-2] if len(session["topics_covered"]) >= 2 else set()
        repeated = keywords & prev
        if repeated:
            topic = list(repeated)[0]
            session["error_tracker"][topic] = session["error_tracker"].get(topic, 0) + 1
            if session["error_tracker"][topic] >= 2:
                error_topic = topic

    # ── Modificar input al LLM si hay frustración o error repetido ────────
    enhanced = message
    if error_topic:
        enhanced = f"[El alumno lleva 2+ intentos fallidos con el concepto '{error_topic}'. Cambia la estrategia: usa un ejemplo concreto diferente, más sencillo.] {message}"
    elif frustration_active:
        enhanced = f"[El alumno muestra frustración persistente. Primero valida sus sentimientos con empatía antes de continuar.] {message}"

    response = session["chain"].invoke({"input": enhanced})
    reply = response["response"] if isinstance(response, dict) else str(response)

    return {
        "reply": reply,
        "turn": session["turn_count"],
        "session_id": session_id,
        "frustration_detected": frustration_active,
        "error_topic": error_topic,
    }


def get_session_summary(session_id: str) -> dict:
    if session_id not in _sessions:
        return {"summary": "No hay sesión activa.", "turns": 0}
    session = _sessions[session_id]
    memory = session["memory"]
    summary = getattr(memory, "moving_summary_buffer", "") or "Sesión reciente sin resumen generado."
    return {
        "summary": summary,
        "turns": session["turn_count"],
        "profile": session["profile"],
    }


def list_profiles() -> list:
    return [
        {"id": k, "name": v["description"], "style_hint": v["style"]}
        for k, v in PROFILES.items()
    ]
