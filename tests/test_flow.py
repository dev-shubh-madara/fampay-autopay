import hashlib
import hmac
import json

from app.payments.emailparse import parse_payment
from app.webhook import WebhookSender, sign
from tests.conftest import ADMIN, SECRET, build_email, make_settings


# ── parser ────────────────────────────────────────────────────────────────
def test_parser_basics(tmp_path):
    s = make_settings(tmp_path)
    p = parse_payment("Payment received", "You received ₹29.00 from Rahul Kumar. UTR 412345678901", s)
    assert (p.direction, p.amount_paise, p.utr, p.payer) == ("in", 2900, "412345678901", "Rahul Kumar")
    assert parse_payment("", "You paid ₹29 to Swiggy. UTR 111111111111", s).direction == "out"
    assert parse_payment("", "received ₹29. phone 9876543210", s).utr is None


# ── orders ────────────────────────────────────────────────────────────────
def test_public_order_uses_product_price(env):
    o = env.order(product="pro")
    assert o["status"] == "pending" and o["amount_inr"] == 79
    assert o["pay"]["upi_link"].startswith("upi://pay?pa=shop%40fam") and "am=79.00" in o["pay"]["upi_link"]


def test_custom_amount_needs_secret(env):
    r = env.client.post("/api/orders", json={"amount_inr": 5})
    assert r.status_code == 403
    r = env.client.post("/api/orders", json={"amount_inr": 5, "metadata": {"cart": 7}}, headers=SECRET)
    assert r.status_code == 201 and r.json()["amount_inr"] == 5 and r.json()["metadata"] == {"cart": 7}
    assert env.client.post("/api/orders", json={"amount_inr": 5}, headers={"X-Api-Secret": "nope"}).status_code == 401


def test_public_cannot_set_price_or_metadata(env):
    o = env.order(product="basic", amount_inr=1, metadata={"x": 1})
    assert o["amount_inr"] == 29 and "metadata" not in o


def test_public_orders_can_be_disabled(make_env):
    e = make_env(allow_public_orders=False)
    assert e.client.post("/api/orders", json={"product": "basic"}).status_code == 403
    assert e.client.post("/api/orders", json={"product": "basic"}, headers=SECRET).status_code == 201


# ── the main flow: UTR first, e-mail second ───────────────────────────────
def test_claim_then_email_marks_paid(env):
    o = env.order()
    r = env.client.post(f"/api/orders/{o['order_id']}/claim", json={"utr": "4123 4567 8901"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert env.email()["result"] == "matched"
    assert env.client.get(f"/api/orders/{o['order_id']}").json()["status"] == "paid"
    assert env.paid_events and env.paid_events[0]["order_id"] == o["order_id"]


def test_email_then_claim_marks_paid(env):
    assert env.email()["result"] == "unmatched"
    o = env.order()
    r = env.client.post(f"/api/orders/{o['order_id']}/claim", json={"utr": "412345678901"})
    assert r.json()["status"] == "paid"
    assert len(env.paid_events) == 1


def test_utr_can_only_be_used_once(env):
    env.email()
    a, b = env.order(customer_ref="a"), env.order(customer_ref="b")
    assert env.client.post(f"/api/orders/{a['order_id']}/claim", json={"utr": "412345678901"}).json()["status"] == "paid"
    r = env.client.post(f"/api/orders/{b['order_id']}/claim", json={"utr": "412345678901"})
    assert r.status_code == 409


def test_pending_utr_cannot_be_attached_twice(env):
    a, b = env.order(customer_ref="a"), env.order(customer_ref="b")
    env.client.post(f"/api/orders/{a['order_id']}/claim", json={"utr": "999988887777"})
    r = env.client.post(f"/api/orders/{b['order_id']}/claim", json={"utr": "999988887777"})
    assert r.status_code == 409


def test_underpaid_is_rejected(env):
    env.email(body="You received ₹10.00 from X. UTR 555566667777.")
    o = env.order()
    r = env.client.post(f"/api/orders/{o['order_id']}/claim", json={"utr": "555566667777"})
    assert r.status_code == 402


def test_same_email_twice_is_ignored(env):
    o = env.order()
    env.client.post(f"/api/orders/{o['order_id']}/claim", json={"utr": "412345678901"})
    assert env.email()["result"] == "matched"
    assert env.email()["result"] == "already_processed"
    assert env.email(message_id="<other@mail>")["result"] == "duplicate_payment"


# ── fake e-mails must not work ────────────────────────────────────────────
def test_fake_sender_and_unauthenticated_mail_rejected(env):
    assert env.email(sender="fampay@gmail.com")["result"] == "ignored_sender"
    assert env.email(auth=None, message_id="<x1@m>")["result"] == "rejected_unauthenticated"
    assert env.email(auth="mx.google.com; dkim=pass header.i=@evil.com", message_id="<x2@m>")["result"] == "rejected_unauthenticated"
    assert env.email(body="You paid ₹29 to Shop. UTR 412345678901", message_id="<x3@m>")["result"] == "ignored_outgoing"


def test_invalid_utr(env):
    o = env.order()
    assert env.client.post(f"/api/orders/{o['order_id']}/claim", json={"utr": "12345"}).status_code == 422


# ── admin ─────────────────────────────────────────────────────────────────
def test_admin(env):
    o = env.order()
    assert env.client.get("/admin/orders").status_code == 401
    assert env.client.get("/admin/orders", headers=ADMIN).json()[0]["order_id"] == o["order_id"]
    assert env.client.post(f"/admin/orders/{o['order_id']}/approve", headers=ADMIN).json()["status"] == "paid"
    raw = build_email().decode()
    r = env.client.post("/admin/parse-email", content=raw, headers=ADMIN | {"Content-Type": "text/plain"})
    assert r.json()["utr"] == "412345678901" and r.json()["auth_ok"] is True


def test_admin_disabled_without_token(make_env):
    e = make_env(admin_token="")
    assert e.client.get("/admin/orders", headers=ADMIN).status_code == 404


# ── webhook signature ─────────────────────────────────────────────────────
def test_webhook_is_signed(env, tmp_path):
    s = make_settings(tmp_path, webhook_url="https://shop.example.com/hook", webhook_secret="whsec")
    sent = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def opener(req, timeout):
        sent["req"] = req
        return Resp()

    o = env.order()
    WebhookSender(s, env.db, opener)._send(env.service.get_order(o["order_id"]))
    req = sent["req"]
    ts, body = req.get_header("X-fampay-timestamp"), req.data
    expected = "sha256=" + hmac.new(b"whsec", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert req.get_header("X-fampay-signature") == expected == sign("whsec", ts, body)
    assert json.loads(body)["order"]["order_id"] == o["order_id"]
    assert env.db.fetchone("SELECT webhook_state FROM orders WHERE id=?", (o["order_id"],))[0] == "sent"


def test_checkout_page_served(env):
    assert "Choose a plan" in env.client.get("/checkout").text
