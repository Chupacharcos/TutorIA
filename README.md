# TutorIA — El profesor particular que nunca se cansa

Aplicación web de tutoría adaptativa con IA para niños de 6 a 14 años. La IA adapta automáticamente el lenguaje, el ritmo y el tipo de ejercicios al perfil cognitivo de cada alumno.

**Licencia:** MIT (ver [LICENSE](LICENSE)) — uso libre, incluido comercial, manteniendo el aviso de copyright. Sin garantía ni soporte incluidos.

## Demo en vivo

[adrianmoreno-dev.com/demo/tutor-ia](https://adrianmoreno-dev.com/demo/tutor-ia)

---

## Integración, datos y licencia

### Integración con Moodle (LTI 1.3)

TutorIA se instala en Moodle como **External Tool** estándar (LTI 1.3 Core, implementado con [`pylti1p3`](https://github.com/dmitry-viskov/pylti1p3), la librería de referencia del spec 1EdTech). No requiere plugin propio.

| Endpoint | Uso en la configuración de Moodle |
|---|---|
| `POST/GET /tutoria/lti/login` | *Initiate login URL* |
| `POST /tutoria/lti/launch` | *Redirection URI* / Launch URL |
| `GET /tutoria/lti/jwks` | *Public keyset URL* |

**Pasos:**
1. En TutorIA: registra tu Moodle en `lti_platforms.json` (`client_id`, `auth_login_url`, `auth_token_url`, `key_set_url`, `deployment_ids`). El fichero incluye un ejemplo comentado.
2. En Moodle: *Administración del sitio → Plugins → Módulos de actividad → LTI externo → Gestionar herramientas → Configurar manualmente*, usando las URLs de arriba.
3. Las claves de la herramienta se generan en `lti_keys/` (la privada nunca se versiona).

**Identificación de usuario, curso y contexto:** del `id_token` firmado que envía Moodle (validado contra su JWKS: firma, `nonce`, `state` y audiencia) se extraen:

| Dato | Origen (claim LTI) |
|---|---|
| Identidad del usuario | `sub` — identificador **pseudónimo** y estable por plataforma |
| Curso | `.../claim/context` → `id` (usado como *namespace* del RAG) y `title` |
| Rol (profesor/alumno) | `.../claim/roles` |
| Actividad concreta | `.../claim/resource_link` |

El frontend recupera ese contexto llamando una sola vez a `GET /tutoria/lti/session/{launch_id}` (token de un solo uso, expira a los 10 min).

> **Alcance actual:** login OIDC + launch + identificación de usuario/curso/rol.
> **No implementado (roadmap):** Deep Linking, Names and Roles Provisioning (NRPS) y envío de notas (AGS).

### Base de conocimiento propia (RAG multi-tenant)

Por defecto TutorIA responde con el conocimiento general del modelo. Si una organización sube su documentación, el tutor la usa como **material prioritario** y **cita la fuente** al final de la respuesta.

| Pregunta | Respuesta |
|---|---|
| ¿Formatos indexables? | **PDF, TXT y Markdown** (PyMuPDF + embeddings). Páginas de Moodle o webs: no directamente — hay que exportarlas a esos formatos. |
| ¿Separación por cliente/curso? | Sí. Cada **namespace** = un índice FAISS aislado en disco. En un launch LTI el namespace es automáticamente el `context_id` del curso. |
| ¿Cita la fuente? | Sí, de forma **determinista** (la añade el código con los documentos realmente recuperados, no depende de que el LLM "se acuerde" de citar). |

```bash
# Subir documentación al curso/organización (requiere X-Admin-Key)
curl -X POST https://tu-servidor/tutoria/kb/<namespace>/upload \
     -H "X-Admin-Key: $KB_ADMIN_KEY" -F "file=@guia_del_centro.pdf"

curl https://tu-servidor/tutoria/kb/<namespace>/sources          # listar
curl -X DELETE https://tu-servidor/tutoria/kb/<namespace>/sources/<source_id> \
     -H "X-Admin-Key: $KB_ADMIN_KEY"                             # eliminar
```

Stack de recuperación: `intfloat/multilingual-e5-base` + FAISS `IndexFlatL2` (mismo motor ya en producción en el chatbot RAG del portfolio).

### Tratamiento de datos

| Qué | Dónde | Cuánto tiempo |
|---|---|---|
| Conversaciones | **Solo en memoria del proceso** (`_sessions` en `tutor.py`). No hay BD ni ficheros de log de conversaciones. | Se pierden al reiniciar el servicio |
| Documentos indexados | Disco del propio servidor: `knowledge_base_data/<namespace>/` (texto troceado + vectores) | Hasta que se borren vía API |
| Contexto del launch LTI | SQLite local (`lti_keys/lti_cache.db`) | 10 min, un solo uso |

**Qué sale del servidor:** el texto de cada mensaje del alumno, el resumen de la conversación y (si hay base de conocimiento) los fragmentos recuperados se envían a la **API de Groq** para generar la respuesta. Nada más se comparte con terceros. En un despliegue propio, los datos que salen son exactamente los que decida el proveedor LLM que configures.

> ⚠️ Al estar orientado a menores, revisa la normativa aplicable (RGPD/LOPDGDD) antes de un despliegue real y valora usar un modelo autoalojado (ver abajo).

### Despliegue propio y otros motores de IA

Todo el repositorio es la aplicación completa: se puede desplegar **100 % en infraestructura propia** (FastAPI + systemd, sin dependencias SaaS más allá del proveedor LLM).

El motor está aislado en `tutor.py` (`ChatGroq` de LangChain). Cambiar a **OpenAI, Azure OpenAI, Gemini u Ollama/vLLM local** es sustituir esa clase por el `ChatOpenAI` / `ChatGoogleGenerativeAI` / `ChatOllama` equivalente de LangChain — el resto del código (perfiles, RAG, LTI) no cambia. Con un modelo local, ningún dato sale de tu infraestructura.

### Costes

El código es gratuito (MIT). Los costes reales de un despliegue son ajenos al proyecto: **motor de IA** (Groq tiene plan gratuito con límites; OpenAI/Azure se facturan por uso; un modelo local requiere GPU), **infraestructura** donde se aloje, y la **implantación/mantenimiento**, que corren a cargo de quien lo despliega — el autor no ofrece soporte ni consultoría.

## Características

### Perfiles de aprendizaje
| Perfil | Adaptación |
|--------|-----------|
| **Estándar** | Explicaciones claras con ejemplos cotidianos |
| **TDAH** | Respuestas cortas, emojis, pasos muy pequeños, celebración de logros |
| **Dislexia** | Frases simples, listas visuales, sin corrección directa de ortografía |
| **Bajo rendimiento** | Inicio desde lo más básico, refuerzo continuo de la confianza |

### Funcionalidades
- Conversación natural en lenguaje adaptado al perfil del alumno
- **Memoria entre sesiones** — el alumno no repite sus dificultades cada vez
- Detección automática de errores repetidos → cambio de enfoque
- Panel de padres con resumen de sesión generado por IA

## Arquitectura técnica

```
FastAPI (puerto 8096)
├── tutor.py          Motor de conversación con LangChain
│   ├── ConversationSummaryBufferMemory (historial entre sesiones)
│   ├── ConversationChain con prompts por perfil
│   └── Detección de errores repetidos → cambio de explicación
├── router.py         Endpoints REST
└── api.py            FastAPI app + CORS
```

### Flujo
1. El alumno selecciona su perfil al entrar
2. LangChain carga el historial de sesiones anteriores
3. El prompt del sistema cambia tono, longitud y ejercicios según perfil
4. Si el alumno falla 2 veces lo mismo → la IA cambia el enfoque automáticamente
5. Los padres ven el resumen en su panel separado

## Stack técnico

| Capa | Tecnología |
|------|-----------|
| LLM | Groq API — LLaMA 3.1 (200-300 tokens/s) |
| Orquestación | LangChain Classic |
| Memoria | ConversationSummaryBufferMemory |
| API | FastAPI + Uvicorn |
| Frontend | Laravel + Blade |
| DB | MySQL |

## Endpoints

```
POST /tutoria/chat              Conversación con el tutor
POST /tutoria/session/new       Crear nueva sesión
GET  /tutoria/session/{id}/summary  Resumen de sesión
GET  /tutoria/profiles          Perfiles disponibles
GET  /tutoria/health
```

## Instalación

```bash
# Requiere el venv compartido en /var/www/chatbot/venv
pip install fastapi uvicorn langchain-classic langchain-core langchain-groq python-dotenv

# Variables de entorno
cp .env.example .env
# Añadir: GROQ_API_KEY=gsk_...

# Desarrollo
uvicorn api:app --host 127.0.0.1 --port 8096 --reload

# Producción (systemd)
sudo systemctl start tutoria
```

## Servicio systemd

```ini
[Unit]
Description=TutorIA FastAPI — puerto 8096
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/var/www/tutoria
ExecStart=/var/www/chatbot/venv/bin/uvicorn api:app --host 127.0.0.1 --port 8096
Restart=on-failure
```
