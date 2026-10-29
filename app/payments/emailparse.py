"""Turn a raw payment e-mail into structured data, and decide if it is trustworthy.

The FamPay wording is configurable (see config.py) because mail templates
change. Use POST /admin/parse-email to see what the parser extracts from a
real e-mail and adjust PAYMENT_SENDERS / keywords if needed.
"""

import email
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from ..config import Settings

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid typos

_AMOUNT_RES = [
    re.compile(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I),
    re.compile(r"amount\s*(?:of)?\s*[:\-]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I),
]
_PAYER_RE = re.compile(
    r"(?:received|credited)?.{0,40}?\bfrom\s+([A-Za-z][A-Za-z .'\-]{1,40}?)(?=\s+(?:on|via|using|at|for|with|\(|,|\.|$)|[,.\n(])",
    re.I,
)


@dataclass
class EmailInfo:
    message_id: str
    from_addr: str
    subject: str
    text: str
    date_ts: int | None
    auth_ok: bool


@dataclass
class ParsedPayment:
    direction: str  # "in" | "out" | "unknown"
    amount_paise: int | None
    utr: str | None
    code: str | None
    payer: str | None


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    s = _Stripper()
    s.feed(html)
    return re.sub(r"[ \t\r\f\v]+", " ", "".join(s.parts))


def _body_text(msg: EmailMessage) -> str:
    plain, html = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except Exception:
            continue
        (plain if ctype == "text/plain" else html).append(str(content))
    parts = list(plain)
    if html:
        parts.append(html_to_text("\n".join(html)))
    return "\n".join(parts)


def sender_allowed(from_addr: str, allowed: list[str]) -> bool:
    addr = from_addr.lower().strip()
    if "@" not in addr:
        return False
    domain = addr.split("@", 1)[1]
    for entry in allowed:
        if "@" in entry:
            if addr == entry:
                return True
        elif domain == entry or domain.endswith("." + entry):
            return True
    return False


def _allowed_domains(allowed: list[str]) -> list[str]:
    return [e.split("@", 1)[1] if "@" in e else e for e in allowed]


def auth_passes(msg: EmailMessage, allowed: list[str]) -> bool:
    """Trust only the TOP Authentication-Results header: it was added by our own
    mail server. Lower ones could have been forged by the sender."""
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        return False
    top = str(headers[0]).lower()
    if "dmarc=pass" in top:
        return True
    domains = _allowed_domains(allowed)
    for m in re.finditer(r"dkim=pass[^;]*?header\.(?:d|i)=@?([a-z0-9.\-]+)", top):
        host = m.group(1)
        if any(host == d or host.endswith("." + d) for d in domains):
            return True
    return False


def parse_email_bytes(raw: bytes, settings: Settings) -> EmailInfo:
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)
    message_id = (str(msg.get("Message-ID") or "")).strip().strip("<>")
    if not message_id:
        import hashlib

        message_id = "nomid-" + hashlib.sha256(raw).hexdigest()[:32]
    from_addr = parseaddr(str(msg.get("From") or ""))[1].lower()
    subject = str(msg.get("Subject") or "")
    date_ts = None
    try:
        if msg.get("Date"):
            date_ts = int(parsedate_to_datetime(str(msg["Date"])).timestamp())
    except Exception:
        date_ts = None
    return EmailInfo(
        message_id=message_id,
        from_addr=from_addr,
        subject=subject,
        text=_body_text(msg),
        date_ts=date_ts,
        auth_ok=auth_passes(msg, settings.payment_sender_list),
    )


def _to_paise(text: str) -> int | None:
    try:
        return int((Decimal(text.replace(",", "")) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


def parse_payment(subject: str, text: str, settings: Settings) -> ParsedPayment:
    blob = f"{subject}\n{text}"
    low = blob[:4000].lower()

    direction = "unknown"
    if any(k in low for k in settings.out_keywords):
        direction = "out"
    elif any(k in low for k in settings.in_keywords):
        direction = "in"

    amount = None
    for rx in _AMOUNT_RES:
        m = rx.search(blob)
        if m:
            amount = _to_paise(m.group(1))
            if amount is not None:
                break

    utr = None
    num = settings.utr_pattern
    labelled = re.search(
        rf"(?:utr|rrn|upi\s*ref(?:erence)?|ref(?:erence)?(?:\s*(?:no\.?|number|id))?|txn(?:\s*id)?|"
        rf"transaction(?:\s*(?:id|no\.?|number))?)\D{{0,25}}?(?<![0-9])({num})(?![0-9])",
        blob,
        re.I,
    )
    if labelled:
        utr = labelled.group(1)
    else:
        bare = re.search(rf"(?<![0-9])({num})(?![0-9])", blob)
        if bare:
            utr = bare.group(1)

    code = None
    prefix = re.escape(settings.order_prefix)
    cm = re.search(rf"\b{prefix}([{CODE_ALPHABET}]{{6}})\b", blob, re.I)
    if cm:
        code = settings.order_prefix.upper() + cm.group(1).upper()

    payer = None
    pm = _PAYER_RE.search(blob[:1500])
    if pm:
        payer = " ".join(pm.group(1).split())[:60]

    return ParsedPayment(direction, amount, utr, code, payer)
