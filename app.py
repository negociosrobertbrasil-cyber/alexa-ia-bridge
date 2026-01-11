import os
from flask import Flask, request, jsonify

# OpenAI SDK (pip install openai)
from openai import OpenAI

app = Flask(__name__)

# ----------------------------
# Config
# ----------------------------
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # podés cambiarlo luego
client = OpenAI()  # usa OPENAI_API_KEY del environment :contentReference[oaicite:1]{index=1}


# ----------------------------
# Helpers Alexa
# ----------------------------
def alexa_response(text, end_session=False, reprompt=None, session_attributes=None):
    """
    Arma una respuesta estándar de Alexa Skills Kit
    """
    if session_attributes is None:
        session_attributes = {}

    r = {
        "version": "1.0",
        "sessionAttributes": session_attributes,
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": text
            },
            "shouldEndSession": end_session
        }
    }

    if reprompt:
        r["response"]["reprompt"] = {
            "outputSpeech": {
                "type": "PlainText",
                "text": reprompt
            }
        }

    return r


def safe_get(d, *path, default=None):
    """
    Navega dicts anidados sin reventar.
    safe_get(obj, "request", "intent", "slots")
    """
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def extract_user_text(payload):
    """
    Intenta extraer el texto del usuario desde:
    - slot "texto"
    - cualquier slot string
    - query suelta si viene en un campo raro
    """
    intent = safe_get(payload, "request", "intent", default={}) or {}
    slots = intent.get("slots") or {}

    # 1) slot específico "texto"
    if "texto" in slots:
        v = (slots.get("texto") or {}).get("value")
        if v:
            return v.strip()

    # 2) cualquier slot con value
    for s in slots.values():
        if isinstance(s, dict):
            v = s.get("value")
            if isinstance(v, str) and v.strip():
                return v.strip()

    # 3) fallback: a veces aparece como "query" o similar en tests raros
    possible = safe_get(payload, "request", "query")
    if isinstance(possible, str) and possible.strip():
        return possible.strip()

    return ""


# ----------------------------
# OpenAI call
# ----------------------------
def ask_openai(user_text):
    """
    Llama a OpenAI Responses API y devuelve texto plano.
    Usamos un system prompt corto para controlar el estilo.
    """
    system_prompt = (
        Listo. Sos una IA clara, directa y útil. Contestás en español.
        Si falta contexto, asumí lo mínimo y pedí al usuario que precise.
        Respuestas cortas, prácticas y sin humo.
    )

    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        # store=False  # opcional
    )
    # resp.output_text está documentado como salida directa de texto :contentReference[oaicite:2]{index=2}
    out = (resp.output_text or "").strip()
    return out if out else "No pude generar una respuesta. Probá reformular la pregunta."


# ----------------------------
# Routes
# ----------------------------
@app.get("/")
def home():
    return "OK - Alexa IA Bridge funcionando", 200


@app.route("/alexa", methods=["POST", "GET", "HEAD"])
def alexa_webhook():
    # Verificación simple de vida
    if request.method in ("GET", "HEAD"):
        return "OK", 200

    payload = request.get_json(silent=True) or {}

    rtype = safe_get(payload, "request", "type", default="")
    intent_name = safe_get(payload, "request", "intent", "name", default="")

    # 1) LaunchRequest
    if rtype == "LaunchRequest":
        return jsonify(
            alexa_response(
                "Hola Robert. Decime una frase empezando con: pregunta. Por ejemplo: pregunta cuánto es dos más dos.",
                end_session=False,
                reprompt="Decí: pregunta... y tu consulta."
            )
        ), 200

    # 2) Stop/Cancel
    if intent_name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
        return jsonify(alexa_response("Listo, cierro.", end_session=True)), 200

    # 3) Fallback (cuando Alexa no matchea nada)
    if intent_name in ("AMAZON.FallbackIntent",):
        return jsonify(
            alexa_response(
                "No entendí. Probá diciendo: pregunta... y tu consulta.",
                end_session=False,
                reprompt="Decí: pregunta... y tu consulta."
            )
        ), 200

    # 4) IntentRequest (tu intent custom)
    if rtype == "IntentRequest":
        text = extract_user_text(payload)

        if not text:
            return jsonify(
                alexa_response(
                    "No me llegó texto. Probá diciendo: pregunta... y tu consulta.",
                    end_session=False,
                    reprompt="Decí: pregunta... y tu consulta."
                )
            ), 200

        # Si querés forzar que empiece con "pregunta", podés descomentar:
        # if not text.lower().startswith("pregunta"):
        #     return jsonify(alexa_response("Decí: pregunta... y tu consulta.", False, "Decí: pregunta... y tu consulta.")), 200

        # Le pegamos a la IA y devolvemos
        try:
            answer = ask_openai(text)
        except Exception as e:
            # No le muestres el error interno al usuario
            answer = "Tuve un problema conectando con la IA. Revisá la API key y volvé a intentar."

        return jsonify(
            alexa_response(
                answer,
                end_session=False,
                reprompt="Decí otra pregunta... lo que quieras."
            )
        ), 200

    # 5) Default fallback
    return jsonify(
        alexa_response(
            "Estoy vivo, pero no entendí el tipo de request. Probá diciendo: pregunta... y tu consulta.",
            end_session=False,
            reprompt="Decí: pregunta... y tu consulta."
        )
    ), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

