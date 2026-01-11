from flask import Flask, request, jsonify
import os
import logging

app = Flask(__name__)

# Logging simple (Render muestra esto en Logs)
logging.basicConfig(level=logging.INFO)


@app.get("/")
def home():
    return "OK - Alexa IA Bridge funcionando", 200


def alexa_response(text, end_session=False, reprompt=None):
    """
    Construye una respuesta válida para Alexa Skills Kit.
    """
    r = {
        "version": "1.0",
        "sessionAttributes": {},  # ✅ recomendado para evitar rarezas en sesiones
        "response": {
            "outputSpeech": {"type": "PlainText", "text": text},
            "shouldEndSession": end_session
        }
    }

    if reprompt:
        r["response"]["reprompt"] = {
            "outputSpeech": {"type": "PlainText", "text": reprompt}
        }

    return r


@app.route("/alexa", methods=["POST", "GET", "HEAD"])
def alexa_webhook():
    # Health checks rápidos
    if request.method in ("GET", "HEAD"):
        return "OK", 200

    data = request.get_json(silent=True) or {}
    req = data.get("request", {}) or {}
    rtype = req.get("type", "")

    logging.info("Alexa request type: %s", rtype)

    # 1) Cuando abrís la skill ("abre asistente ia")
    if rtype == "LaunchRequest":
        resp = alexa_response(
            "Hola Robert. Decime una frase empezando con: pregunta. "
            "Por ejemplo: pregunta cuánto es dos más dos.",
            end_session=False,
            reprompt="Decí: pregunta... y luego tu consulta."
        )
        return jsonify(resp), 200

    # 2) Cuando Alexa detecta un Intent
    if rtype == "IntentRequest":
        intent = (req.get("intent", {}) or {})
        name = intent.get("name", "")
        logging.info("Intent name: %s", name)

        # Stop / Cancel
        if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
            return jsonify(alexa_response("Listo, cierro.", end_session=True)), 200

        # Fallback (cuando no matchea bien)
        if name == "AMAZON.FallbackIntent":
            return jsonify(
                alexa_response(
                    "No te entendí. Decí: pregunta... y luego tu consulta.",
                    end_session=False,
                    reprompt="Decí: pregunta... y luego tu consulta."
                )
            ), 200

        # Tu intent custom
        if name == "PreguntaIntent":
            slots = (intent.get("slots", {}) or {})
            # ✅ tu slot se llama "consulta"
            texto = ((slots.get("consulta", {}) or {}).get("value", "") or "").strip()

            logging.info("Slot consulta: %s", texto)

            if not texto:
                return jsonify(
                    alexa_response(
                        "No capté la consulta. Probá diciendo: pregunta... y luego tu consulta.",
                        end_session=False,
                        reprompt="Decí: pregunta... y luego tu consulta."
                    )
                ), 200

            # Respuesta simple (fase 1). Después lo conectamos a IA real.
            return jsonify(
                alexa_response(
                    f"Perfecto. Capté: {texto}. Cuando quieras, hago que la IA te responda eso.",
                    end_session=False,
                    reprompt="Decí otra: pregunta... lo que quieras."
                )
            ), 200

        # Intent desconocido
        return jsonify(
            alexa_response(
                "Estoy viva, pero no entendí ese intent. Decí: pregunta... y luego tu consulta.",
                end_session=False,
                reprompt="Decí: pregunta... y luego tu consulta."
            )
        ), 200

    # 3) Otros tipos (SessionEndedRequest, etc.)
    return jsonify(alexa_response("Ok.", end_session=True)), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
