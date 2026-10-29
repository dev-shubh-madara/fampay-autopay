"""FamPay Auto-Pay backend – FastAPI app.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import hmac
import logging
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import Database
from .errors import ApiError
from .payments.emailparse import parse_email_bytes, parse_payment
from .payments.imap_poller import ImapPoller
from .payments.service import PaymentService
from .webhook import WebhookSender

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fampay")
STATIC = os.path.join(os.path.dirname(__file__), "static")


class OrderIn(BaseModel):
    product: str | None = Field(None, description="Product id from PRODUCTS, e.g. 'pro'")
    amount_inr: float | None = Field(None, description="Custom amount – only with X-Api-Secret")
    description: str | None = None
    customer_ref: str | None = Field(None, description="Your user id / email / cart id")
    metadata: dict | None = Field(None, description="Anything you want back in the webhook (backend only)")


class ClaimIn(BaseModel):
    utr: str


class RateLimiter:
    """Small in-memory sliding window per key."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: int = 60) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                raise ApiError(429, "rate_limited", "Too many requests. Slow down and try again in a minute.")
            q.append(now)


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def create_app(settings: Settings | None = None, db: Database | None = None, start_poller: bool = True) -> FastAPI:
    s = settings or get_settings()
    db = db or Database(os.path.join(s.data_dir, "payments.db"))
    service = PaymentService(db, s)
    service.on_paid = WebhookSender(s, db)
    poller = ImapPoller(s, db, service) if s.imap_enabled else None
    service.poller = poller
    limiter = RateLimiter()

    @asynccontextmanager
    async def lifespan(_app):
        if not s.upi_id:
            log.warning("UPI_ID is empty – buyers will not see where to pay")
        if poller and start_poller:
            if not (s.imap_user and s.imap_password):
                log.error("IMAP_ENABLED=true but IMAP_USER / IMAP_PASSWORD are missing")
            else:
                poller.start()
        elif not s.imap_enabled:
            log.warning("IMAP is disabled – payments will only be confirmed by /admin approve")
        yield
        if poller:
            poller.stop()

    app = FastAPI(
        title="FamPay Auto-Pay",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if s.expose_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if s.expose_docs else None,
    )
    app.state.service, app.state.poller, app.state.db = service, poller, db

    origins = s.cors_origin_list or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Api-Secret"],
    )

    @app.exception_handler(ApiError)
    async def _api_error(_req, exc: ApiError):
        return JSONResponse(exc.body(), status_code=exc.status)

    def ip(req: Request) -> str:
        return req.headers.get("cf-connecting-ip") or (req.client.host if req.client else "?")

    def require_admin(token: str | None) -> None:
        if not s.admin_token:
            raise ApiError(404, "not_found", "Admin endpoints are disabled (set ADMIN_TOKEN).")
        if not _same(token or "", "Bearer " + s.admin_token):
            raise ApiError(401, "unauthorized", "Bad admin token.")

    # ── public ────────────────────────────────────────────────────────────
    @app.get("/health")
    def health():
        return {
            "ok": True,
            "imap": {
                "enabled": s.imap_enabled,
                "last_ok_at": poller.last_ok_at if poller else None,
                "last_error": poller.last_error if poller else None,
            },
        }

    @app.get("/api/products")
    def products():
        return {
            "upi_configured": bool(s.upi_id),
            "products": [
                {"id": pid, "title": title, "amount_inr": paise / 100}
                for pid, (paise, title) in s.product_map.items()
            ],
        }

    @app.post("/api/orders", status_code=201)
    def create_order(body: OrderIn, request: Request, x_api_secret: str | None = Header(None)):
        trusted = _same(x_api_secret or "", s.api_secret)
        if x_api_secret and not trusted:
            raise ApiError(401, "bad_secret", "X-Api-Secret is wrong.")
        if not trusted:
            limiter.hit("order:" + ip(request), 10)
        return service.create_order(
            product=body.product,
            amount_inr=body.amount_inr,
            description=body.description,
            customer_ref=body.customer_ref,
            metadata=body.metadata if trusted else None,
            trusted=trusted,
        )

    @app.get("/api/orders/{order_id}")
    def get_order(order_id: str, request: Request):
        limiter.hit("get:" + ip(request), 120)
        return service.get_order(order_id)

    @app.post("/api/orders/{order_id}/claim")
    def claim(order_id: str, body: ClaimIn, request: Request):
        limiter.hit("claim:" + ip(request), 10)
        return service.claim(order_id, body.utr)

    @app.get("/checkout", include_in_schema=False)
    def checkout_page():
        return FileResponse(os.path.join(STATIC, "checkout.html"))

    # ── admin (Authorization: Bearer <ADMIN_TOKEN>) ───────────────────────
    @app.get("/admin/orders")
    def admin_orders(status: str | None = None, limit: int = 50, authorization: str | None = Header(None)):
        require_admin(authorization)
        return service.list_orders(status, min(limit, 500))

    @app.get("/admin/payments")
    def admin_payments(status: str | None = None, limit: int = 50, authorization: str | None = Header(None)):
        require_admin(authorization)
        return service.list_payments(status, min(limit, 500))

    @app.get("/admin/emails")
    def admin_emails(limit: int = 50, authorization: str | None = Header(None)):
        require_admin(authorization)
        return service.list_emails(min(limit, 500))

    @app.post("/admin/orders/{order_id}/approve")
    def admin_approve(order_id: str, authorization: str | None = Header(None)):
        require_admin(authorization)
        return service.admin_approve(order_id)

    @app.post("/admin/orders/{order_id}/cancel")
    def admin_cancel(order_id: str, authorization: str | None = Header(None)):
        require_admin(authorization)
        return service.cancel(order_id)

    @app.post("/admin/poll")
    def admin_poll(authorization: str | None = Header(None)):
        require_admin(authorization)
        if not poller:
            raise ApiError(400, "imap_disabled", "IMAP is disabled.")
        return {"handled": poller.poll_once()}

    @app.post("/admin/parse-email")
    def admin_parse(raw: str = Body(..., media_type="text/plain"), authorization: str | None = Header(None)):
        """Paste a full raw e-mail (Gmail: ⋮ → Show original → Copy) to see what the parser reads."""
        require_admin(authorization)
        info = parse_email_bytes(raw.encode(), s)
        p = parse_payment(info.subject, info.text, s)
        return {
            "from": info.from_addr,
            "subject": info.subject,
            "auth_ok": info.auth_ok,
            "direction": p.direction,
            "amount_inr": None if p.amount_paise is None else p.amount_paise / 100,
            "utr": p.utr,
            "payer": p.payer,
            "text_preview": info.text[:800],
        }

    return app


app = create_app()
