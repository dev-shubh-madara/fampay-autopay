"""Every protection the README promises has a test here."""

import ssl

import pytest

from app.main import create_app
from app.payments.imap_poller import ImapPoller
from tests.conftest import ADMIN, SECRET, Env, make_settings


# ── unsafe configs refuse to start ────────────────────────────────────────
@pytest.mark.parametrize("over", [
    {"api_secret": "change-me-long-random-string"},
    {"admin_token": "short"},
    {"api_secret": "x" * 30},  # long but no variety
    {"webhook_url": "https://shop.example.com/hook", "webhook_secret": ""},
    {"webhook_url": "http://shop.example.com/hook", "webhook_secret": "wh_Zx8Cv2Bn5Mq9Lk3Jh7Gf1Ds4"},
    {"imap_enabled": True, "imap_user": "a@b.c", "imap_password": "p", "require_email_auth": False},
    {"imap_enabled": True},
])
def test_unsafe_config_refuses_to_start(tmp_path, over):
    with pytest.raises(RuntimeError, match="Unsafe configuration"):
        create_app(make_settings(tmp_path, **over), start_poller=False)


def test_same_secret_for_admin_and_api_rejected(tmp_path):
    t = "Zx8Cv2Bn5Mq9Lk3Jh7Gf1Ds4Aa"
    with pytest.raises(RuntimeError):
        create_app(make_settings(tmp_path, api_secret=t, admin_token=t), start_poller=False)


# ── IMAP login is TLS-verified ────────────────────────────────────────────
def test_imap_uses_verified_tls(env):
    seen = {}

    class FakeIMAP:
        def __init__(self, host, port, ssl_context=None, timeout=None):
            seen["ctx"] = ssl_context

        def login(self, u, p):
            pass

    s = make_settings(env.settings.data_dir, imap_enabled=True, imap_user="a@b.c", imap_password="pw")
    ImapPoller(s, env.db, env.service, imap_factory=FakeIMAP)._connect()
    ctx = seen["ctx"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


# ── privacy: the public order view leaks nothing about your users ─────────
def test_public_view_hides_private_fields(env):
    r = env.client.post("/api/orders", headers=SECRET, json={
        "amount_inr": 49, "customer_ref": "user@example.com", "metadata": {"user": 42}})
    oid = r.json()["order_id"]
    env.client.post(f"/api/orders/{oid}/claim", json={"utr": "123412341234"})
    public = env.client.get(f"/api/orders/{oid}").json()
    for k in ("customer_ref", "metadata", "claimed_utr", "webhook_state"):
        assert k not in public
    private = env.client.get(f"/api/orders/{oid}", headers=SECRET).json()
    assert private["customer_ref"] == "user@example.com" and private["metadata"] == {"user": 42}


def test_browser_cannot_set_customer_ref(env):
    o = env.order(customer_ref="victim")
    row = env.db.fetchone("SELECT customer_ref FROM orders WHERE id=?", (o["order_id"],))
    assert row["customer_ref"] is None


# ── brute force & abuse ───────────────────────────────────────────────────
def test_utr_guessing_locks_the_order(make_env):
    e = make_env(max_claims_per_order=3)
    o = e.order()
    url = f"/api/orders/{o['order_id']}/claim"
    for i in range(3):
        assert e.client.post(url, json={"utr": f"10000000000{i}"}).status_code == 200
    assert e.client.post(url, json={"utr": "100000000009"}).status_code == 423
    # re-sending the same UTR doesn't burn attempts
    e2 = make_env(max_claims_per_order=2)
    o2 = e2.order()
    for _ in range(5):
        assert e2.client.post(f"/api/orders/{o2['order_id']}/claim", json={"utr": "222233334444"}).status_code == 200


def test_old_payment_cannot_pay_new_order(env):
    env.email()  # payment arrives "now"
    env.service._now = lambda: __import__("time").time() + 3 * 3600  # order created 3 h later
    o = env.order()
    r = env.client.post(f"/api/orders/{o['order_id']}/claim", json={"utr": "412345678901"})
    assert r.status_code == 409 and r.json()["error"] == "payment_before_order"


def test_wrong_secret_is_rejected_and_rate_limited(env):
    bad = {"X-Api-Secret": "nope"}
    codes = [env.client.post("/api/orders", json={"amount_inr": 5}, headers=bad).status_code for _ in range(12)]
    assert codes[0] == 401 and codes[-1] == 429


def test_admin_bruteforce_locked_out(env):
    codes = [env.client.get("/admin/orders", headers={"Authorization": "Bearer guess"}).status_code for _ in range(12)]
    assert codes[0] == 401 and codes[-1] == 429


def test_public_rate_limit(env):
    codes = [env.client.post("/api/orders", json={"product": "basic"}).status_code for _ in range(12)]
    assert codes[0] == 201 and 429 in codes


def test_spoofed_ip_header_ignored_by_default(env):
    codes = [env.client.post("/api/orders", json={"product": "basic"},
                             headers={"CF-Connecting-IP": f"1.2.3.{i}"}).status_code for i in range(12)]
    assert 429 in codes  # changing the header did NOT give a fresh limit


# ── input validation ──────────────────────────────────────────────────────
@pytest.mark.parametrize("amount", [0, -5, 0.001, 10**9, float("inf")])
def test_bad_amounts(env, amount):
    r = env.client.post("/api/orders", headers=SECRET | {"Content-Type": "application/json"},
                        content=('{"amount_inr": %s}' % ("1e999" if amount == float("inf") else amount)))
    assert r.status_code == 422


def test_unknown_fields_and_bad_ids(env):
    assert env.client.post("/api/orders", json={"product": "basic", "price": 1}).status_code == 422
    assert env.client.get("/api/orders/../../etc").status_code == 404
    assert env.client.get("/api/orders/zzzz").status_code == 404
    assert env.client.post("/api/orders/abc/claim", json={"utr": "123456789012"}).status_code == 404


def test_body_size_limit(env):
    r = env.client.post("/api/orders", content=b"x" * 200_000, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_errors_never_echo_input(env):
    r = env.client.post("/api/orders", json={"product": "<script>alert(1)</script>" * 3})
    assert "<script>" not in r.text


# ── browser hardening ─────────────────────────────────────────────────────
def test_security_headers(env):
    r = env.client.get("/checkout")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "<script>" not in r.text and "cdnjs" not in r.text  # no inline or third-party scripts
    api = env.client.get("/api/products")
    assert api.headers["Cache-Control"] == "no-store"


def test_cors_never_allows_secret_header(env):
    r = env.client.options("/api/orders", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-api-secret"})
    assert "x-api-secret" not in r.headers.get("access-control-allow-headers", "").lower()
    assert r.headers.get("access-control-allow-credentials") != "true"


def test_qr_is_self_hosted_svg(env):
    o = env.order()
    r = env.client.get(f"/api/orders/{o['order_id']}/qr.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and b"<svg" in r.content


def test_health_reveals_nothing(env):
    assert set(env.client.get("/health").json()) == {"ok", "imap_enabled"}
    assert env.client.get("/admin/status", headers=ADMIN).status_code == 200


def test_db_file_is_private(tmp_path):
    import os
    import stat
    from app.db import Database
    p = tmp_path / "x" / "payments.db"
    Database(str(p))
    assert stat.S_IMODE(os.stat(p).st_mode) & 0o077 == 0


def test_webhook_never_follows_redirects():
    import urllib.request
    from app.webhook import _NoRedirect
    req = urllib.request.Request("https://a.example/hook", data=b"{}", method="POST")
    assert _NoRedirect().redirect_request(req, None, 302, "Found", {}, "https://evil.example") is None
