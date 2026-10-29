# FamPay Auto-Pay

**Self-hosted, zero-fee UPI payment verification for your website – using your own FamPay account.**

No payment gateway, no KYC paperwork, no 2% cut. Buyers pay your FamPay UPI ID from any UPI app,
type their **UTR**, and this backend confirms the payment automatically by reading FamPay's
"payment received" e-mail from your Gmail over IMAP.

```
 Buyer ──pays ₹79──▶ your FamPay UPI ID
   │                         │
   │ enters 12-digit UTR      ▼  FamPay e-mail "You received ₹79 … UTR 4123…"
   ▼                         │
 Your website ──▶ FamPay Auto-Pay backend ◀── IMAP (read-only) ── your Gmail
                    │  UTR matches + amount OK + DKIM/DMARC pass
                    ▼
            order = PAID ──▶ signed webhook to your site
```

## Features

- ✅ **Automatic confirmation** – usually under a minute after the buyer submits the UTR
- 🔒 **One UTR, one use – ever.** Re-using someone else's (or an old) UTR is impossible
- 🛡️ **Fake e-mail protection** – only trusts mail from FamPay that passes DKIM/DMARC on *your* mail server
- 💸 **Under-payment detection** – paying ₹1 for a ₹79 order is rejected
- 📬 **Read-only IMAP** – never marks mail as read, moves or deletes anything
- 🔁 **Restart-safe** – every e-mail is processed once (UID + Message-ID tracking)
- 🪝 **Signed webhooks** (HMAC-SHA256) to unlock the purchase on your site
- 🧾 **Ready checkout page** at `/checkout` with UPI QR + "Open UPI app" button
- 👮 **Admin API** – list orders/payments, manual approve, test the e-mail parser
- 🐳 Single small Python service + SQLite. Docker, Railway, Render, Koyeb or your own PC

> **Why UTR and not the UPI note?** FamPay's payment e-mail does not contain the note the buyer
> typed, so the UTR (UPI reference number) is the only reliable link between a payment and an order.

---

## 1. Requirements

| You need | Why |
|---|---|
| A FamPay account with a UPI ID (e.g. `yourname@fam`) | Where buyers send money |
| A Gmail that receives FamPay's payment e-mails | The backend reads them |
| A Gmail **App Password** (needs 2-Step Verification on) | IMAP login – never use your real password |
| Python 3.10+ **or** Docker | To run the backend |

Create the App Password: Google Account → Security → 2-Step Verification → **App passwords** → create one → copy the 16 characters.

Make sure FamPay e-mail notifications for received payments are on and arrive in that Gmail inbox.

## 2. Install & run

```bash
git clone <this-repo-url> fampay-autopay
cd fampay-autopay
cp .env.example .env        # then edit .env with YOUR values
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

or with Docker:

```bash
cp .env.example .env        # edit it
docker compose up -d --build
```

Open:
- `http://localhost:8000/checkout` – working checkout page
- `http://localhost:8000/docs` – interactive API docs
- `http://localhost:8000/health` – shows whether IMAP is connected

## 3. Configure (`.env`)

| Variable | Example | Meaning |
|---|---|---|
| `UPI_ID` | `yourname@fam` | Your FamPay UPI ID shown to buyers |
| `PAYEE_NAME` | `My Shop` | Name in the buyer's UPI app |
| `PRODUCTS` | `basic:29:Basic plan,pro:79:Pro plan` | Fixed-price items the browser may order |
| `ALLOW_PUBLIC_ORDERS` | `true` | `false` = only your backend can create orders |
| `API_SECRET` | random | Your website **backend** sends it as `X-Api-Secret` for custom amounts |
| `ADMIN_TOKEN` | random | `Authorization: Bearer …` for `/admin/*` |
| `IMAP_ENABLED` | `true` | Turn automatic verification on |
| `IMAP_USER` / `IMAP_PASSWORD` | `you@gmail.com` / app password | The Gmail that gets FamPay mails |
| `PAYMENT_SENDERS` | `fampay.in,famapp.in` | Trusted sender domains |
| `REQUIRE_EMAIL_AUTH` | `true` | Keep `true` – blocks forged e-mails |
| `WEBHOOK_URL` / `WEBHOOK_SECRET` | `https://site.com/hook` / random | Notify your site when paid |
| `CORS_ORIGINS` | `https://site.com` | Your website origin(s) |
| `DATA_DIR` | `data` | SQLite location – **mount a volume** on cloud hosts |

Generate secrets: `python -c "import secrets;print(secrets.token_urlsafe(32))"`

## 4. Add it to your website

Full guide with every request/response: **[docs/INTEGRATION.md](docs/INTEGRATION.md)**

**Fastest (no code):** link buyers to `https://pay.yourdomain.com/checkout?product=pro`.

**Your own UI:** 3 calls –

```js
// 1. create order
const order = await (await fetch(API + "/api/orders", {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({ product: "pro", customer_ref: "user-42" })
})).json();
// show order.pay.upi_id, order.amount_inr and a QR of order.pay.upi_link

// 2. buyer pays, types UTR
await fetch(`${API}/api/orders/${order.order_id}/claim`, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({ utr: "412345678901" })
});

// 3. poll until paid (or wait for your webhook)
const s = await (await fetch(`${API}/api/orders/${order.order_id}`)).json(); // s.status === "paid"
```

Ready-made examples:
- [`examples/frontend-snippet.html`](examples/frontend-snippet.html) – drop-in payment box for any site
- [`examples/node-express/server.js`](examples/node-express/server.js) – create orders + verify webhook (Node)
- [`examples/python-flask/app.py`](examples/python-flask/app.py) – same in Python

> Always unlock the purchase from the **webhook** (or a server-side `GET /api/orders/{id}`),
> never only because the browser said "paid".

## 5. Deploy

- **Docker / your own PC / VPS:** `docker compose up -d`. To expose a home PC, use Cloudflare Tunnel.
- **Railway / Render / Koyeb:** deploy from your fork (Dockerfile is detected), add the `.env` values as
  variables, and **attach a volume at `/srv/data`** so orders and used UTRs survive redeploys.

## 6. Security notes

- Never commit `.env`. It is already in `.gitignore`.
- Keep `REQUIRE_EMAIL_AUTH=true`; otherwise anyone could e-mail you a fake "payment received".
- Use a Gmail used for nothing else if you can – the app password can read that inbox.
- `API_SECRET` belongs only on servers, never in browser JavaScript.
- Losing the database means losing the list of used UTRs – back up `data/payments.db`.

## 7. If a payment isn't detected

1. `GET /health` – is `imap.last_error` empty?
2. `GET /admin/emails` – what happened to the e-mail (`ignored_sender`, `rejected_unauthenticated`, `unparsed`…)?
3. In Gmail open the FamPay mail → ⋮ → **Show original** → copy all → `POST /admin/parse-email` (text/plain).
   It shows the amount/UTR the parser reads. If FamPay changed its wording, adjust
   `PAYMENT_IN_KEYWORDS` / `PAYMENT_SENDERS`.
4. Approve by hand: `POST /admin/orders/{id}/approve`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Disclaimer

Not affiliated with FamPay / FamApp. This project only reads e-mails you already receive.
Check FamPay's terms for using a personal account to accept business payments, and follow
the tax rules that apply to you. Provided as-is under the MIT license.
