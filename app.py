import os
import time
import logging
import threading
from typing import Any, Dict, Optional, Tuple, List

import requests
from flask import Flask, request, jsonify

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("alexa-ia-bridge")

# -----------------------------
# App
# -----------------------------
app = Flask(__name__)

# -----------------------------
# Config (env vars)
# -----------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# OJO: el modelo lo vamos a normalizar para evitar "models/models/..."
GEMINI_MODEL_RAW = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()

MAX_ALEXA_CHARS = int(os.getenv("MAX_ALEXA_CHARS", "800"))
GEMINI_TIMEOUT_S = float(os.getenv("GEMINI_TIMEOUT_S", "8"))
CACHE_TTL_S = int(os.getenv("CACHE_TTL_S", "30"))
DEDUP_TTL_S = int(os.getenv("DEDUP_TTL_S", "180"))

# Firma Alexa: dejalo en 0 por ahora. Activarlo sin librería te complica.
VERIFY_ALEXA_SIGNATURE = os.getenv("VERIFY_ALEXA_SIGNATURE", "0") == "1"

# -----------------------------
# Gemini endpoints
# -----------------------------
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_LIST_MODELS_URL = f"{GEMINI_BASE}/models"


def normalize_model_name(name: str) -> str:
    """
    Gemini listModels devuelve nombres tipo:
      - "models/gemini-1.5-flash"
    A veces la gente setea:
      - "gemini-1.5-flash"
    Normalizamos para que SIEMPRE quede "models/xxx".
    """
    n = (name or "").strip()
    if not n:
        return "models/gemini-1.5-flash"
    if n.startswith("models/"):
        return n
    return f"models/{n}"


GEMINI_MODEL = normalize_model_name(GEMINI_MODEL_RAW)
GEMINI_URL = f"{GEMINI_BASE}/{GEMINI_MODEL}:generateContent"

# -----------------------------
# In-memory cache/dedup
# -----------------------------
_cache_lock = threading.Lock()
_cache: Dict[str, Tuple[float, str]] = {}

_dedup_lock = threading.Lock()
_dedup: Dict[str, Tuple[float, str]] = {}


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


# -----------------------------
# Alexa helpers
# -----------------------------
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

    resp = {
        "version": "1.0",
        "sessionAttributes": session_attributes,
        "response": {
            "outputSpeech": {"type": "PlainText", "text": safe_text or "Ok."},
            "shouldEndSession": end_session,
        },
    }

    if reprompt:
        resp["response"]["reprompt"] = {
            "outputSpeech": {"type": "PlainText", "text": reprompt}
        }

    return resp


def safe_get(d: Any, *path: str, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def extract_user_text(payload: Dict[str, Any]) -> str:
    # Algunas integraciones usan inputTranscript
    it = safe_get(payload, "request", "inputTranscript", default="")
    if isinstance(it, str) and it.strip():
        return it.strip()

    intent = safe_get(payload, "request", "intent", default={}) or {}
    slots = intent.get("slots") or {}

    # Si tenés un slot llamado "texto"
    if isinstance(slots, dict) and "texto" in slots:
        v = (slots.get("texto") or {}).get("value")
        if isinstance(v, str) and v.strip():
            return v.strip()

    # Si no, agarramos el primer slot con value
    if isinstance(slots, dict):
        for s in slots.values():
            if isinstance(s, dict):
                v = s.get("value")
                if isinstance(v, str) and v.strip():
                    return v.strip()

    # fallback
    q = safe_get(payload, "request", "query", default="")
    if isinstance(q, str) and q.strip():
        return q.strip()

    return ""


def verify_alexa_request_or_raise() -> None:
    """
    Para producción real conviene verificar firma.
    Para destrabar esto YA, lo dejamos apagado (VERIFY_ALEXA_SIGNATURE=0).
    """
    if not VERIFY_ALEXA_SIGNATURE:
        return

    # Si lo activás, necesitás esta librería en requirements.txt:
    # ask-sdk-webservice-support
    try:
        from ask_sdk_webservice_support.verifier import SignatureVerifier
    except Exception as e:
        log.warning("Firma Alexa activada pero falta ask-sdk-webservice-support: %s", e)
        return

    cert_url = request.headers.get("SignatureCertChainUrl")
    signature = request.headers.get("Signature")
    body = request.get_data(as_text=False)
    SignatureVerifier().verify(body, cert_url, signature)


# -----------------------------
# Gemini helpers
# -----------------------------
def _has_key() -> bool:
    return bool(GEMINI_API_KEY)


def list_gemini_models() -> Dict[str, Any]:
    if not _has_key():
        return {"ok": False, "error": "Falta GEMINI_API_KEY"}

    try:
        resp = requests.get(
            GEMINI_LIST_MODELS_URL,
            params={"key": GEMINI_API_KEY},
            timeout=min(6.0, GEMINI_TIMEOUT_S),
        )
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": f"Error de red listando modelos: {e}"}

    if resp.status_code != 200:
        return {"ok": False, "error": f"HTTP {resp.status_code}", "body": resp.text[:800]}

    data = resp.json()
    models = data.get("models") or []

    usable: List[Dict[str, Any]] = []
    for m in models:
        methods = m.get("supportedGenerationMethods") or []
        if "generateContent" in methods:
            usable.append(
                {
                    "name": m.get("name"),  # ej "models/gemini-1.5-flash"
                    "displayName": m.get("displayName"),
                    "methods": methods,
                }
            )

    return {
        "ok": True,
        "configured_model_env": GEMINI_MODEL_RAW,
        "configured_model_normalized": GEMINI_MODEL,
        "configured_url": GEMINI_URL,
        "usable_count": len(usable),
        "usable_models": usable[:60],
    }


def ask_gemini(user_text: str) -> str:
    if not _has_key():
        return "Falta configurar GEMINI_API_KEY en el servidor."

    system_prompt = (
        "Sos una IA clara, directa y útil. Contestás en español rioplatense.\n"
        "Respuestas cortas, prácticas, sin humo.\n"
        "Si falta contexto, pedí precisión con una sola pregunta."
    )

    cache_key = f"{GEMINI_MODEL}::{user_text.strip()}"
    cached = cache_get(cache_key)
    if cached:
        log.info("Cache HIT (ttl=%ss)", CACHE_TTL_S)
        return cached

    log.info("Gemini model=%s timeout=%.1fs url=%s", GEMINI_MODEL, GEMINI_TIMEOUT_S, GEMINI_URL)

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

    try:
        resp = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=GEMINI_TIMEOUT_S,
        )
    except requests.exceptions.Timeout:
        return "La IA tardó demasiado. Probá de nuevo con una pregunta más corta."
    except requests.exceptions.RequestException as e:
        log.exception("Error de red Gemini: %s", e)
        return "Tuve un problema de red conectando con Gemini. Probá de nuevo."

    if resp.status_code in (401, 403):
        return "La API key de Gemini no tiene permisos o es inválida. Revisá GEMINI_API_KEY."
    if resp.status_code == 429:
        return "Gemini me rate-limitó. Probá de nuevo en un ratito."
    if resp.status_code >= 500:
        return "Gemini está con problemas del lado del servidor. Probá de nuevo."

    if resp.status_code != 200:
        # Esto te muestra el error REAL en logs (incluye el message del 404)
        try:
            j = resp.json()
        except Exception:
            j = {"raw": resp.text[:1200]}
        log.error("Gemini error HTTP=%s body=%s", resp.status_code, j)
        return "No pude obtener respuesta de Gemini. Revisá logs del servidor."

    data = resp.json()

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


