"""Tells YOUR website that an order was paid.

POST {WEBHOOK_URL}
Headers:
    Content-Type: application/json
    X-FamPay-Event: order.paid
    X-FamPay-Timestamp: <unix seconds>
    X-FamPay-Delivery: <unique id, same for every retry of one event>
    X-FamPay-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>" with WEBHOOK_SECRET>
Body: {"event": "order.paid", "order": {...}}

Retries 5 times with back-off in a background thread. Your endpoint should
reply 2xx and must be idempotent (the same order may arrive twice).
Redirects are never followed, so the signed payload can't be bounced elsewhere.
"""

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import urllib.request

from .config import Settings
from .db import Database

log = logging.getLogger("fampay.webhook")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return "sha256=" + mac


class WebhookSender:
    def __init__(self, settings: Settings, db: Database, opener=_opener.open):
        self.s = settings
        self.db = db
        self._open = opener

    def _send(self, order: dict) -> None:
        body = json.dumps({"event": "order.paid", "order": order}, separators=(",", ":")).encode()
        delivery = "dlv_" + secrets.token_hex(12)
        delay = 2
        for attempt in range(1, 6):
            ts = str(int(time.time()))
            req = urllib.request.Request(
                self.s.webhook_url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "fampay-autopay/1.0",
                    "X-FamPay-Event": "order.paid",
                    "X-FamPay-Timestamp": ts,
                    "X-FamPay-Delivery": delivery,
                    "X-FamPay-Signature": sign(self.s.webhook_secret, ts, body),
                },
            )
            try:
                with self._open(req, timeout=15) as resp:
                    if 200 <= resp.status < 300:
                        self.db.execute("UPDATE orders SET webhook_state='sent' WHERE id=?", (order["order_id"],))
                        return
                    log.warning("webhook got HTTP %s (attempt %d)", resp.status, attempt)
            except Exception as exc:
                log.warning("webhook failed (attempt %d): %s", attempt, type(exc).__name__)
            time.sleep(delay)
            delay *= 3
        self.db.execute("UPDATE orders SET webhook_state='failed' WHERE id=?", (order["order_id"],))

    def __call__(self, order: dict) -> None:
        if not self.s.webhook_url:
            return
        if not self.s.webhook_secret:  # never send an unsigned webhook
            log.error("WEBHOOK_URL is set but WEBHOOK_SECRET is empty – webhook not sent")
            return
        order = {k: v for k, v in order.items() if k != "webhook_state"}
        threading.Thread(target=self._send, args=(order,), daemon=True).start()
