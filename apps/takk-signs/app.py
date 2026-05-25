import os
from datetime import datetime, timezone

from flask import Flask, jsonify
from google.cloud import firestore

app = Flask(__name__)
db = firestore.Client()


@app.route("/")
def index():
    return jsonify(
        message="Hello from Cloud Run",
        service="takk-signs",
        revision=os.environ.get("K_REVISION", "local"),
    )


@app.route("/health")
def health():
    return jsonify(status="ok")


@app.route("/firestore-test")
def firestore_test():
    doc_ref = db.collection("smoke-test").document("latest")
    now = datetime.now(timezone.utc).isoformat()
    doc_ref.set({"pinged_at": now})
    snap = doc_ref.get()
    return jsonify(write=now, read_back=snap.to_dict())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
