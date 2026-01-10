import os
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.get("/")
def home():
    return "OK - Alexa IA Bridge funcionando"

@app.post("/alexa")
def alexa():
    response = {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": "Hola Robert. Ya estoy conectada a tu servidor y lista para hablar con la IA."
            },
            "shouldEndSession": True
        }
    }
    return jsonify(response)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
