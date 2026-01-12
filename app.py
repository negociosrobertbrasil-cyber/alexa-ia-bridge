import os
import time
import json
import logging
import threading
from typing import Any, Dict, Optional, Tuple

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
# Config (ENV)
# -----------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# IMPORTANTÍSIMO:
# En la API de Generative Language (v1beta), el endpoint ya incluye /models/ en la URL.
# Por eso acá el modelo debe ser tipo:
#   gemini-1.5-flash-latest
# o gemini-1.5-pro-latest
# (SIN "models/" adelante)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash-latest").strip()

# Si alguien lo setea como "models/xxx", lo corregimos (pero NO agregamos "models/" nosotros)
if GEMINI_MODEL.startswith("models/"):
    GEMINI_MODEL = GEMINI_MODEL[len("models/"):].strip()

MAX_ALEXA_CHARS = int(os.getenv("MAX_ALEXA_CHARS", "800"))
GEMINI_TIMEOUT_S = float(os.getenv("GEMINI_TIMEOUT_S", "8"))
CACHE_TTL_S = int(os.getenv("CACHE_TTL_S", "30"))
DEDUP_TTL_S = int(os.getenv("DEDUP_TTL_S", "180"))
VERIFY_ALEXA_SIGNATURE = os.getenv("VERIFY_ALEXA_SIGNATURE", "0").strip() == "1"

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_GENERATE_URL = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent"
GEMINI_LIST_URL = f"{GEMINI_BASE}/models"

log.info("BOOT config: GEMINI_MODEL=%s", GEMINI_MODEL)
log.info("BOOT config: GEMINI_GENERATE_URL=%s", GEMINI_GENERATE_URL)
log.info("BOOT config: MAX_ALEXA_CHARS=%s TIMEOUT=%s CACHE_TTL=%s DEDUP_TTL=%s",
         MAX_ALEXA_CHARS, GEMINI_TIMEOUT_S, CACHE_TTL_S, DEDUP_TTL_S)

# -----------------------------
# Simple in-memory cache/dedup
# -----------------------------
_cache_lock = threading.Lock()
_cache: Dict[str, Tuple[float, str]] = {}      # key -> (expires_ts, answer)
_dedup: Dict[str, float] = {}                  # requestId -> expires_ts


def _now() -> float:
    return time.time()


def cache_get(key: str) -> Optional[str]:
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        exp, val = item
        if _now() > exp:
            _cache.pop(key, None)
            return None
        return val


def cache_set(key: str, val: str, ttl_s: int) -> None:
    with _cache_lock:
        _cache[key] = (_now() + ttl_s, val)


def dedup_get(req_id: str) -> bool:
    with _cache_lock:
        exp = _dedup.get(req_id)
        if not exp:
            return False
        if _now() > exp:
            _dedup.pop(req_id, None)
            return False
        return True


def dedup_set(req_id: str, ttl_s: int) -> None:
    with _cache_lock:
        _dedup[req_id] = _now() + ttl_s


# -----------------------------
# Alexa helpers
# -----------------------------
def safe_get(d: Dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def alexa_response(text: str, end_session: bool = False, reprompt: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": text[:MAX_ALEXA_CHARS]},
            "shouldEndSession": end_session,
        },
    }
    if reprompt:
        out["response"]["reprompt"] = {
            "outputSpeech": {"type": "PlainText", "text": reprompt[:MAX_ALEXA_CHARS]}
        }
    return out


def extract_user_query(payload: Dict[str, Any]) -> str:
    # IntentRequest: intent.slots.consulta.value (según tu skill)
    v = safe_get(payload, "request", "intent", "slots", "consulta", "value", default="") or ""
    v = str(v).strip()
    if v:
        return v

    # Fallback: algunos modelos meten otro nombre de slot o el intent no trae slots
    slots = safe_get(payload, "request", "intent", "slots", default={})
    if isinstance(slots, dict):
        for _, slot in slots.items():
            val = ""
            if isinstance(slot, dict):
                val = str(slot.get("value", "")).strip()
            if val:
                return val

    return ""


