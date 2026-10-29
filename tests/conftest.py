import hashlib
import os
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())  # module-level app must not touch ./data
os.environ["FAMPAY_NO_APP"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app

ADMIN_TOKEN = "adm_Q8v2kL9xZp4Rt7Wm3Nc6Yb1H"
API_SECRET = "sec_J5s8Df2Gh7Kl0Pq3Rv6Xz9Ab"
ADMIN = {"Authorization": "Bearer " + ADMIN_TOKEN}
SECRET = {"X-Api-Secret": API_SECRET}


def make_settings(tmp_path, **over) -> Settings:
    base = dict(
        _env_file=None,
        data_dir=str(tmp_path),
        api_secret=API_SECRET,
        admin_token=ADMIN_TOKEN,
        upi_id="shop@fam",
        payee_name="Test Shop",
        products="basic:29:Basic plan,pro:79:Pro plan",
        imap_enabled=False,
    )
    base.update(over)
    return Settings(**base)


def build_email(
    *,
    subject="Payment received",
    body="You received ₹29.00 from Rahul Kumar. UTR 412345678901.",
    sender="no-reply@fampay.in",
    message_id=None,
    auth="mx.google.com; dkim=pass header.i=@fampay.in header.s=s1; spf=pass; dmarc=pass (p=NONE) header.from=fampay.in",
    html=False,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = f"FamPay <{sender}>"
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id or f"<{hashlib.md5((subject + body + sender).encode()).hexdigest()}@mail>"
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    if auth is not None:
        msg["Authentication-Results"] = auth
    if html:
        msg.set_content("fallback")
        msg.add_alternative(f"<html><body><p>{body}</p></body></html>", subtype="html")
    else:
        msg.set_content(body)
    return bytes(msg)


class Env:
    def __init__(self, tmp_path, **over):
        self.settings = make_settings(tmp_path, **over)
        self.db = Database(":memory:")
        self.app = create_app(self.settings, self.db, start_poller=False)
        self.client = TestClient(self.app)
        self.service = self.app.state.service
        self.paid_events: list[dict] = []
        self.service.on_paid = self.paid_events.append  # capture instead of HTTP webhook

    def order(self, **body):
        body = {"product": "basic", **body} if "amount_inr" not in body else body
        r = self.client.post("/api/orders", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    def email(self, **kw):
        return self.service.handle_email(build_email(**kw))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def make_env(tmp_path):
    return lambda **over: Env(tmp_path, **over)
