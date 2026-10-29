"""Orders, payment matching and the one-UTR-one-use rule.

Flow
----
1. Your site creates an order  ->  buyer sees UPI ID + exact amount (+ QR).
2. Buyer pays in any UPI app and types the 12-digit UTR back on your site.
3. FamPay sends you a "payment received" e-mail. The IMAP poller reads it,
   checks DKIM/DMARC, extracts amount + UTR and stores it.
4. UTR from the buyer == UTR from the e-mail  and  amount >= order amount
   ->  order becomes "paid"  ->  your webhook is called.

FamPay e-mails do not include the UPI note, so the UTR is the only reliable
link between a payment and an order. Each UTR can be used exactly once.
"""

import json
import logging
import re
import secrets
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from ..config import Settings
from ..db import Database
from ..errors import ApiError
from .emailparse import CODE_ALPHABET, ParsedPayment, parse_email_bytes, parse_payment, sender_allowed

log = logging.getLogger("fampay.payments")


def _rupees(paise: int | None) -> float | None:
    return None if paise is None else paise / 100


def _fmt(paise: int) -> str:
    return f"{paise / 100:.2f}"


class PaymentService:
    def __init__(self, db: Database, settings: Settings, now=time.time):
        self.db = db
        self.s = settings
        self._now = now
        self.poller = None  # set in main.py; lets a UTR claim check the mailbox immediately
        self.on_paid = None  # callback(order_view) – used for the webhook

    def now(self) -> int:
        return int(self._now())

    # ── presentation ──────────────────────────────────────────────────────
    def upi_link(self, amount_paise: int, code: str) -> str | None:
        if not self.s.upi_id:
            return None
        q = f"pa={quote(self.s.upi_id)}&am={_fmt(amount_paise)}&cu=INR&tn={quote(code)}"
        if self.s.payee_name:
            q = f"pa={quote(self.s.upi_id)}&pn={quote(self.s.payee_name)}&am={_fmt(amount_paise)}&cu=INR&tn={quote(code)}"
        return "upi://pay?" + q

    def status_of(self, row) -> str:
        if row["status"] == "pending" and self.now() > row["expires_at"] + self.s.order_grace_minutes * 60:
            return "expired"
        return row["status"]

    def view(self, row, private: bool = False) -> dict:
        """Public view = only what the buyer's browser needs. Your customer ids,
        metadata and the submitted UTR are shown only to your backend/admin."""
        status = self.status_of(row)
        v = {
            "order_id": row["id"],
            "code": row["code"],
            "status": status,
            "amount_inr": _rupees(row["amount_paise"]),
            "product": row["product"],
            "description": row["description"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "paid_at": row["paid_at"],
        }
        if private:
            v["customer_ref"] = row["customer_ref"]
            v["metadata"] = None
            if row["metadata"]:
                try:
                    v["metadata"] = json.loads(row["metadata"])
                except ValueError:
                    pass
        if status == "pending":
            v["pay"] = {
                "upi_id": self.s.upi_id or None,
                "payee_name": self.s.payee_name or None,
                "amount_inr": _rupees(row["amount_paise"]),
                "upi_link": self.upi_link(row["amount_paise"], row["code"]),
                "utr_submitted": bool(row["claimed_utr"]),
                "instructions": (
                    f"Pay exactly ₹{_fmt(row['amount_paise'])} to {self.s.upi_id or 'the UPI ID shown'}, "
                    "then enter the 12-digit UTR / UPI reference number from your payment app."
                ),
            }
        if private:
            v["claimed_utr"] = row["claimed_utr"]
            v["webhook_state"] = row["webhook_state"]
        return v

    def _to_paise(self, amount_inr) -> int:
        try:
            d = Decimal(str(amount_inr))
        except (InvalidOperation, ValueError):
            raise ApiError(422, "invalid_amount", "Amount must be a number.")
        if not d.is_finite() or d != d.quantize(Decimal("0.01")):
            raise ApiError(422, "invalid_amount", "Amount must have at most 2 decimals.")
        paise = int(d * 100)
        lo, hi = round(self.s.min_amount_inr * 100), round(self.s.max_amount_inr * 100)
        if paise < lo or paise > hi:
            raise ApiError(422, "invalid_amount", f"Amount must be between ₹{lo / 100:g} and ₹{hi / 100:g}.")
        return paise

    # ── orders ────────────────────────────────────────────────────────────
    def _new_code(self) -> str:
        return self.s.order_prefix.upper() + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))

    def create_order(
        self,
        *,
        product: str | None = None,
        amount_inr: float | None = None,
        description: str | None = None,
        customer_ref: str | None = None,
        metadata: dict | None = None,
        trusted: bool = False,
    ) -> dict:
        """trusted=True means the caller sent the correct X-Api-Secret (your backend)."""
        if not trusted and not self.s.allow_public_orders:
            raise ApiError(403, "secret_required", "Orders can only be created by the site's backend.")
        products = self.s.product_map
        title = None
        if product:
            product = product.strip().lower()
            if product not in products:
                raise ApiError(422, "invalid_product", "Unknown product.")
            amount_paise, title = products[product]
            if amount_inr is not None and trusted:
                amount_paise = self._to_paise(amount_inr)  # backend may override the price
        elif amount_inr is not None:
            if not trusted:
                raise ApiError(403, "secret_required", "Custom amounts need the X-Api-Secret header (call from your backend).")
            amount_paise = self._to_paise(amount_inr)
        else:
            raise ApiError(422, "missing_amount", "Send either 'product' or 'amount_inr'.")

        if not trusted:
            # The browser can't be trusted: price comes from PRODUCTS only, and it can't
            # set ids/metadata (stops strangers filling another user's order slots).
            customer_ref, metadata, description = None, None, None

        customer_ref = (customer_ref or "").strip()[:120] or None
        description = (description or title or "").strip()[:200] or None
        meta = None
        if metadata:
            meta = json.dumps(metadata, separators=(",", ":"))
            if len(meta) > 4000:
                raise ApiError(422, "metadata_too_large", "metadata must be under 4 KB of JSON.")
        now = self.now()

        with self.db.tx() as t:
            if customer_ref:
                same = t.execute(
                    "SELECT * FROM orders WHERE customer_ref=? AND amount_paise=? AND COALESCE(product,'')=?"
                    " AND status='pending' AND claimed_utr IS NULL AND expires_at>?",
                    (customer_ref, amount_paise, product or "", now),
                ).fetchone()
                if same:
                    return self.view(same, private=trusted)  # reuse instead of piling up orders
                n = t.execute(
                    "SELECT COUNT(*) FROM orders WHERE customer_ref=? AND status='pending' AND expires_at>?",
                    (customer_ref, now),
                ).fetchone()[0]
                if n >= self.s.max_pending_per_customer:
                    raise ApiError(429, "too_many_orders", "Too many unpaid orders. Pay one or wait for them to expire.")
            for _ in range(10):
                order_id = secrets.token_hex(10)
                code = self._new_code()
                try:
                    t.execute(
                        "INSERT INTO orders(id, code, customer_ref, product, description, amount_paise, status,"
                        " metadata, created_at, expires_at) VALUES(?,?,?,?,?,?, 'pending', ?,?,?)",
                        (order_id, code, customer_ref, product, description, amount_paise, meta, now,
                         now + self.s.order_ttl_minutes * 60),
                    )
                    break
                except sqlite3.IntegrityError:
                    continue
            else:  # pragma: no cover
                raise ApiError(500, "order_failed", "Could not create an order, try again.")
            row = t.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return self.view(row, private=trusted)

    def get_order(self, order_id: str, private: bool = False) -> dict:
        row = self.db.fetchone("SELECT * FROM orders WHERE id=?", (order_id,))
        if not row:
            raise ApiError(404, "order_not_found", "No such order.")
        return self.view(row, private)

    def _matchable(self, order) -> bool:
        return order["status"] == "pending" and self.now() <= order["expires_at"] + self.s.order_grace_minutes * 60

    def _settle(self, t, order, payment_id: int, note: str) -> None:
        now = self.now()
        t.execute("UPDATE orders SET status='paid', payment_id=?, paid_at=? WHERE id=?", (payment_id, now, order["id"]))
        t.execute("UPDATE payments SET status='matched', order_id=?, note=? WHERE id=?", (order["id"], note, payment_id))

    def _fire_paid(self, order_id: str) -> None:
        if self.on_paid is None:
            return
        try:
            self.on_paid(self.get_order(order_id, private=True))
        except Exception:
            log.exception("on_paid callback failed for %s", order_id)

    # ── e-mail -> payment -> order ────────────────────────────────────────
    def handle_email(self, raw: bytes) -> dict:
        info = parse_email_bytes(raw, self.s)
        now = self.now()

        def finish(result: str, extra: dict | None = None) -> dict:
            self.db.execute(
                "INSERT OR REPLACE INTO processed_emails(message_id, from_addr, subject, result, processed_at)"
                " VALUES(?,?,?,?,?)",
                (info.message_id, info.from_addr, info.subject[:200], result, now),
            )
            log.info("email %s from %s -> %s", info.message_id[:24], info.from_addr, result)
            return {"result": result, "message_id": info.message_id, **(extra or {})}

        if self.db.fetchone("SELECT 1 FROM processed_emails WHERE message_id=?", (info.message_id,)):
            return {"result": "already_processed", "message_id": info.message_id}
        if not sender_allowed(info.from_addr, self.s.payment_sender_list):
            return finish("ignored_sender")
        if self.s.require_email_auth and not info.auth_ok:
            return finish("rejected_unauthenticated")

        parsed = parse_payment(info.subject, info.text, self.s)
        if parsed.direction == "out":
            return finish("ignored_outgoing")
        if parsed.direction != "in" or parsed.amount_paise is None:
            return finish("unparsed")

        outcome = self._match_payment(info.message_id, parsed, info.date_ts or now)
        result = outcome.pop("result")
        if result == "matched":
            self._fire_paid(outcome["order_id"])
        return finish(result, outcome)

    def _match_payment(self, message_id: str, p: ParsedPayment, received_at: int) -> dict:
        with self.db.tx() as t:
            if p.utr and t.execute("SELECT 1 FROM payments WHERE utr=?", (p.utr,)).fetchone():
                return {"result": "duplicate_payment"}
            try:
                cur = t.execute(
                    "INSERT INTO payments(message_id, utr, amount_paise, code, payer, received_at, status)"
                    " VALUES(?,?,?,?,?,?, 'unmatched')",
                    (message_id, p.utr, p.amount_paise, p.code, p.payer, received_at),
                )
            except sqlite3.IntegrityError:
                return {"result": "duplicate_payment"}
            pid = cur.lastrowid

            order = None
            if p.code:  # some banks do include the note – use it when present
                order = t.execute("SELECT * FROM orders WHERE code=?", (p.code,)).fetchone()
            if order is None and p.utr:
                order = t.execute(
                    "SELECT * FROM orders WHERE claimed_utr=? AND status='pending'", (p.utr,)
                ).fetchone()
            if order is None:
                return {"result": "unmatched", "payment_id": pid}
            if not self._matchable(order):
                t.execute("UPDATE payments SET order_id=?, note=? WHERE id=?", (order["id"], f"order {self.status_of(order)}", pid))
                return {"result": "order_not_open", "payment_id": pid, "order_id": order["id"]}
            if self._older_than_order(received_at, order):
                t.execute("UPDATE payments SET note=? WHERE id=?", ("paid before the order existed", pid))
                return {"result": "payment_before_order", "payment_id": pid, "order_id": order["id"]}
            if p.amount_paise < order["amount_paise"]:
                t.execute(
                    "UPDATE payments SET status='underpaid', order_id=?, note=? WHERE id=?",
                    (order["id"], f"paid {_fmt(p.amount_paise)}, needed {_fmt(order['amount_paise'])}", pid),
                )
                return {"result": "underpaid", "payment_id": pid, "order_id": order["id"]}
            self._settle(t, order, pid, "overpaid" if p.amount_paise > order["amount_paise"] else "ok")
            return {"result": "matched", "payment_id": pid, "order_id": order["id"]}

    # ── buyer submits the UTR ─────────────────────────────────────────────
    def _older_than_order(self, received_at: int, order) -> bool:
        return received_at < order["created_at"] - self.s.payment_before_order_tolerance_minutes * 60

    def claim(self, order_id: str, utr: str) -> dict:
        utr = re.sub(r"\s+", "", utr or "")
        if not re.fullmatch(self.s.utr_pattern, utr):
            raise ApiError(422, "invalid_utr", "That does not look like a valid 12-digit UPI reference (UTR).")
        settled = False
        with self.db.tx() as t:
            order = t.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                raise ApiError(404, "order_not_found", "No such order.")
            if order["status"] == "paid":
                return self.view(order)
            if not self._matchable(order):
                raise ApiError(410, "order_expired", "This order has expired. Create a new one.")
            if utr == order["claimed_utr"]:
                payment = t.execute("SELECT * FROM payments WHERE utr=?", (utr,)).fetchone()
                if payment is None:  # same UTR again, still waiting: no new attempt used
                    return self.view(order) | {"message": "Still waiting for the payment e-mail."}
            else:
                # Every NEW UTR counts as an attempt, so nobody can brute-force UTRs.
                if order["claim_attempts"] >= self.s.max_claims_per_order:
                    raise ApiError(423, "order_locked", "Too many UTR attempts on this order. Contact the seller.")
                t.execute("UPDATE orders SET claim_attempts=claim_attempts+1 WHERE id=?", (order_id,))

        with self.db.tx() as t:
            order = t.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if order["status"] == "paid":
                return self.view(order)
            # One UTR, one use – ever.
            payment = t.execute("SELECT * FROM payments WHERE utr=?", (utr,)).fetchone()
            if payment is not None:
                if payment["status"] == "matched":
                    raise ApiError(409, "utr_used", "That payment was already used for an order.")
                if payment["order_id"] and payment["order_id"] != order_id:
                    raise ApiError(409, "utr_used", "That payment belongs to another order.")
                if self._older_than_order(payment["received_at"], order):
                    raise ApiError(409, "payment_before_order", "That payment was made before this order was created. Contact the seller.")
                if payment["amount_paise"] < order["amount_paise"]:
                    raise ApiError(
                        402, "underpaid",
                        f"That payment was ₹{_fmt(payment['amount_paise'])} but this order needs ₹{_fmt(order['amount_paise'])}.",
                    )
                t.execute("UPDATE orders SET claimed_utr=? WHERE id=?", (utr, order_id))
                self._settle(t, order, payment["id"], "claimed by utr")
                settled = True
            else:
                clash = t.execute(
                    "SELECT 1 FROM orders WHERE claimed_utr=? AND status IN ('pending','paid') AND id<>?",
                    (utr, order_id),
                ).fetchone()
                if clash:
                    raise ApiError(409, "utr_in_use", "That UTR is already attached to another order.")
                t.execute("UPDATE orders SET claimed_utr=? WHERE id=?", (utr, order_id))
        if settled:
            self._fire_paid(order_id)
            return self.get_order(order_id)

        if self.poller is not None:  # look in the mailbox right now
            try:
                self.poller.poll_soon()
            except Exception:
                log.warning("immediate mailbox check failed", exc_info=True)
        v = self.get_order(order_id)
        if v["status"] == "pending":
            v["message"] = "UTR saved. Your order is confirmed automatically as soon as the payment e-mail arrives (usually under a minute)."
        return v

    # ── maintenance ───────────────────────────────────────────────────────
    def housekeeping(self) -> None:
        """Drop old e-mail logs and old UNPAID orders. Paid orders and every
        UTR ever seen are kept forever – that's what blocks re-use."""
        now = self.now()
        self.db.execute("DELETE FROM processed_emails WHERE processed_at < ?", (now - 90 * 86400,))
        self.db.execute(
            "DELETE FROM orders WHERE status IN ('pending','cancelled') AND expires_at < ?",
            (now - self.s.purge_unpaid_after_days * 86400,),
        )

    # ── admin ─────────────────────────────────────────────────────────────
    def admin_approve(self, order_id: str, note: str = "manual approval") -> dict:
        with self.db.tx() as t:
            order = t.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                raise ApiError(404, "order_not_found", "No such order.")
            if order["status"] == "paid":
                return self.view(order, True)
            cur = t.execute(
                "INSERT INTO payments(message_id, utr, amount_paise, code, payer, received_at, status, note)"
                " VALUES(?,?,?,?,?,?, 'unmatched', ?)",
                (f"manual-{secrets.token_hex(6)}", None, order["amount_paise"], order["code"], "admin", self.now(), note),
            )
            self._settle(t, order, cur.lastrowid, note)
        self._fire_paid(order_id)
        return self.get_order(order_id, True)

    def cancel(self, order_id: str) -> dict:
        with self.db.tx() as t:
            order = t.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                raise ApiError(404, "order_not_found", "No such order.")
            if order["status"] == "pending":
                t.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
        return self.get_order(order_id, True)

    def list_orders(self, status: str | None, limit: int) -> list[dict]:
        if status:
            rows = self.db.fetchall("SELECT * FROM orders WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.fetchall("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self.view(r, True) for r in rows]

    def list_payments(self, status: str | None, limit: int) -> list[dict]:
        if status:
            rows = self.db.fetchall("SELECT * FROM payments WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.fetchall("SELECT * FROM payments ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {k: r[k] for k in ("id", "utr", "code", "payer", "status", "order_id", "note", "received_at")}
            | {"amount_inr": _rupees(r["amount_paise"])}
            for r in rows
        ]

    def list_emails(self, limit: int) -> list[dict]:
        rows = self.db.fetchall("SELECT * FROM processed_emails ORDER BY processed_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
