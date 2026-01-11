import os
import time
import logging
from typing import Any, Dict, Optional

from flask import Flask, request, jsonify

from openai import OpenAI

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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_ALEXA_CHARS = int(os.getenv("MAX_ALEXA_CHARS", "800"))  # evita respuestas demasiado largas
OPENAI_TIMEOUT_S = float(os.getenv("OPENAI_TIMEOUT_S", "12"))  # si tarda mucho, Alexa te corta

# Seguridad opcional:
# Si lo ponés en "1", verifica firmas de Alexa (recomendado en prod).
VERIFY_ALEXA_SIGNATURE = os.getenv("VERIFY_ALEXA_SIGNATURE", "0") == "1"


# OpenAI client con timeout
client = OpenAI(timeout=OPENAI_TIMEOUT_S)


# ----------------------------
# Helpers
# ----------------------------
def alexa_response(
    text: str,
    end_session: bool = False,
    reprompt: Optional[str] = None,
    session_attributes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if session_attributes is None:
        session_attributes = {}

    # Alexa a veces falla si mandás textos demasiado largos
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
    Orden de prioridad:
    1) request.inputTranscript (cuando existe)
    2) slot específico "texto"
    3) cualquier slot con 'value'
    4) request.query (tests raros)
    """
    # 1) inputTranscript
    it = safe_get(payload, "request", "inputTranscript", default="")
    if isinstance(it, str) and it.strip():
        return it.strip()

    intent = safe_get(payload, "request", "intent", default={}) or {}
    slots = intent.get("slots") or {}

    # 2) slot "texto"
    if isinstance(slots, dict) and "texto" in slots:
        v = (slots.get("texto") or {}).get("value")
        if isinstance(v, str) and v.strip():
            return v.strip()

    # 3) cualquier slot con value
    if isinstance(slots, dict):
        for s in slots.values():
            if isinstance(s, dict):
                v = s.get("value")
                if isinstance(v, str) and v.strip():
                    return v.strip()

    # 4) request.query (cuando el simulador manda cosas raras)
    q = safe_get(payload, "request", "query", default="")
    if isinstance(q, str) and q.strip():
        return q.strip()

    return ""


def verify_alexa_request_or_raise():
    """
    Verificación de firma Amazon (opcional).
    Si VERIFY_ALEXA_SIGNATURE=1 y falta la lib, te avisamos en logs.
    """
    if not VERIFY_ALEXA_SIGNATURE:
        return

    try:
        from ask_sdk_webservice_support.verifier import SignatureVerifier
    except Exception as e:
        log.warning("Firma Alexa activada pero falta dependencia ask-sdk-webservice-support: %s", e)
        return

    # Flask request headers:
    cert_url = request.headers.get("SignatureCertChainUrl")
    signature = request.headers.get("Signature")
    body = request.get_data(as_text=False)

    # Lanza excepción si es inválido
    SignatureVerifier().verify(body, cert_url, signature)


def ask_openai(user_text: str) -> str:
    system_prompt = (
        "Sos una IA clara, directa y útil. Contestás en español rioplatense.\n"
        "Respuestas cortas, prácticas, sin humo.\n"
        "Si falta contexto, pedí precisión con una sola pregunta."
    )

    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        # store=False,  # opcional
    )

    out = (getattr(resp, "output_text", None) or "").strip()
    return out or "No pude generar una respuesta. Probá reformular la pregunta."


# ----------------------------
# Routes
# ----------------------------
@app.get("/")
def home():
    return "OK - Alexa IA Bridge funcionando", 200


@app.route("/alexa", methods=["POST", "GET", "HEAD"])
def alexa_webhook():
    # Healthcheck simple
    if request.method in ("GET", "HEAD"):
        return "OK", 200

    start = time.time()
    try:
        # Seguridad opcional
        verify_alexa_request_or_raise()

        payload = request.get_json(silent=True) or {}
        rtype = safe_get(payload, "request", "type", default="") or ""
        intent_name = safe_get(payload, "request", "intent", "name", default="") or ""

        req_id = safe_get(payload, "request", "requestId", default="") or ""
        log.info("Alexa request: type=%s intent=%s id=%s", rtype, intent_name, req_id)

        # 1) LaunchRequest
        if rtype == "LaunchRequest":
            return jsonify(
                alexa_response(
                    "Hola Robert. Decime una frase empezando con: pregunta. Por ejemplo: pregunta cuánto es dos más dos.",
                    end_session=False,
                    reprompt="Decí: pregunta... y tu consulta.",
                )
            ), 200

        # 2) SessionEndedRequest (Alexa cierra; devolvés OK)
        if rtype == "SessionEndedRequest":
            return jsonify(alexa_response("Listo.", end_session=True)), 200

        # 3) Intents de sistema
        if intent_name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
            return jsonify(alexa_response("Listo, cierro.", end_session=True)), 200

        if intent_name == "AMAZON.HelpIntent":
            return jsonify(
                alexa_response(
                    "Usame así: decí 'pregunta' y después tu consulta. Por ejemplo: pregunta quién es Elon Musk.",
                    end_session=False,
                    reprompt="Decí: pregunta... y tu consulta.",
                )
            ), 200

        # 4) FallbackIntent (cuando no matchea nada)
        if intent_name == "AMAZON.FallbackIntent":
            return jsonify(
                alexa_response(
                    "No entendí. Probá diciendo: pregunta... y tu consulta.",
                    end_session=False,
                    reprompt="Decí: pregunta... y tu consulta.",
                )
            ), 200

        # 5) IntentRequest (tu intent custom o cualquier otro)
        if rtype == "IntentRequest":
            user_text = extract_user_text(payload)

            if not user_text:
                return jsonify(
                    alexa_response(
                        "No me llegó el texto. Probá diciendo: pregunta... y tu consulta.",
                        end_session=False,
                        reprompt="Decí: pregunta... y tu consulta.",
                    )
                ), 200

            try:
                answer = ask_openai(user_text)
            except Exception as e:
                log.exception("Error OpenAI: %s", e)
                answer = "Tuve un problema conectando con la IA. Revisá la API key y volvé a intentar."

            return jsonify(
                alexa_response(
                    answer,
                    end_session=False,
                    reprompt="Decí otra pregunta... lo que quieras.",
                )
            ), 200

        # 6) Default fallback (tipo desconocido)
        return jsonify(
            alexa_response(
                "Estoy vivo, pero no entendí el tipo de request. Probá diciendo: pregunta... y tu consulta.",
                end_session=False,
                reprompt="Decí: pregunta... y tu consulta.",
            )
        ), 200

    except Exception as e:
        # Si algo explota, NO devolvemos 500 crudo (Alexa lo interpreta como no respuesta)
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
