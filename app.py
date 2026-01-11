from flask import Flask, request, jsonify
import os

app = Flask(__name__)


@app.get("/")
def home():
    return "OK - Alexa IA Bridge funcionando", 200


def alexa_response(text: str, end_session: bool = False, reprompt: str | None = None):
    """Construye una respuesta Alexa (ASK) estándar."""
    r = {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": text},
            "shouldEndSession": end_session,
        },
    }
    if reprompt:
        r["response"]["reprompt"] = {
            "outputSpeech": {"type": "PlainText", "text": reprompt}
        }
    return r


def get_slot_value(intent: dict, slot_names: list[str]) -> str:
    """Extrae el value de un slot (prioridad por orden), si existe."""
    slots = intent.get("slots") or {}
    for name in slot_names:
        s = slots.get(name) or {}
        val = s.get("value")
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return ""


@app.route("/alexa", methods=["POST", "GET", "HEAD"])
def alexa_webhook():
    # Para checks de endpoint (algunos servicios hacen GET/HEAD)
    if request.method in ("GET", "HEAD"):
        return "OK", 200

    # JSON de Alexa
    data = request.get_json(silent=True) or {}
    req = data.get("request", {}) or {}
    rtype = req.get("type", "")

    # Debug mínimo (aparece en logs de Render)
    try:
        print("=== INCOMING REQUEST TYPE:", rtype)
        print("Intent name:", (req.get("intent") or {}).get("name"))
    except Exception:
        pass

    # 1) Cuando abre la skill
    if rtype == "LaunchRequest":
        resp = alexa_response(
            "Hola Robert. Decime una frase empezando con: pregunta. "
            "Por ejemplo: pregunta cuánto es dos más dos.",
            end_session=False,
            reprompt="Decí: pregunta... y tu consulta.",
        )
        return jsonify(resp), 200

    # 2) IntentRequest (intents custom)
    if rtype == "IntentRequest":
        intent = req.get("intent", {}) or {}
        name = intent.get("name", "")

        # Tu intent principal
        if name == "PreguntaIntent":
            # IMPORTANTE: tu slot en el modelo se llama "consulta"
            # Igual dejamos fallback por si un día lo renombrás
            texto = get_slot_value(intent, ["consulta", "texto", "query", "pregunta"])

            if not texto:
                resp = alexa_response(
                    "No capturé tu consulta. Probá diciendo: pregunta seguida de tu consulta.",
                    end_session=False,
                    reprompt="Decí: pregunta... y tu consulta.",
                )
                return jsonify(resp), 200

            # Respuesta “bridge” (confirmación)
            resp = alexa_response(
                f"Perfecto. Capturé: {texto}.",
                end_session=False,
                reprompt="Decí otra pregunta.",
            )
            return jsonify(resp), 200

        # Built-ins comunes: cancelar / stop
        if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
            return jsonify(alexa_response("Listo, cierro.", end_session=True)), 200

        # Cualquier otro intent
        return jsonify(
            alexa_response(
                "Entendí la intención, pero no la tengo configurada todavía.",
                end_session=False,
                reprompt="Decí: pregunta... y tu consulta.",
            )
        ), 200

    # 3) Fallback por si llega otra cosa
    return jsonify(
        alexa_response(
            "Estoy viva, pero no entendí. Decí: pregunta... y tu consulta.",
            end_session=False,
            reprompt="Decí: pregunta... y tu consulta.",
        )
    ), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
