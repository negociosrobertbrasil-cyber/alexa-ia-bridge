import os
import time
import json
import logging
import threading
from typing import Any, Dict, Optional, Tuple

import requests
from flask import Flask, request, jsonify

# ----------------------------
# Logging (simple y útil)
# ----------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("alexa-ia-bridge")

# ----------------------------
# App
# ----------------------------
app = Flask(__name__)

# ----------------------------
# Config
# ----------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash-latest").strip()

MAX_ALEXA_CHARS = int(os.getenv("MAX_ALEXA_CHARS", "800"))

# Timeouts (conservadores para Alexa)
GEMINI_TIMEOUT_S = float(os.getenv("GEMINI_TIMEOUT_S", "8"))

# Cache (para evitar gastar tokens al pedo)
CACHE_TTL_S = int(os.getenv("CACHE_TTL_S", "30"))

# Dedup por requestId de Alexa (evita doble cobro si Alexa reintenta el MISMO request)
DEDUP_TTL_S = int(os.getenv("DEDUP_TTL_S", "180"))

# Seguridad opcional (firma Amazon)
VERIFY_ALEXA_SIGNATURE = os.getenv("VERIFY_ALEXA_SIGNATURE", "0") == "1"

# Endpoint Gemini (Generative Language API)
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


# ----------------------------
# Cache / Dedup in-memory (simple pero efectivo)
# ----------------------------
_cache_lock = threading.Lock()
_cache: Dict[str, Tuple[float, str]] = {}  # key -> (expire_ts, answer)

_dedup_lock = threading.Lock()
_dedup: Dict[str, Tuple[float, str]] = {}  # requestId -> (expire_ts, answer)


def _now() -> float:
    return time.time()


def _prune_store(store: Dict[str, Tuple[float, str]]) -> None:
    t = _now()
    dead = [k for k, (exp, _) in store.items() if exp <= t]
    for k in dead:
        store.pop(k, None)


def cache_get(key: str) -> Optional[str]:
    if CACHE_TTL_S <= 0:
        return None
    with _cache_lock:
        _prune_store(_cache)
        v = _cache.get(key)
        if not v:
            return None
        exp, ans = v
        if exp <= _now():
            _cache.pop(key, None)
            return None
        return ans


def cache_set(key: str, ans: str) -> None:
    if CACHE_TTL_S <= 0:
        return
    with _cache_lock:
        _prune_store(_cache)
        _cache[key] = (_now() + CACHE_TTL_S, ans)


def dedup_get(req_id: str) -> Optional[str]:
    if not req_id:
        return None
    with _dedup_lock:
        _prune_store(_dedup)
        v = _dedup.get(req_id)
        if not v:
            return None
        exp, ans = v
        if exp <= _now():
            _dedup.pop(req_id, None)
            return None
        return ans


def dedup_set(req_id: str, ans: str) -> None:
    if not req_id:
        return
    with _dedup_lock:
        _prune_store(_dedup)
        _dedup[req_id] = (_now() + DEDUP_TTL_S, ans)


