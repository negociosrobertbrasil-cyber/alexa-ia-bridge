from flask import Flask, request, jsonify
import os

app = Flask(__name__)

@app.get("/")
def home():
    return "OK - Alexa IA Bridge funcionando", 200


def alexa_response(text, end_session=False, reprompt=None):
    r = {
        "version": "1.0",
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
    if request.method in ("GET", "HEAD"):
        return ("OK", 200)

    data = request.get_json(silent=True) or {}
    req = data.get("request", {})
    rtype = req.get("type", "")

    # 1) Cuando abrís la skill
    if rtype == "LaunchRequest":
        resp = alexa_response(
            "Hola Robert. Decime una frase empezando con: pregunta. Por ejemplo: pregunta cuánto es dos más dos.",
            end_session=False,
            reprompt="Decí: pregunta... y lo que quieras que procese."
        )
        return jsonify(resp), 200

    # 2) Intent custom: Captura lo que decís en un slot
    if rtype == "IntentRequest":
        intent = req.get("intent", {})
        name = intent.get("name", "")

        if name == "PreguntaIntent":
            slots = intent.get("slots", {}) or {}
            texto = (slots.get("texto", {}) or {}).get("value", "")
            if not texto:
                resp = alexa_response(
                    "No te escuché el texto. Probá diciendo: pregunta seguido de tu consulta.",
                    end_session=False,
                    reprompt="Decí: pregunta... y tu consulta."
                )
            else:
                resp = alexa_response(
                    f"Perfecto. Capturé: {texto}. Cuando quieras, hago que la IA te responda eso.",
                    end_session=False,
                    reprompt="Decí otra: pregunta... lo que quieras."
                )
            return jsonify(resp), 200

        if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
            return jsonify(alexa_response("Listo, cierro.", end_session=True)), 200

    # 3) Fallback
    return jsonify(alexa_response("Estoy viva, pero no entendí. Decí: pregunta ...", end_session=False)), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
