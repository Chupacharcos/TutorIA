"""
Integración LTI 1.3 con Moodle (Inserver, 2026-07-28).

Responde directamente a la pregunta de integración: "¿plugin, LTI, servicios
web o API externa? ¿Cómo se identifican usuario, curso y contexto?".

Implementa el flujo estándar LTI 1.3 Core (1EdTech) usando `pylti1p3` (librería
de referencia del spec, no hecha a medida) sobre FastAPI:

  1. GET/POST /tutoria/lti/login   — OIDC third-party initiated login: Moodle
     redirige aquí, respondemos redirigiendo de vuelta a su auth endpoint.
  2. POST /tutoria/lti/launch      — Moodle POSTea el id_token (JWT firmado).
     Validamos firma (contra el JWKS de Moodle), nonce, state y audiencia.
     Del JWT validado extraemos:
       - `sub`                                          → identidad del alumno/profesor
                                                            (pseudónima, estable por plataforma)
       - claim `.../lti/claim/context` (id, title)       → EL CURSO
       - claim `.../lti/claim/roles`                     → si es profesor o alumno
       - claim `.../lti/claim/resource_link`             → la actividad concreta enlazada
     El `context_id` del curso se usa directamente como NAMESPACE de la base de
     conocimiento (knowledge_base.py) — así cada curso de Moodle tiene su propio
     RAG aislado sin configuración adicional.
  3. GET /tutoria/lti/session/{launch_id} — la SPA/frontend cambia el launch_id
     (de un solo uso, expira a los 10 min) por el contexto ya resuelto.
  4. GET /tutoria/lti/jwks         — clave pública de la herramienta (requerida
     por el spec; no usamos servicios firmados hacia Moodle en esta versión).

Alcance actual: login + launch + identificación de usuario/curso/contexto.
NO implementado (roadmap, no pedido por Inserver): Deep Linking, Names and
Roles Provisioning, Assignment and Grades Services (notas).

Registro de plataformas: lti_platforms.json (vacío por defecto — ver README).
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
import typing as t
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request as StarletteRequest, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pylti1p3.cookie import CookieService
from pylti1p3.exception import LtiException, OIDCException
from pylti1p3.launch_data_storage.cache import CacheDataStorage
from pylti1p3.message_launch import MessageLaunch
from pylti1p3.oidc_login import OIDCLogin
from pylti1p3.redirect import Redirect
from pylti1p3.request import Request as LtiRequest
from pylti1p3.session import SessionService
from pylti1p3.tool_config import ToolConfJsonFile

BASE_DIR = Path(__file__).parent
PLATFORMS_FILE = BASE_DIR / "lti_platforms.json"
LTI_CACHE_DB = BASE_DIR / "lti_keys" / "lti_cache.db"

router = APIRouter()


# ── Cache SQLite (backend de CacheDataStorage — interfaz get/set/delete) ────
# pylti1p3 recomienda explícitamente NO depender solo de cookies para el
# round-trip OIDC (SameSite/Chrome rompe launches cross-site) — el estado
# vive aquí, la cookie solo transporta un session-id opaco.
class _SqliteCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, exp REAL)"
        )
        self._con.commit()

    def get(self, key: str) -> t.Any:
        row = self._con.execute("SELECT v, exp FROM kv WHERE k=?", (key,)).fetchone()
        if not row:
            return None
        value, exp = row
        if exp and exp < time.time():
            self._con.execute("DELETE FROM kv WHERE k=?", (key,))
            self._con.commit()
            return None
        return json.loads(value)

    def set(self, key: str, value: t.Any, exp: int | None = 3600) -> None:
        expiry = (time.time() + exp) if exp else None
        self._con.execute(
            "INSERT OR REPLACE INTO kv (k, v, exp) VALUES (?, ?, ?)",
            (key, json.dumps(value), expiry),
        )
        self._con.commit()


_cache = _SqliteCache(LTI_CACHE_DB)


class _TutoriaCacheDataStorage(CacheDataStorage):
    def __init__(self):
        super().__init__()
        self._cache = _cache


# ── Adaptadores FastAPI para pylti1p3 (mismo patrón que el contrib/flask
#    oficial de la librería — pylti1p3 no trae soporte nativo para FastAPI) ──
class _TutoriaLtiRequest(LtiRequest):
    def __init__(self, params: dict, cookies: dict, secure: bool):
        super().__init__()
        self._params = params
        self._cookies = cookies
        self._secure = secure
        self.session: dict = {}

    def get_param(self, key: str) -> str | None:
        return self._params.get(key)

    def get_cookie(self, key: str) -> str | None:
        return self._cookies.get(key)

    def is_secure(self) -> bool:
        return self._secure


class _TutoriaCookieService(CookieService):
    def __init__(self, request: _TutoriaLtiRequest):
        self._request = request
        self._to_set: dict[str, dict] = {}

    def _key(self, name: str) -> str:
        return f"{self._cookie_prefix}-{name}"

    def get_cookie(self, name: str) -> str | None:
        return self._request.get_cookie(self._key(name))

    def set_cookie(self, name: str, value, exp: int | None = 3600) -> None:
        self._to_set[self._key(name)] = {"value": value, "exp": exp}

    def apply(self, response: Response) -> None:
        for key, data in self._to_set.items():
            response.set_cookie(
                key=key, value=str(data["value"]), max_age=data["exp"],
                secure=self._request.is_secure(), httponly=True, path="/",
                samesite="none" if self._request.is_secure() else "lax",
            )


class _TutoriaSessionService(SessionService):
    def __init__(self, request: _TutoriaLtiRequest):
        super().__init__(request)
        self.data_storage = _TutoriaCacheDataStorage()
        self.data_storage.set_request(request)


class _TutoriaRedirect(Redirect):
    def __init__(self, location: str, cookie_service: _TutoriaCookieService):
        self._location = location
        self._cookie_service = cookie_service

    def _finish(self, response: Response) -> Response:
        self._cookie_service.apply(response)
        return response

    def do_redirect(self):
        return self._finish(RedirectResponse(self._location, status_code=302))

    def do_js_redirect(self):
        return self._finish(HTMLResponse(
            f'<script>window.location="{self._location}";</script>'
        ))

    def set_redirect_url(self, location: str) -> None:
        self._location = location

    def get_redirect_url(self) -> str:
        return self._location


class _TutoriaOIDCLogin(OIDCLogin):
    def __init__(self, request, tool_config):
        cookie_service = _TutoriaCookieService(request)
        session_service = _TutoriaSessionService(request)
        super().__init__(request, tool_config, session_service, cookie_service)

    def get_redirect(self, url: str) -> _TutoriaRedirect:
        return _TutoriaRedirect(url, self._cookie_service)

    def get_response(self, html: str):
        resp = HTMLResponse(html)
        self._cookie_service.apply(resp)
        return resp


class _TutoriaMessageLaunch(MessageLaunch):
    def __init__(self, request, tool_config):
        cookie_service = _TutoriaCookieService(request)
        session_service = _TutoriaSessionService(request)
        super().__init__(request, tool_config, session_service, cookie_service)

    def _get_request_param(self, key: str) -> str | None:
        return self._request.get_param(key)


def _tool_config() -> ToolConfJsonFile:
    if not PLATFORMS_FILE.exists():
        raise HTTPException(503, "lti_platforms.json no encontrado")
    try:
        return ToolConfJsonFile(str(PLATFORMS_FILE))
    except Exception as e:
        # Config vacía/solo comentarios ("_comentario", "_ejemplo") = ninguna
        # plataforma registrada todavía → 503 explícito, no un 500 opaco.
        raise HTTPException(503, f"Ninguna plataforma Moodle registrada aún: {e}")


async def _extract_params(request: StarletteRequest) -> dict:
    if request.method == "POST":
        form = await request.form()
        return {**request.query_params, **dict(form)}
    return dict(request.query_params)


def _is_secure(request: StarletteRequest) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.api_route("/lti/login", methods=["GET", "POST"])
async def lti_login(request: StarletteRequest):
    """Paso 1 — OIDC third-party initiated login. Moodle redirige aquí antes
    de lanzar la herramienta; nosotros redirigimos de vuelta a su auth
    endpoint con state+nonce, que Moodle firmará en el id_token del launch."""
    params = await _extract_params(request)
    target = params.get("target_link_uri")
    if not target:
        # Validar ANTES del try: si se lanza dentro, el except lo re-envuelve
        # y Starlette acaba devolviendo 500 en vez de un 400 legible.
        raise HTTPException(400, "Falta el parámetro target_link_uri (¿lo lanza Moodle como External Tool?)")
    lti_req = _TutoriaLtiRequest(params, dict(request.cookies), _is_secure(request))
    tool_conf = _tool_config()
    try:
        oidc = _TutoriaOIDCLogin(lti_req, tool_conf)
        return oidc.redirect(target)
    except (LtiException, OIDCException) as e:
        raise HTTPException(400, f"Error OIDC login: {e}")


@router.post("/lti/launch")
async def lti_launch(request: StarletteRequest):
    """Paso 2 — Moodle POSTea aquí el id_token firmado tras la autenticación.
    Validamos y extraemos usuario+curso+contexto+rol, y redirigimos al
    frontend con un launch_id opaco de un solo uso (ver /lti/session)."""
    params = await _extract_params(request)
    lti_req = _TutoriaLtiRequest(params, dict(request.cookies), _is_secure(request))
    tool_conf = _tool_config()
    try:
        launch = _TutoriaMessageLaunch(lti_req, tool_conf)
        launch.validate()
    except LtiException as e:
        raise HTTPException(401, f"Launch LTI inválido: {e}")

    data = launch.get_launch_data()
    context = data.get("https://purl.imsglobal.org/spec/lti/claim/context") or {}
    roles = data.get("https://purl.imsglobal.org/spec/lti/claim/roles") or []
    resource_link = data.get("https://purl.imsglobal.org/spec/lti/claim/resource_link") or {}

    context_id = context.get("id") or "moodle_sin_contexto"
    is_instructor = any("Instructor" in r or "Teacher" in r for r in roles)

    launch_id = secrets.token_urlsafe(24)
    _cache.set(f"tutoria-lti-session-{launch_id}", {
        "namespace": context_id,                       # → knowledge_base.py
        "course_title": context.get("title") or context.get("label") or "",
        "user_sub": data.get("sub"),                    # id pseudónimo, estable por plataforma
        "user_name": data.get("name") or data.get("given_name") or "Alumno",
        "is_instructor": is_instructor,
        "resource_link_title": resource_link.get("title", ""),
        "iss": launch.get_iss(),
    }, exp=600)  # 10 min — de un solo uso, se borra al leerlo

    import os
    frontend = os.getenv(
        "TUTORIA_LTI_REDIRECT_URL",
        "https://adrianmoreno-dev.com/demo/tutor-ia",
    )
    return RedirectResponse(f"{frontend}?lti_launch={launch_id}", status_code=302)


@router.get("/lti/session/{launch_id}")
def lti_session(launch_id: str):
    """Paso 3 — el frontend cambia el launch_id (una vez) por el contexto
    resuelto: namespace del curso, nombre de alumno, si es profesor, etc."""
    key = f"tutoria-lti-session-{launch_id}"
    ctx = _cache.get(key)
    if not ctx:
        raise HTTPException(404, "launch_id no encontrado o ya usado (expira en 10 min, un solo uso)")
    _cache.set(key, None, exp=1)  # invalidar — un solo uso
    return ctx


@router.get("/lti/jwks")
def lti_jwks():
    """Clave pública de la herramienta (requerida por el spec LTI 1.3).

    Debe funcionar SIEMPRE, incluso sin plataformas registradas: Moodle valida
    esta URL en el momento de dar de alta la herramienta, que es justo ANTES de
    que exista el client_id con el que registrarla aquí (problema del huevo y
    la gallina). Se construye directamente desde lti_keys/public.key."""
    try:
        tool_conf = _tool_config()
        jwks = tool_conf.get_jwks()
        if jwks.get("keys"):
            return jwks
    except HTTPException:
        pass  # aún sin plataformas: caemos al JWKS derivado de la clave pública

    from pylti1p3.registration import Registration
    pub_path = BASE_DIR / "lti_keys" / "public.key"
    if not pub_path.exists():
        raise HTTPException(503, "Falta lti_keys/public.key — genera el par de claves de la herramienta")
    return {"keys": [Registration.get_jwk(pub_path.read_text())]}