# -----------------------------
# Debug endpoints
# -----------------------------
@app.get("/debug/health")
def debug_health():
    return jsonify(
        {
            "ok": True,
            "service": "alexa-ia-bridge",
            "has_api_key": _has_key(),
            "configured_model_env": GEMINI_MODEL_RAW,
            "configured_model_normalized": GEMINI_MODEL,
            "gemini_url": GEMINI_URL,
        }
    ), 200


@app.get("/debug/models")
def debug_models():
    info = list_gemini_models()
    code = 200 if info.get("ok") else 500
    return jsonify(info), code


# -----------------------------
# Basic routes
# -----------------------------
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

        # Dedup
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

        if rtype == "SessionEndedRequest":
            msg = "Listo."
            dedup_set(req_id, msg)
            return jsonify(alexa_response(msg, end_session=True)), 200

        if intent_name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
            msg = "Listo, cierro."
            dedup_set(req_id, msg)
            return jsonify(alexa_response(msg, end_session=True)), 200

        if intent_name == "AMAZON.HelpIntent":
            msg = "Usame así: decí 'pregunta' y después tu consulta. Por ejemplo: pregunta quién es Elon Musk."
            dedup_set(req_id, msg)
            return jsonify(alexa_response(msg, end_session=False, reprompt="Decí: pregunta... y tu consulta.")), 200

        if intent_name == "AMAZON.FallbackIntent":
            msg = "No entendí. Probá diciendo: pregunta... y tu consulta."
            dedup_set(req_id, msg)
            return jsonify(alexa_response(msg, end_session=False, reprompt="Decí: pregunta... y tu consulta.")), 200

        if rtype == "IntentRequest":
            user_text = extract_user_text(payload)

            if not user_text:
                msg = "No me llegó el texto. Probá diciendo: pregunta... y tu consulta."
                dedup_set(req_id, msg)
                return jsonify(alexa_response(msg, end_session=False, reprompt="Decí: pregunta... y tu consulta.")), 200

            answer = ask_gemini(user_text)
            dedup_set(req_id, answer)

            return jsonify(alexa_response(answer, end_session=False, reprompt="Decí otra pregunta... lo que quieras.")), 200

        msg = "Estoy vivo, pero no entendí el tipo de request. Probá diciendo: pregunta... y tu consulta."
        dedup_set(req_id, msg)
        return jsonify(alexa_response(msg, end_session=False, reprompt="Decí: pregunta... y tu consulta.")), 200

    except Exception as e:
        log.exception("Error general webhook: %s", e)
        return jsonify(
            alexa_response(
                "Se cayó algo del lado del servidor. Probá de nuevo en unos segundos.",
                end_session=False,
                reprompt="Decí: pregunta... y tu consulta.",
            )
        ), 200

    finally:
        elapsed = (time.time() - start) * 1000
        log.info("Webhook time: %.1fms", elapsed)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
