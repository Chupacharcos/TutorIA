"""
Base de conocimiento MULTI-TENANT para TutorIA (RAG real).

Responde directamente a las preguntas de integración de Inserver (2026-07-28):
  - "¿puede trabajar sobre documentación proporcionada por cada organización?"
  - "¿qué tipos de contenidos puede indexar?" → PDF, TXT, Markdown.
  - "¿separar la base de conocimiento por plataforma, curso o cliente?"
  - "¿las respuestas pueden incluir referencia a la fuente concreta?"

Reutiliza el MISMO stack ya probado en producción en chatbot-manual
(PyMuPDF + intfloat/multilingual-e5-base + FAISS IndexFlatL2, ver
/var/www/chatbot/src/rag_engine.py y api.py::build_faiss_index_from_pdf) —
sin dependencias nuevas, ambos servicios comparten venv.

Diferencia clave con chatbot-manual: aquí el índice se persiste en DISCO por
NAMESPACE (no es efímero por sesión). Un namespace = una organización/curso —
en el flujo LTI (lti.py) el namespace es el `context_id` del curso de Moodle;
en uso directo (API key) es el que decida el integrador. Sube su documentación
UNA vez y queda disponible para todas las conversaciones futuras de ese
namespace. SIN namespace, TutorIA se comporta exactamente igual que antes de
este cambio (solo LLM + memoria conversacional) — compatibilidad total con la
demo pública.
"""
from __future__ import annotations

import pickle
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import faiss
import fitz  # PyMuPDF — mismo extractor que chatbot-manual
from sentence_transformers import SentenceTransformer

KB_ROOT = Path(__file__).parent / "knowledge_base_data"
EMBEDDINGS_MODEL = "intfloat/multilingual-e5-base"  # mismo modelo que chatbot-manual
ALLOWED_SUFFIXES = {".pdf", ".txt", ".md"}

_embedder: SentenceTransformer | None = None
_index_cache: dict[str, dict] = {}  # namespace -> {"index":.., "chunks":[...]}


def _get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBEDDINGS_MODEL)
    return _embedder