# -----------------------------
# Gemini call
# -----------------------------
def gemini_generate(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("Falta GEMINI_API_KEY en variables de entorno")

    prompt = prompt.strip()
    if not prompt:
        return "Decime tu consulta."

    cache_key = f"q:{prompt.lower()}"
    cached = cache_get(cache_key)
    if cached:
        log.info("CACHE HIT prompt=%r", prompt[:80])
        return cached

    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}

    body = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]}
        ]
    }

    log.info("Gemini call: model=%s url=%s timeout=%ss", GEMINI_MODEL, GEMINI_GENERATE_URL, GEMINI_TIMEOUT_S)

    r = requests.post(
        GEMINI_GENERATE_URL,
        headers=headers,
        params=params,
        data=json.dumps(body),
        timeout=GEMINI_TIMEOUT_S,
    )

    if r.status_code != 200:
        try:
            err = r.json()
        except Exception:
            err = {"raw": r.text[:500]}
        log.error("Gemini HTTP %s error=%s", r.status_code, err)
        raise RuntimeError(f"Gemini HTTP {r.status_code}")

    data = r.json()

    # parse candidates[0].content.parts[*].text
    text_out = ""
    candidates = data.get("candidates", [])
    if candidates:
        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        for p in parts:
            t = p.get("text", "")
            if t:
                text_out += t

    text_out = (text_out or "").strip()
    if not text_out:
        text_out = "No recibí texto de Gemini. Probá de nuevo con otra pregunta."

    cache_set(cache_key, text_out, CACHE_TTL_S)
    return text_out


# -----------------------------
# Optional: debug routes
# -----------------------------
@app.route("/", methods=["GET"])
def root():
    return "OK", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/debug/models", methods=["GET"])
def debug_models():
    if not GEMINI_API_KEY:
        return jsonify({"error": "missing GEMINI_API_KEY"}), 500
    try:
        r = requests.get(GEMINI_LIST_URL, params={"key": GEMINI_API_KEY}, timeout=10)
        return (r.text, r.status_code, {"Content-Type": "application/json"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------
# Alexa webhook
# -----------------------------
@app.route("/alexa", methods=["POST", "GET", "HEAD"])
def alexa_webhook():
    # Browser sanity check
    if request.method in ("GET", "HEAD"):
        return "OK", 200

    start = _now()
    payload = request.get_json(silent=True) or {}

    rtype = safe_get(payload, "request", "type", default="")
    req_id = safe_get(payload, "request", "requestId", default="") or ""

    log.info("Alexa request: type=%s requestId=%s", rtype, req_id)

    # Dedup: si Alexa reintenta el mismo requestId, respondemos rápido
    if req_id:
        if dedup_get(req_id):
            log.info("DEDUP HIT requestId=%s", req_id)
            return jsonify(alexa_response("Decí otra pregunta...", end_session=False, reprompt="Decime tu consulta.")), 200
        dedup_set(req_id, DEDUP_TTL_S)

    try:
        if rtype == "LaunchRequest":
            msg = "Hola Robert. Decime: preguntá… y tu consulta. Ejemplo: preguntá quién es Elon Musk."
            return jsonify(alexa_response(msg, end_session=False, reprompt="Decime tu consulta.")), 200

        if rtype == "SessionEndedRequest":
            return jsonify(alexa_response("OK", end_session=True)), 200

        if rtype == "IntentRequest":
            user_q = extract_user_query(payload)
            if not user_q:
                return jsonify(alexa_response("No entendí la consulta. Repetila, por favor.", end_session=False,
                                             reprompt="Decime tu consulta.")), 200

            answer = gemini_generate(user_q)
            dt = (_now() - start) * 1000
            log.info("Webhook OK in %.1fms", dt)
            return jsonify(alexa_response(answer, end_session=False, reprompt="Decime otra pregunta.")), 200

        # Default fallback
        return jsonify(alexa_response("No entiendo ese tipo de request.", end_session=False)), 200

    except Exception as e:
        dt = (_now() - start) * 1000
        log.exception("Webhook ERROR in %.1fms: %s", dt, str(e))
        return jsonify(alexa_response("No pude obtener respuesta de Gemini. Revisá logs del servidor.",
                                      end_session=False, reprompt="Probá otra vez.")), 200


if __name__ == "__main__":
    # Render usa PORT
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
