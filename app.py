from flask import Flask, request, jsonify, make_response

app = Flask(__name__)

# Health general
@app.get("/")
def home():
    return "OK - Alexa IA Bridge funcionando", 200

# Endpoint que Alexa llama
@app.route("/alexa", methods=["POST", "GET", "HEAD"])
def alexa_webhook():
    # Alexa/validadores a veces mandan HEAD/GET para verificar disponibilidad
    if request.method in ("GET", "HEAD"):
        return ("OK", 200)

    # POST real de Alexa
    data = request.get_json(silent=True) or {}

    # Respuesta mínima válida para Alexa
    response = {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": "Hola Robert, ya conecté con tu skill."},
            "shouldEndSession": True
        }
    }
    return jsonify(response)
10000)
    
