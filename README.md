# TutorIA — El profesor particular que nunca se cansa

Aplicación web de tutoría adaptativa con IA para niños de 6 a 14 años. La IA adapta automáticamente el lenguaje, el ritmo y el tipo de ejercicios al perfil cognitivo de cada alumno.

## Demo en vivo

[adrianmoreno-dev.com/demo/tutor-ia](https://adrianmoreno-dev.com/demo/tutor-ia)

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