def _safe_namespace(namespace: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", namespace.strip())[:80] or "default"


def _ns_dir(namespace: str) -> Path:
    d = KB_ROOT / _safe_namespace(namespace)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _extract_text(file_path: Path) -> str:
    """PDF (PyMuPDF), TXT o MD. ValueError si el tipo no está soportado."""
    suffix = file_path.suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(
            f"Tipo de archivo no soportado: {suffix} (admitidos: {sorted(ALLOWED_SUFFIXES)})"
        )
    if suffix == ".pdf":
        doc = fitz.open(str(file_path))
        try:
            return "\n\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
    return file_path.read_text(encoding="utf-8", errors="ignore")


def _chunk_text(text: str, chunk_size: int = 900, overlap: int = 120) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
    return [c.strip() for c in chunks if len(c.strip()) > 40]


DEMO_NS_PREFIX = "demo_"
DEMO_NS_TTL_DAYS = 7


def prune_demo_namespaces(ttl_days: int = DEMO_NS_TTL_DAYS) -> int:
    """Borra los namespaces de la DEMO pública (`demo_*`) sin actividad en
    `ttl_days`. Se llama de forma oportunista al indexar, así el disco se
    mantiene solo sin cron ni timer que mantener. Los namespaces reales de
    integraciones (LTI: el context_id del curso) NUNCA se tocan."""
    import shutil
    if not KB_ROOT.exists():
        return 0
    cutoff = datetime.now().timestamp() - ttl_days * 86400
    removed = 0
    for d in KB_ROOT.iterdir():
        if not d.is_dir() or not d.name.startswith(DEMO_NS_PREFIX):
            continue
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                _index_cache.pop(d.name, None)
                removed += 1
        except Exception:
            continue
    return removed


def ingest_document(namespace: str, file_path: str, source_name: str) -> dict:
    """Indexa un documento en la base de conocimiento del namespace.
    Persiste en disco inmediatamente. Devuelve {source_id, chunks_added, total_chunks}."""
    try:
        prune_demo_namespaces()  # housekeeping oportunista, nunca bloquea la subida
    except Exception:
        pass
    fp = Path(file_path)
    text = _extract_text(fp)
    chunk_texts = _chunk_text(text)
    if not chunk_texts:
        raise ValueError("No se pudo extraer texto indexable del documento (¿está vacío o escaneado sin OCR?)")

    embedder = _get_embedder()
    embeddings = embedder.encode(chunk_texts, show_progress_bar=False)

    ns_dir = _ns_dir(namespace)
    index_path = ns_dir / "index.faiss"
    chunks_path = ns_dir / "chunks.pkl"

    if index_path.exists() and chunks_path.exists():
        index = faiss.read_index(str(index_path))
        with open(chunks_path, "rb") as f:
            existing = pickle.load(f)
    else:
        index = faiss.IndexFlatL2(embeddings.shape[1])
        existing = []

    source_id = _safe_namespace(source_name)[:50] + "_" + datetime.now().strftime("%y%m%d%H%M%S")
    new_chunks = [
        {"text": t, "source_id": source_id, "source_name": source_name, "chunk_idx": i}
        for i, t in enumerate(chunk_texts)
    ]

    index.add(embeddings.astype("float32"))
    existing.extend(new_chunks)

    faiss.write_index(index, str(index_path))
    with open(chunks_path, "wb") as f:
        pickle.dump(existing, f)

    _index_cache.pop(_safe_namespace(namespace), None)
    return {"source_id": source_id, "chunks_added": len(new_chunks), "total_chunks": len(existing)}


def list_sources(namespace: str) -> list[dict]:
    chunks_path = _ns_dir(namespace) / "chunks.pkl"
    if not chunks_path.exists():
        return []
    with open(chunks_path, "rb") as f:
        chunks = pickle.load(f)
    seen: dict[str, dict] = {}
    for c in chunks:
        sid = c["source_id"]
        seen.setdefault(sid, {"source_id": sid, "source_name": c["source_name"], "chunks": 0})
        seen[sid]["chunks"] += 1
    return list(seen.values())


def delete_source(namespace: str, source_id: str) -> bool:
    """Elimina un documento y reconstruye el índice del namespace sin él."""
    ns_dir = _ns_dir(namespace)
    chunks_path = ns_dir / "chunks.pkl"
    if not chunks_path.exists():
        return False
    with open(chunks_path, "rb") as f:
        chunks = pickle.load(f)
    remaining = [c for c in chunks if c["source_id"] != source_id]
    if len(remaining) == len(chunks):
        return False

    index_path = ns_dir / "index.faiss"
    if remaining:
        embedder = _get_embedder()
        embeddings = embedder.encode([c["text"] for c in remaining], show_progress_bar=False)
        index = faiss.IndexFlatL2(embeddings.shape[1])
        index.add(embeddings.astype("float32"))
        faiss.write_index(index, str(index_path))
    else:
        index_path.unlink(missing_ok=True)

    with open(chunks_path, "wb") as f:
        pickle.dump(remaining, f)
    _index_cache.pop(_safe_namespace(namespace), None)
    return True


def has_knowledge_base(namespace: str | None) -> bool:
    if not namespace:
        return False
    return (_ns_dir(namespace) / "index.faiss").exists()


def retrieve(namespace: str, query: str, top_k: int = 4) -> list[dict]:
    """Chunks más relevantes del namespace para la query, cada uno con su fuente."""
    key = _safe_namespace(namespace)
    if key not in _index_cache:
        ns_dir = _ns_dir(namespace)
        index_path = ns_dir / "index.faiss"
        chunks_path = ns_dir / "chunks.pkl"
        if not (index_path.exists() and chunks_path.exists()):
            return []
        with open(chunks_path, "rb") as f:
            chunks = pickle.load(f)
        _index_cache[key] = {"index": faiss.read_index(str(index_path)), "chunks": chunks}

    cache = _index_cache[key]
    if not cache["chunks"]:
        return []
    embedder = _get_embedder()
    q_emb = embedder.encode([query], show_progress_bar=False)
    _, idxs = cache["index"].search(q_emb.astype("float32"), min(top_k, len(cache["chunks"])))
    return [cache["chunks"][i] for i in idxs[0] if 0 <= i < len(cache["chunks"])]
