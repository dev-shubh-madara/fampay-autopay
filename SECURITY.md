# Security

This project moves real money, so security comes first. This page explains exactly
what it protects against, what it can't, and how to report a problem.

## Design principles

- **Your money never touches this code.** Buyers pay your own UPI ID directly from their
  own UPI app. This backend only *reads* the confirmation e-mail. It cannot send, refund or
  move money, and it never sees a UPI PIN, OTP, card or bank login.
- **Read-only mailbox access.** The mailbox is opened with `EXAMINE` and mail is fetched with
  `BODY.PEEK`. Nothing is marked read, moved, deleted or sent.
- **Nothing leaves your server** except the signed webhook to the URL *you* configure.
  No analytics, no telemetry, no third-party scripts (the QR code is generated locally).
- **Fail closed.** The app refuses to start with weak or missing secrets, unauthenticated
  e-mail checks, or an `http://` webhook.

## What is protected

| Threat | Protection |
|---|---|
| Fake "payment received" e-mail | Sender must be FamPay **and** pass DMARC/DKIM according to the top `Authentication-Results` header added by *your* mail server. Lower (forgeable) headers are ignored. |
| Re-using a UTR (own or someone else's) | Every UTR is stored with a database `UNIQUE` index and can be used for exactly one order, forever. Used UTRs are never purged. |
| Guessing UTRs | Each order allows `MAX_CLAIMS_PER_ORDER` (default 6) different UTRs, then locks (`423`). Plus per-IP rate limits. |
| Re-using an old, unrelated payment | A payment must arrive after the order was created (30 min tolerance). |
| Paying less than the price | Amount from the e-mail is compared to the order; under-payment is rejected. |
| Changing the price in the browser | Browsers can only order fixed `PRODUCTS`. Custom amounts need `X-Api-Secret`, which CORS refuses to accept from browsers. |
| Processing the same e-mail twice | Tracked by IMAP UID **and** Message-ID; restart-safe. |
| Intercepting the Gmail login | IMAP uses TLS ≥ 1.2 with full certificate + hostname verification (Python's `imaplib` does *not* verify by default; we fix that). |
| Forged webhooks to your site | HMAC-SHA256 over `timestamp.body`, timestamp for replay protection, unique delivery id. Redirects are never followed. Unsigned webhooks are never sent. |
| Leaking your users' data | Public order responses never include `customer_ref`, `metadata` or the submitted UTR. Errors never echo input. `/health` reveals nothing. |
| Admin token brute force | Constant-time compare, 10 wrong tries per 10 min per IP, then blocked. Admin is disabled entirely if no token is set. |
| Spoofed client IPs | `CF-Connecting-IP` / `X-Forwarded-For` are ignored unless you set `TRUST_PROXY_HEADERS=true`. |
| XSS / clickjacking on the checkout | Strict CSP (`script-src 'self'`, no inline script, no third-party code), `X-Frame-Options: DENY`, no `innerHTML` with data, `upi://` links validated. |
| Oversized / malformed requests | Body limit (64 KB), unknown JSON fields rejected, strict ID format, amount must be finite with ≤ 2 decimals. |
| SQL injection | Every query is parameterised. |
| Database theft on shared hosts | DB file is created `0600`, folder `0700`; Docker runs as a non-root user. |
| Vulnerable dependencies | Four small, version-capped dependencies; Dependabot + `pip-audit` in CI. |

All of the above are covered by tests in [`tests/test_security.py`](tests/test_security.py).

## What you are responsible for

No software can be "100% safe" on its own. These are on you:

1. **Keep `.env` secret.** Generate secrets with
   `python -c "import secrets;print(secrets.token_urlsafe(32))"`. Never commit `.env`.
2. **Protect the Gmail account.** Use a dedicated Gmail with 2-Step Verification. The app
   password can read that inbox; revoke it in your Google account if it leaks.
3. **Serve over HTTPS** (Cloudflare Tunnel, Railway/Render/Koyeb do this for you).
4. **Unlock purchases from the webhook or a server-side check**, never only from what the
   browser says.
5. **Back up `data/payments.db`.** It holds the list of used UTRs.
6. Set `CORS_ORIGINS` to your own site and `ALLOW_PUBLIC_ORDERS=false` if you create
   orders from your backend.
7. Keep dependencies updated (merge Dependabot PRs).

## Known limits

- **Trust in FamPay's e-mails.** Verification is only as reliable as FamPay's notification
  e-mails. If FamPay changes its wording, payments show as `unparsed` in `/admin/emails`
  (they fail closed – nothing is wrongly approved).
- **A buyer who shares their UTR** before submitting it could, in theory, have it claimed by
  someone else first. The UTR only works for an order of at least the paid amount, created
  around the same time. Tell buyers to enter the UTR only on your site.
- **Rate limits are in-memory** per process. Running many instances? Put a rate limit at
  your proxy / Cloudflare too.
- Using a personal UPI account for business payments may be subject to FamPay's terms and
  tax rules where you live.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's
**Security → Report a vulnerability** (private advisory) on this repository.
Include steps to reproduce. You'll get a reply as soon as possible, and credit in the fix
if you want it.
