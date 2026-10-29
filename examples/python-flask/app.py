"""Example: your Flask website talking to FamPay Auto-Pay.

    pip install flask requests
    FAMPAY_API=http://localhost:8000 API_SECRET=... WEBHOOK_SECRET=... python app.py
"""

import hashlib
import hmac
import os
import time

import requests
from flask import Flask, abort, jsonify, request

FAMPAY_API = os.getenv("FAMPAY_API", "http://localhost:8000")
API_SECRET = os.environ["API_SECRET"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

app = Flask(__name__)


@app.post("/buy")
def buy():
    user_id = "user-123"  # from your own login/session
    r = requests.post(
        f"{FAMPAY_API}/api/orders",
        headers={"X-Api-Secret": API_SECRET},
        json={
            "amount_inr": 149,
            "description": "Course: Python Basics",
            "customer_ref": user_id,
            "metadata": {"user_id": user_id, "course_id": "py-101"},
        },
        timeout=15,
    )
    order = r.json()
    if not r.ok:
        return jsonify(order), r.status_code
    return jsonify(order_id=order["order_id"], amount_inr=order["amount_inr"], pay=order["pay"])


@app.post("/webhook")
def webhook():
    ts = request.headers.get("X-FamPay-Timestamp", "")
    sig = request.headers.get("X-FamPay-Signature", "")
    body = request.get_data()  # RAW bytes – do not re-serialise the JSON
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected) or abs(time.time() - int(ts or 0)) > 600:
        abort(401)

    data = request.get_json()
    if data["event"] == "order.paid":
        order = data["order"]
        # Idempotent: ignore if you already handled order["order_id"]
        print("PAID", order["order_id"], order["amount_inr"], order.get("metadata"))
    return "", 200


if __name__ == "__main__":
    app.run(port=3000)