# ----------------------------
# Helpers Alexa
# ----------------------------
def alexa_response(
    text: str,
    end_session: bool = False,
    reprompt: Optional[str] = None,
    session_attributes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if session_attributes is None:
        session_attributes = {}

    safe_text = (text or "").strip()
    if len(safe_text) > MAX_ALEXA_CHARS:
        safe_text = safe_text[: MAX_ALEXA_CHARS - 3].rstrip() + "..."

    r = {
        "version": "1.0",
        "sessionAttributes": session_attributes,
        "response": {
            "outputSpeech": {"type": "PlainText", "text": safe_text or "Ok."},
            "shouldEndSession": end_session,
        },
    }

    if reprompt:
        r["response"]["reprompt"] = {
            "outputSpeech": {"type": "PlainText", "text": reprompt}
        }

    return r


def safe_get(d: Any, *path: str, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def extract_user_text(payload: Dict[str, Any]) -> str:
    """
    Extrae texto de usuario SIN depender del nombre del slot.
    Prioridad:
    1) request.inputTranscript
    2) slot "texto"
    3) cualquier slot con 'value'
    4) request.query
    """
    it = safe_get(payload, "request", "inputTranscript", default="")
    if isinstance(it, str) and it.strip():
        return it.strip()

    intent = safe_get(payload, "request", "intent", default={}) or {}
    slots = intent.get("slots") or {}

    if isinstance(slots, dict) and "texto" in slots:
        v = (slots.get("texto") or {}).get("value")
        if isinstance(v, str) and v.strip():
            return v.strip()

    if isinstance(slots, dict):
        for s in slots.values():
            if isinstance(s, dict):
                v = s.get("value")
                if isinstance(v, str) and v.strip():
                    return v.strip()

    q = safe_get(payload, "request", "query", default="")
    if isinstance(q, str) and q.strip():
        return q.strip()

    return ""


def verify_alexa_request_or_raise():
    """
    Verificación de firma Amazon (opcional).
    """
    if not VERIFY_ALEXA_SIGNATURE:
        return

    try:
        from ask_sdk_webservice_support.verifier import SignatureVerifier
    except Exception as e:
        log.warning("Firma Alexa activada pero falta ask-sdk-webservice-support: %s", e)
        return

    cert_url = request.headers.get("SignatureCertChainUrl")
    signature = request.headers.get("Signature")
    body = request.get_data(as_text=False)

    SignatureVerifier().verify(body, cert_url, signature)


# ----------------------------
# Gemini
# ----------------------------
def ask_gemini(user_text: str) -> str:
    if not GEMINI_API_KEY:
        return "Falta configurar GEMINI_API_KEY en el servidor."

    system_prompt = (
        "Sos una IA clara, directa y útil. Contestás en español rioplatense.\n"
        "Respuestas cortas, prácticas, sin humo.\n"
        "Si falta contexto, pedí precisión con una sola pregunta."
    )

    # Cache por texto (evita gastar tokens si preguntan lo mismo en 30s)
    cache_key = f"{GEMINI_MODEL}::{user_text.strip()}"
    cached = cache_get(cache_key)
    if cached:
        log.info("Cache HIT (ttl=%ss)", CACHE_TTL_S)
        return cached

    log.info("Gemini model=%s timeout=%.1fs", GEMINI_MODEL, GEMINI_TIMEOUT_S)

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": f"{system_prompt}\n\nUsuario: {user_text}"}],
            }
        ],
        "generationConfig": {
            "temperature": 0.6,
            "maxOutputTokens": 300,
        },
    }

    params = {"key": GEMINI_API_KEY}

    try:
        resp = requests.post(
            GEMINI_URL,
            params=params,
            json=payload,
            timeout=GEMINI_TIMEOUT_S,
        )
    except requests.exceptions.Timeout:
        return "La IA tardó demasiado. Probá de nuevo con una pregunta más corta."
    except requests.exceptions.RequestException as e:
        log.exception("Error de red Gemini: %s", e)
        return "Tuve un problema de red conectando con Gemini. Probá de nuevo."

    if resp.status_code == 401 or resp.status_code == 403:
        return "La API key de Gemini no tiene permisos o es inválida. Revisá GEMINI_API_KEY."
    if resp.status_code == 429:
        return "Gemini me rate-limitó. Probá de nuevo en un ratito."
    if resp.status_code >= 500:
        return "Gemini está con problemas del lado del servidor. Probá de nuevo."

    if resp.status_code != 200:
        # Log interno para diagnóstico
        log.error("Gemini error %s: %s", resp.status_code, resp.text[:800])
        return "No pude obtener respuesta de Gemini. Revisá logs del servidor."

    data = resp.json()

    # Estructura típica:
    # candidates[0].content.parts[0].text
    text = ""
    try:
        candidates = data.get("candidates") or []
        if candidates:
            content = (candidates[0].get("content") or {})
            parts = content.get("parts") or []
            if parts:
                text = (parts[0].get("text") or "").strip()
    except Exception:
        text = ""

    if not text:
        text = "No pude generar una respuesta. Probá reformular la pregunta."

    cache_set(cache_key, text)
    return text


# ----------------------------
# Routes
# ----------------------------
@app.get("/")
def home():
    return "OK - Alexa IA Bridge funcionando (Gemini)", 200


@app.route("/alexa", methods=["POST", "GET", "HEAD"])
def alexa_webhook():
    if request.method in ("GET", "HEAD"):
        return "OK", 200

    start = time.time()
    try:
        verify_alexa_request_or_raise()

        payload = request.get_json(silent=True) or {}
        rtype = safe_get(payload, "request", "type", default="") or ""
        intent_name = safe_get(payload, "request", "intent", "name", default="") or ""
        req_id = safe_get(payload, "request", "requestId", default="") or ""

        log.info("Alexa request: type=%s intent=%s id=%s", rtype, intent_name, req_id)

        # DEDUP: si Alexa reintenta el MISMO requestId, devolvemos la misma respuesta sin cobrar de nuevo
        if req_id:
            prev = dedup_get(req_id)
            if prev:
                log.info("DEDUP HIT requestId=%s", req_id)
                return jsonify(alexa_response(prev, end_session=False, reprompt="Decí otra pregunta...")), 200

        if rtype == "LaunchRequest":
            msg = "Hola Robert. Decime: pregunta... y tu consulta. Ejemplo: pregunta cuánto es dos más dos."
            dedup_set(req_id, msg)
            return jsonify(
                alexa_response(
                    msg,
                    end_session=False,
                    reprompt="Decí: pregunta... y tu consulta.",
                )
            ), 200
