"""Reads new mail over IMAP and feeds it to PaymentService.

Read-only: the mailbox is opened with EXAMINE and messages are fetched with
BODY.PEEK, so nothing is marked read, moved or deleted. Progress is tracked by
IMAP UID (stored in the database) and every e-mail is also de-duplicated by
Message-ID, so a restart never double-credits a payment.

Runs in a background thread so it works with any web framework.
"""

import imaplib
import logging
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone

from ..config import Settings
from ..db import Database
from .service import PaymentService

log = logging.getLogger("fampay.imap")


class ImapPoller:
    def __init__(self, settings: Settings, db: Database, service: PaymentService, imap_factory=imaplib.IMAP4_SSL):
        self.s = settings
        self.db = db
        self.service = service
        self._factory = imap_factory
        self._conn = None
        self._uidvalidity: str | None = None
        self._last_uid: int | None = None
        self._lock = threading.Lock()  # background loop and on-demand checks never overlap
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_poll = 0.0
        self.last_ok_at: int | None = None
        self.last_error: str | None = None
        self._load_state()

    # ── state ─────────────────────────────────────────────────────────────
    def _load_state(self) -> None:
        raw = self.db.get_kv("imap_state")
        if raw:
            try:
                validity, uid = raw.split(":", 1)
                self._uidvalidity, self._last_uid = validity or None, int(uid) or None
            except ValueError:
                pass

    def _save_state(self) -> None:
        self.db.set_kv("imap_state", f"{self._uidvalidity or ''}:{self._last_uid or 0}")

    # ── IMAP ──────────────────────────────────────────────────────────────
    def _connect(self):
        # Python's imaplib does NOT verify the server certificate by default.
        # Always pass a verifying context so nobody can intercept the Gmail login.
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        conn = self._factory(self.s.imap_host, self.s.imap_port, ssl_context=ctx, timeout=30)
        conn.login(self.s.imap_user, self.s.imap_password)
        return conn

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def _fetch_new(self):
        """Returns (uidvalidity, [(uid, raw_bytes)], initial, high_uid)."""
        if self._conn is None:
            self._conn = self._connect()
        conn = self._conn
        typ, _ = conn.select(self.s.imap_folder, readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"cannot open folder {self.s.imap_folder}")
        validity = None
        _, data = conn.response("UIDVALIDITY")
        if data and data[0]:
            validity = data[0].decode() if isinstance(data[0], bytes) else str(data[0])

        initial = self._last_uid is None or bool(validity and self._uidvalidity and validity != self._uidvalidity)
        high = 0
        if initial:
            since = (datetime.now(timezone.utc) - timedelta(days=self.s.imap_lookback_days)).strftime("%d-%b-%Y")
            typ, data = conn.uid("SEARCH", None, "SINCE", since)
            floor = 0
            typ_all, data_all = conn.uid("SEARCH", None, "ALL")
            if typ_all == "OK" and data_all and data_all[0]:
                high = max(int(u) for u in data_all[0].split())
        else:
            typ, data = conn.uid("SEARCH", None, "UID", f"{self._last_uid + 1}:*")
            floor = self._last_uid
        if typ != "OK":
            raise imaplib.IMAP4.error("search failed")
        uids = sorted(int(u) for u in (data[0] or b"").split())
        uids = [u for u in uids if u > floor]  # "n:*" always returns the newest mail, even if old

        out = []
        for uid in uids[-200:]:
            typ, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not parts:
                continue
            for part in parts:
                if isinstance(part, tuple) and len(part) > 1:
                    out.append((uid, part[1]))
                    break
        return validity, out, initial, high

    def poll_once(self) -> int:
        """Fetch and process new mail. Returns how many e-mails were handled."""
        with self._lock:
            self._last_poll = time.monotonic()
            try:
                validity, batch, initial, high = self._fetch_new()
            except Exception:
                self._drop()
                raise
            if validity and validity != self._uidvalidity:
                if self._uidvalidity is not None:
                    self._last_uid = None  # mailbox rebuilt: old UIDs mean nothing
                self._uidvalidity = validity
            handled, failed = 0, False
            for uid, raw in batch:
                try:
                    self.service.handle_email(raw)
                except Exception:
                    log.exception("failed to process e-mail uid=%s", uid)
                    failed = True  # keep _last_uid behind it so it is retried
                    continue
                handled += 1
                if not failed:
                    self._last_uid = max(self._last_uid or 0, uid)
            if initial and not failed:
                self._last_uid = max(self._last_uid or 0, high)
            self._save_state()
            self.last_ok_at = int(time.time())
            self.last_error = None
            return handled

    def poll_soon(self, min_gap: float = 5.0) -> int:
        """On-demand check right after a buyer submits a UTR. Rate-limited so it can't be spammed."""
        if time.monotonic() - self._last_poll < min_gap:
            return 0
        return self.poll_once()

    # ── background loop ───────────────────────────────────────────────────
    def _run(self) -> None:
        self.service.housekeeping()
        delay = self.s.imap_poll_seconds
        log.info("IMAP poller started for %s", self.s.imap_user)
        while not self._stop.is_set():
            try:
                n = self.poll_once()
                if n:
                    log.info("processed %d new e-mail(s)", n)
                delay = self.s.imap_poll_seconds
            except Exception as exc:
                # Never echo raw server responses (could contain the login name); type + short text only.
                text = str(exc)
                if self.s.imap_password:
                    text = text.replace(self.s.imap_password, "***")
                self.last_error = (type(exc).__name__ + ": " + text)[:200]
                delay = min(max(delay * 2, 30), 300)
                log.warning("IMAP poll failed (%s); retrying in %ss", self.last_error, delay)
            self._stop.wait(delay)
        self._drop()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="imap-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
