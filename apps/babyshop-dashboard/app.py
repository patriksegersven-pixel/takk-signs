import os
from flask import Flask, jsonify, send_from_directory

app = Flask(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = "babyshop-dashboard.html"


@app.route("/")
@app.route("/" + PAGE)
def dashboard():
    return send_from_directory(HERE, PAGE)


@app.route("/health")
def health():
    return jsonify(status="ok", revision=os.environ.get("K_REVISION", "local"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
