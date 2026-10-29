"""FamPay Auto-Pay backend – FastAPI app.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import hmac
import io
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import segno
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

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
ORDER_ID_RE = re.compile(r"^[0-9a-f]{20}$")

# Browser hardening for every response.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cache-Control": "no-store",
}
CHECKOUT_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
API_CSP = "default-src 'none'; frame-ancestors 'none'"
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
    "frame-ancestors 'none'"
)


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str | None = Field(None, max_length=40, description="Product id from PRODUCTS, e.g. 'pro'")
    amount_inr: float | None = Field(None, description="Custom amount – only with X-Api-Secret")
    description: str | None = Field(None, max_length=200)
    customer_ref: str | None = Field(None, max_length=120, description="Your user id / cart id (backend only)")
    metadata: dict | None = Field(None, description="Returned in the webhook (backend only, < 4 KB)")


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    utr: str = Field(..., max_length=32)


class RateLimiter:
    """In-memory sliding window per key."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def hit(self, key: str, limit: int, window: int = 60) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_sweep > 300:  # forget idle keys so memory can't grow forever
                for k in [k for k, q in self._hits.items() if not q or q[-1] < now - 3600]:
                    del self._hits[k]
                self._last_sweep = now
            q = self._hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                raise ApiError(429, "rate_limited", "Too many requests. Please wait a minute and try again.")
            q.append(now)


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def create_app(settings: Settings | None = None, db: Database | None = None, start_poller: bool = True) -> FastAPI:
    s = settings or get_settings()
    problems = s.security_problems()
    if problems:
        msg = "Unsafe configuration:\n  - " + "\n  - ".join(problems)
        if s.strict_secrets:
            raise RuntimeError(msg + "\nFix your .env (see .env.example). Set STRICT_SECRETS=false only for local testing.")
        log.warning(msg)

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
        if not s.imap_enabled:
            log.warning("IMAP is disabled – payments will only be confirmed by /admin approve")
        service.housekeeping()
        if poller and start_poller:
            poller.start()
        yield
        if poller:
            poller.stop()

    app = FastAPI(
        title="FamPay Auto-Pay",
        version="1.1.0",
        lifespan=lifespan,
        docs_url="/docs" if s.expose_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if s.expose_docs else None,
    )
    app.state.service, app.state.poller, app.state.db = service, poller, db

    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origin_list or ["*"],
        allow_credentials=False,  # no cookies are ever used, so no CSRF surface
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],  # X-Api-Secret is deliberately NOT allowed from browsers
        max_age=600,
    )

    @app.middleware("http")
    async def harden(request: Request, call_next):
        limit = 2 * 1024 * 1024 if request.url.path == "/admin/parse-email" else s.max_body_bytes
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            return JSONResponse({"error": "too_large", "message": "Request body too large."}, status_code=413)
        if request.method in ("POST", "PUT", "PATCH") and length is None:
            body = await request.body()  # chunked upload: measure it ourselves
            if len(body) > limit:
                return JSONResponse({"error": "too_large", "message": "Request body too large."}, status_code=413)
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        path = request.url.path
        if path in ("/docs", "/openapi.json"):
            csp = DOCS_CSP
        elif path.startswith("/checkout"):
            csp = CHECKOUT_CSP
        else:
            csp = API_CSP
        response.headers.setdefault("Content-Security-Policy", csp)
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.exception_handler(ApiError)
    async def _api_error(_req, exc: ApiError):
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _bad_input(_req, exc: RequestValidationError):
        # Don't echo the submitted input back.
        fields = sorted({".".join(str(p) for p in e.get("loc", [])[1:]) or "body" for e in exc.errors()})
        return JSONResponse({"error": "invalid_request", "message": "Invalid field(s): " + ", ".join(fields)}, status_code=422)

    @app.exception_handler(Exception)
    async def _crash(_req, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse({"error": "server_error", "message": "Something went wrong."}, status_code=500)

    def ip(req: Request) -> str:
        if s.trust_proxy_headers:
            cf = req.headers.get("cf-connecting-ip")
            if cf:
                return cf.strip()
            xff = req.headers.get("x-forwarded-for")
            if xff:
                return xff.split(",")[-1].strip()  # the entry added by YOUR proxy
        return req.client.host if req.client else "?"

    def check_order_id(order_id: str) -> None:
        if not ORDER_ID_RE.fullmatch(order_id):
            raise ApiError(404, "order_not_found", "No such order.")

    def require_admin(req: Request, token: str | None) -> None:
        if not s.admin_token:
            raise ApiError(404, "not_found", "Not found.")
        if not _same(token or "", "Bearer " + s.admin_token):
            limiter.hit("adminfail:" + ip(req), 10, 600)  # 10 wrong tokens / 10 min, then locked out
            raise ApiError(401, "unauthorized", "Bad admin token.")

    def is_backend(req: Request, secret: str | None) -> bool:
        if not secret:
            return False
        if _same(secret, s.api_secret):
            return True
        limiter.hit("secretfail:" + ip(req), 10, 600)
        raise ApiError(401, "bad_secret", "X-Api-Secret is wrong.")

    # ── public ────────────────────────────────────────────────────────────
    @app.get("/health")
    def health():
        ok = poller is None or poller.last_error is None
        return {"ok": ok, "imap_enabled": s.imap_enabled}

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
        trusted = is_backend(request, x_api_secret)
        if not trusted:
            limiter.hit("order:" + ip(request), 10)
        return service.create_order(
            product=body.product,
            amount_inr=body.amount_inr,
            description=body.description,
            customer_ref=body.customer_ref,
            metadata=body.metadata,
            trusted=trusted,
        )

    @app.get("/api/orders/{order_id}")
    def get_order(order_id: str, request: Request, x_api_secret: str | None = Header(None)):
        trusted = is_backend(request, x_api_secret)
        if not trusted:
            limiter.hit("get:" + ip(request), 120)
        check_order_id(order_id)
        return service.get_order(order_id, private=trusted)

    @app.get("/api/orders/{order_id}/qr.svg")
    def order_qr(order_id: str, request: Request):
        limiter.hit("qr:" + ip(request), 60)
        check_order_id(order_id)
        order = service.get_order(order_id)
        link = (order.get("pay") or {}).get("upi_link")
        if not link:
            raise ApiError(404, "no_qr", "No QR for this order.")
        buf = io.BytesIO()
        segno.make(link, error="m").save(buf, kind="svg", scale=6, border=2, xmldecl=False)
        return Response(buf.getvalue(), media_type="image/svg+xml")

    @app.post("/api/orders/{order_id}/claim")
    def claim(order_id: str, body: ClaimIn, request: Request):
        limiter.hit("claim:" + ip(request), 10)
        check_order_id(order_id)
        return service.claim(order_id, body.utr)

    @app.get("/checkout", include_in_schema=False)
    def checkout_page():
        return FileResponse(os.path.join(STATIC, "checkout.html"))

    @app.get("/checkout.js", include_in_schema=False)
    def checkout_js():
        return FileResponse(os.path.join(STATIC, "checkout.js"), media_type="text/javascript")

    # ── admin (Authorization: Bearer <ADMIN_TOKEN>) ───────────────────────
    @app.get("/admin/status")
    def admin_status(request: Request, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        return {
            "imap": {
                "enabled": s.imap_enabled,
                "last_ok_at": poller.last_ok_at if poller else None,
                "last_error": poller.last_error if poller else None,
            },
            "warnings": s.security_problems(),
        }

    @app.get("/admin/orders")
    def admin_orders(request: Request, status: str | None = None, limit: int = 50, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        return service.list_orders(status, max(1, min(limit, 500)))

    @app.get("/admin/payments")
    def admin_payments(request: Request, status: str | None = None, limit: int = 50, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        return service.list_payments(status, max(1, min(limit, 500)))

    @app.get("/admin/emails")
    def admin_emails(request: Request, limit: int = 50, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        return service.list_emails(max(1, min(limit, 500)))

    @app.post("/admin/orders/{order_id}/approve")
    def admin_approve(order_id: str, request: Request, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        check_order_id(order_id)
        return service.admin_approve(order_id)

    @app.post("/admin/orders/{order_id}/cancel")
    def admin_cancel(order_id: str, request: Request, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        check_order_id(order_id)
        return service.cancel(order_id)

    @app.post("/admin/poll")
    def admin_poll(request: Request, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        if not poller:
            raise ApiError(400, "imap_disabled", "IMAP is disabled.")
        return {"handled": poller.poll_once()}

    @app.post("/admin/parse-email")
    async def admin_parse(request: Request, authorization: str | None = Header(None)):
        """Body = a full raw e-mail (Gmail: ⋮ → Show original → Copy). Shows what the parser reads.
        Read-only: nothing is stored or credited."""
        require_admin(request, authorization)
        raw = await request.body()
        info = parse_email_bytes(raw, s)
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


def _lazy_app():
    """Module-level app for `uvicorn app.main:app`."""
    return create_app()


app = _lazy_app() if os.getenv("FAMPAY_NO_APP") != "1" else None
