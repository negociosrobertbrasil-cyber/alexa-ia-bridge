from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/")
def home():
    return "OK - Alexa IA Bridge funcionando"

@app.route("/alexa", methods=["POST"])
def alexa():
    data = request.json  # Alexa manda JSON por POST

    return jsonify({
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": "Hola Robert, el puente está funcionando correctamente."
            },
            "shouldEndSession": True
        }
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
    
