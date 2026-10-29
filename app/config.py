"""All settings come from environment variables (or a local .env file).

Nothing personal is hard-coded here. Every value that identifies YOU
(UPI ID, Gmail, app password, secrets) must be set in your own .env.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # ── Server ────────────────────────────────────────────────────────────
    data_dir: str = "data"  # SQLite file lives here. Mount a volume on cloud hosts.
    cors_origins: str = "*"  # your website origin(s), e.g. https://shop.example.com
    expose_docs: bool = True  # /docs (Swagger UI)
    # Only believe CF-Connecting-IP / X-Forwarded-For when you really run behind
    # Cloudflare or a platform proxy. Otherwise clients could fake their IP to
    # dodge rate limits.
    trust_proxy_headers: bool = False
    # Refuse to start with missing/weak secrets. Only set false for local testing.
    strict_secrets: bool = True
    max_body_bytes: int = 64 * 1024  # request size cap (admin parse-email gets 2 MB)

    # ── Secrets ───────────────────────────────────────────────────────────
    # Your website's BACKEND sends this as  X-Api-Secret  to create orders
    # with any amount. Never put it in browser JavaScript.
    api_secret: str = ""
    admin_token: str = ""  # empty = /admin endpoints disabled

    # ── What buyers pay ───────────────────────────────────────────────────
    upi_id: str = ""  # YOUR FamPay UPI ID, e.g. yourname@fam
    payee_name: str = ""  # name shown in the buyer's UPI app
    # Fixed-price products the browser may order WITHOUT the api secret.
    # Format: id:price_in_rupees:Title  (comma separated)
    products: str = "basic:29:Basic plan,pro:79:Pro plan"
    allow_public_orders: bool = True  # browser may create orders for the products above
    order_ttl_minutes: int = 60
    order_grace_minutes: int = 360  # payment e-mails can arrive late
    order_prefix: str = "PAY"  # prefix of the short order code shown to the buyer
    max_pending_per_customer: int = 5
    max_claims_per_order: int = 6  # wrong-UTR attempts before an order is locked
    # A payment must arrive after the order was created (minus this tolerance),
    # so an old unrelated receipt can't be re-used for a new order.
    payment_before_order_tolerance_minutes: int = 30
    min_amount_inr: float = 1
    max_amount_inr: float = 100000
    purge_unpaid_after_days: int = 30  # delete old unpaid orders (paid ones and used UTRs are kept forever)

    # ── Webhook to your website when an order is paid ─────────────────────
    webhook_url: str = ""  # e.g. https://shop.example.com/api/fampay-webhook
    webhook_secret: str = ""  # used to sign the webhook (HMAC-SHA256)

    # ── IMAP payment verification (Gmail that receives FamPay e-mails) ────
    imap_enabled: bool = False
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""  # your Gmail address
    imap_password: str = ""  # Gmail App Password (NOT your normal password)
    imap_folder: str = "INBOX"
    imap_poll_seconds: int = 20
    imap_lookback_days: int = 2  # only used on the very first run
    # Only e-mails whose From matches one of these are trusted.
    # An entry with "@" is an exact address, otherwise a domain.
    payment_senders: str = "fampay.in,famapp.in"
    # Require DMARC/DKIM pass from YOUR mail server. Without it anyone could
    # e-mail you a fake "payment received". Keep this true in production.
    require_email_auth: bool = True
    payment_in_keywords: str = "received,credited,paid you,sent you,money in,payment from,deposit"
    payment_out_keywords: str = "debited,you paid,you sent,paid to,sent to,payment to,refund,failed,declined,reversed"
    utr_pattern: str = r"[0-9]{12}"  # shape of a UPI reference (UTR) number

    def security_problems(self) -> list[str]:
        """Things that make a deployment unsafe. Empty list = OK."""
        bad: list[str] = []

        def weak(v: str) -> bool:
            return len(v) < 24 or "change-me" in v.lower() or len(set(v)) < 8

        if self.api_secret and weak(self.api_secret):
            bad.append("API_SECRET is too weak (use 24+ random characters)")
        if self.admin_token and weak(self.admin_token):
            bad.append("ADMIN_TOKEN is too weak (use 24+ random characters)")
        if self.api_secret and self.admin_token and self.api_secret == self.admin_token:
            bad.append("API_SECRET and ADMIN_TOKEN must be different")
        if self.webhook_url:
            if not self.webhook_secret or weak(self.webhook_secret):
                bad.append("WEBHOOK_URL is set but WEBHOOK_SECRET is missing or weak")
            if not (self.webhook_url.startswith("https://") or self.webhook_url.startswith(("http://localhost", "http://127.0.0.1"))):
                bad.append("WEBHOOK_URL must use https://")
        if self.imap_enabled and not self.require_email_auth:
            bad.append("REQUIRE_EMAIL_AUTH=false lets anyone fake a payment e-mail")
        if self.imap_enabled and not (self.imap_user and self.imap_password):
            bad.append("IMAP_ENABLED=true but IMAP_USER / IMAP_PASSWORD are missing")
        if self.upi_id and "@" not in self.upi_id:
            bad.append("UPI_ID does not look like a UPI ID (name@bank)")
        return bad

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]

    @property
    def payment_sender_list(self) -> list[str]:
        return _csv(self.payment_senders)

    @property
    def in_keywords(self) -> list[str]:
        return _csv(self.payment_in_keywords)

    @property
    def out_keywords(self) -> list[str]:
        return _csv(self.payment_out_keywords)

    @property
    def product_map(self) -> dict[str, tuple[int, str]]:
        """{id: (price_paise, title)}"""
        out: dict[str, tuple[int, str]] = {}
        for raw in self.products.split(","):
            parts = [p.strip() for p in raw.split(":")]
            if len(parts) < 2 or not parts[0]:
                continue
            try:
                paise = round(float(parts[1]) * 100)
            except ValueError:
                continue
            if paise > 0:
                out[parts[0].lower()] = (paise, parts[2] if len(parts) > 2 and parts[2] else parts[0])
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
