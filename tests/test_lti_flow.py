"""
Test E2E del flujo LTI 1.3 SIN Moodle real.

Levanta una PLATAFORMA SIMULADA que hace exactamente lo que hace Moodle:
  1. genera su propio par de claves RSA y publica un JWKS,
  2. inicia el login OIDC contra /tutoria/lti/login,
  3. firma un `id_token` real (RS256) con los claims obligatorios del spec
     (iss, aud, sub, nonce, deployment_id, message_type, version, roles,
     context, resource_link) y lo POSTea a /tutoria/lti/launch,
  4. sigue la redirección y canjea el launch_id por el contexto.

Si esto pasa, la validación criptográfica, el nonce, el state y la extracción
de usuario/curso/rol funcionan de verdad: es el mismo camino que recorrería
Moodle. Lo único que no cubre es la configuración concreta del Moodle destino.

Uso:  /var/www/chatbot/venv/bin/python tests/test_lti_flow.py
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import jwt  # PyJWT (dependencia de pylti1p3)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

PASS, FAIL = 0, 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name} — {detail}")
        print(f"  ✗ {name}{('  — ' + detail) if detail else ''}")


# ── Plataforma simulada (hace de Moodle) ────────────────────────────────────
ISS = "https://moodle-simulado.test"
CLIENT_ID = "TEST_CLIENT_ID_123"
DEPLOYMENT_ID = "1"
PLATFORM_KID = "moodle-sim-key-1"

_platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_platform_priv_pem = _platform_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
).decode()


def platform_jwks() -> dict:
    """JWKS que publicaría Moodle en /mod/lti/certs.php."""
    from pylti1p3.registration import Registration
    pub_pem = _platform_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    jwk = dict(Registration.get_jwk(pub_pem))
    jwk["kid"] = PLATFORM_KID
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return {"keys": [jwk]}


def sign_id_token(nonce: str, extra: dict | None = None) -> str:
    """Construye y firma el id_token igual que lo hace Moodle en un launch."""
    now = int(time.time())
    claims = {
        "iss": ISS,
        "aud": CLIENT_ID,
        "sub": "moodle-user-42",                    # id pseudónimo del alumno
        "exp": now + 600,
        "iat": now,
        "nonce": nonce,
        "name": "Ana Pérez",
        "given_name": "Ana",
        "https://purl.imsglobal.org/spec/lti/claim/message_type": "LtiResourceLinkRequest",
        "https://purl.imsglobal.org/spec/lti/claim/version": "1.3.0",
        "https://purl.imsglobal.org/spec/lti/claim/deployment_id": DEPLOYMENT_ID,
        "https://purl.imsglobal.org/spec/lti/claim/target_link_uri":
            "https://adrianmoreno-dev.com/tutoria/lti/launch",
        "https://purl.imsglobal.org/spec/lti/claim/roles": [
            "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
        ],
        "https://purl.imsglobal.org/spec/lti/claim/context": {
            "id": "curso-mates-6b",
            "label": "MAT6B",
            "title": "Matemáticas 6º B",
            "type": ["http://purl.imsglobal.org/vocab/lis/v2/course#CourseOffering"],
        },
        "https://purl.imsglobal.org/spec/lti/claim/resource_link": {
            "id": "actividad-tutor-1",
            "title": "Tutor de refuerzo",
        },
    }
    if extra:
        claims.update(extra)
    return jwt.encode(claims, _platform_priv_pem, algorithm="RS256",
                      headers={"kid": PLATFORM_KID})


def main() -> int:
    import lti

    # ── Registrar la plataforma simulada en una config temporal ─────────────
    cfg = {
        ISS: [{
            "default": True,
            "client_id": CLIENT_ID,
            "auth_login_url": f"{ISS}/mod/lti/auth.php",
            "auth_token_url": f"{ISS}/mod/lti/token.php",
            "auth_audience": None,
            "key_set_url": f"{ISS}/mod/lti/certs.php",
            "key_set": platform_jwks(),          # evita salida a red en el test
            "private_key_file": "lti_keys/private.key",
            "public_key_file": "lti_keys/public.key",
            "deployment_ids": [DEPLOYMENT_ID],
        }]
    }
    tmp_cfg = BASE / "lti_platforms.test.json"
    tmp_cfg.write_text(json.dumps(cfg), encoding="utf-8")
    original = lti.PLATFORMS_FILE
    lti.PLATFORMS_FILE = tmp_cfg

    try:
        import api
        client = TestClient(api.app, base_url="https://adrianmoreno-dev.com")

        print("\n=== 1) JWKS de la herramienta (lo lee Moodle al configurar) ===")
        r = client.get("/tutoria/lti/jwks")
        check("jwks responde 200", r.status_code == 200, f"HTTP {r.status_code}")
        keys = r.json().get("keys", [])
        check("jwks expone una clave pública RSA", bool(keys) and keys[0].get("kty") == "RSA")

        print("\n=== 2) Login OIDC (paso 1 del launch) ===")
        r = client.post("/tutoria/lti/login", data={
            "iss": ISS,
            "login_hint": "moodle-user-42",
            "target_link_uri": "https://adrianmoreno-dev.com/tutoria/lti/launch",
            "client_id": CLIENT_ID,
            "lti_deployment_id": DEPLOYMENT_ID,
        }, follow_redirects=False)
        check("login redirige (302) al auth endpoint de la plataforma",
              r.status_code == 302, f"HTTP {r.status_code}")
        location = r.headers.get("location", "")
        check("redirige al auth_login_url correcto", location.startswith(f"{ISS}/mod/lti/auth.php"),
              location[:80])
        qs = parse_qs(urlparse(location).query)
        state = qs.get("state", [""])[0]
        nonce = qs.get("nonce", [""])[0]
        check("incluye state y nonce", bool(state) and bool(nonce))
        check("client_id y redirect_uri correctos",
              qs.get("client_id", [""])[0] == CLIENT_ID and "lti/launch" in qs.get("redirect_uri", [""])[0])
        cookies = dict(r.cookies)

        print("\n=== 3) Launch con id_token FIRMADO por la plataforma ===")
        id_token = sign_id_token(nonce)
        r = client.post("/tutoria/lti/launch",
                        data={"id_token": id_token, "state": state},
                        cookies=cookies, follow_redirects=False)
        check("launch válido → 302 al frontend", r.status_code == 302,
              f"HTTP {r.status_code}: {r.text[:200]}")
        launch_url = r.headers.get("location", "")
        launch_id = parse_qs(urlparse(launch_url).query).get("lti_launch", [""])[0]
        check("devuelve un launch_id", bool(launch_id), launch_url[:100])

        print("\n=== 4) El frontend canjea el launch_id por el contexto ===")
        r = client.get(f"/tutoria/lti/session/{launch_id}")
        check("session responde 200", r.status_code == 200, f"HTTP {r.status_code}")
        ctx = r.json() if r.status_code == 200 else {}
        check("namespace = id del curso (aísla el RAG por curso)",
              ctx.get("namespace") == "curso-mates-6b", str(ctx.get("namespace")))
        check("identifica al usuario (sub pseudónimo)", ctx.get("user_sub") == "moodle-user-42")
        check("identifica el título del curso", ctx.get("course_title") == "Matemáticas 6º B")
        check("detecta rol alumno (no instructor)", ctx.get("is_instructor") is False)
        check("recoge la actividad enlazada", ctx.get("resource_link_title") == "Tutor de refuerzo")

        print("\n=== 5) Seguridad ===")
        r = client.get(f"/tutoria/lti/session/{launch_id}")
        check("launch_id es de UN SOLO USO (2ª vez → 404)", r.status_code == 404,
              f"HTTP {r.status_code}")

        bad = jwt.encode({"iss": ISS, "aud": CLIENT_ID, "sub": "x"},
                         _platform_priv_pem, algorithm="RS256",
                         headers={"kid": PLATFORM_KID})
        r = client.post("/tutoria/lti/launch", data={"id_token": bad, "state": state},
                        cookies=cookies, follow_redirects=False)
        check("id_token sin claims obligatorios → rechazado", r.status_code >= 400,
              f"HTTP {r.status_code}")

        # Firmado por OTRA clave (suplantación)
        rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        rogue_pem = rogue.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()).decode()
        forged = jwt.encode(json.loads(json.dumps({
            "iss": ISS, "aud": CLIENT_ID, "sub": "atacante",
            "exp": int(time.time()) + 600, "iat": int(time.time()), "nonce": nonce,
            "https://purl.imsglobal.org/spec/lti/claim/message_type": "LtiResourceLinkRequest",
            "https://purl.imsglobal.org/spec/lti/claim/version": "1.3.0",
            "https://purl.imsglobal.org/spec/lti/claim/deployment_id": DEPLOYMENT_ID,
            "https://purl.imsglobal.org/spec/lti/claim/roles": [],
        })), rogue_pem, algorithm="RS256", headers={"kid": PLATFORM_KID})
        r = client.post("/tutoria/lti/launch", data={"id_token": forged, "state": state},
                        cookies=cookies, follow_redirects=False)
        check("id_token firmado por clave AJENA → rechazado", r.status_code >= 400,
              f"HTTP {r.status_code} (¡debería rechazar la suplantación!)")

        r = client.post("/tutoria/lti/launch",
                        data={"id_token": sign_id_token(nonce), "state": "estado-inventado"},
                        cookies=cookies, follow_redirects=False)
        check("state inválido → rechazado", r.status_code >= 400, f"HTTP {r.status_code}")

        print("\n=== 6) Launch como PROFESOR (rol distinto) ===")
        r = client.post("/tutoria/lti/login", data={
            "iss": ISS, "login_hint": "prof-1",
            "target_link_uri": "https://adrianmoreno-dev.com/tutoria/lti/launch",
            "client_id": CLIENT_ID, "lti_deployment_id": DEPLOYMENT_ID,
        }, follow_redirects=False)
        qs2 = parse_qs(urlparse(r.headers["location"]).query)
        tok = sign_id_token(qs2["nonce"][0], extra={
            "https://purl.imsglobal.org/spec/lti/claim/roles": [
                "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"],
        })
        r = client.post("/tutoria/lti/launch",
                        data={"id_token": tok, "state": qs2["state"][0]},
                        cookies=dict(r.cookies), follow_redirects=False)
        lid = parse_qs(urlparse(r.headers.get("location", "")).query).get("lti_launch", [""])[0]
        ctx2 = client.get(f"/tutoria/lti/session/{lid}").json() if lid else {}
        check("detecta rol INSTRUCTOR", ctx2.get("is_instructor") is True, str(ctx2)[:120])

    finally:
        lti.PLATFORMS_FILE = original
        tmp_cfg.unlink(missing_ok=True)

    print(f"\nRESULTADO LTI: {PASS} OK · {FAIL} fallos")
    for f in FAILS:
        print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
