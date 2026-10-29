# Integration guide

Base URL below = wherever you run the backend, e.g. `https://pay.yourdomain.com`.

## Pick a mode

| Mode | Who creates orders | Use when |
|---|---|---|
| **A. Public products** | Browser, with `{"product": "pro"}` | Fixed prices, static sites, no backend |
| **B. Server orders** (recommended) | Your backend, with `X-Api-Secret` | Custom amounts, carts, linking orders to your users |

Set `ALLOW_PUBLIC_ORDERS=false` to allow only mode B.

---

## Order lifecycle

```
pending ──(UTR + matching e-mail)──▶ paid ──▶ webhook "order.paid"
   │
   ├─ no payment before expires_at + grace ──▶ expired
   └─ admin cancel ──▶ cancelled
```

`ORDER_TTL_MINUTES` (default 60) + `ORDER_GRACE_MINUTES` (default 360, for late e-mails).

---

## Endpoints

### `GET /api/products`
```json
{"upi_configured": true, "products": [{"id":"basic","title":"Basic plan","amount_inr":29.0}]}
```

### `POST /api/orders`
Mode A (browser):
```json
{"product": "pro", "customer_ref": "user-42"}
```
Mode B (server, header `X-Api-Secret: <API_SECRET>`):
```json
{"amount_inr": 149, "description": "Cart #881", "customer_ref": "user-42",
 "metadata": {"cart_id": 881}}
```
`201` response:
```json
{
  "order_id": "9f1c2a...",
  "code": "PAYK7M2QX",
  "status": "pending",
  "amount_inr": 149.0,
  "description": "Cart #881",
  "customer_ref": "user-42",
  "metadata": {"cart_id": 881},
  "expires_at": 1767000000,
  "pay": {
    "upi_id": "yourname@fam",
    "payee_name": "My Shop",
    "amount_inr": 149.0,
    "upi_link": "upi://pay?pa=yourname%40fam&pn=My%20Shop&am=149.00&cu=INR&tn=PAYK7M2QX",
    "utr_submitted": false,
    "instructions": "Pay exactly ₹149.00 to ..."
  }
}
```
Show `pay.upi_id`, the amount, a QR code of `pay.upi_link`, and on phones a button linking to `pay.upi_link`.

Rules: browser orders can't set amounts or metadata. The same `customer_ref` + amount reuses an
open order instead of creating new ones; max `MAX_PENDING_PER_CUSTOMER` open orders.

### `POST /api/orders/{order_id}/claim`
```json
{"utr": "412345678901"}
```
- `200` `status: "paid"` – payment e-mail already found, done
- `200` `status: "pending"` + `message` – UTR saved; it is confirmed the moment the e-mail arrives (the mailbox is checked right away)
- `409 utr_used` / `utr_in_use` – that UTR already belongs to another order
- `402 underpaid` – the payment was smaller than the order
- `410 order_expired`, `422 invalid_utr`, `429 rate_limited`

### `GET /api/orders/{order_id}`
Same object as above. Poll every 3–5 s after a claim until `status == "paid"`.

### Errors
Always JSON: `{"error": "code", "message": "human readable"}`.

---

## Webhook

When an order becomes paid the backend sends:

```
POST {WEBHOOK_URL}
Content-Type: application/json
X-FamPay-Event: order.paid
X-FamPay-Timestamp: 1767000123
X-FamPay-Signature: sha256=<hex>

{"event":"order.paid","order":{ ...order object incl. metadata and claimed_utr... }}
```

Signature = `HMAC_SHA256(WEBHOOK_SECRET, timestamp + "." + raw_body)` in hex.

Verify it (Node):
```js
const expected = "sha256=" + crypto.createHmac("sha256", WEBHOOK_SECRET)
  .update(req.get("X-FamPay-Timestamp") + "." + rawBody).digest("hex");
```
Python:
```python
expected = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
hmac.compare_digest(expected, request.headers["X-FamPay-Signature"])
```

- Reply with any `2xx`. Failures are retried 5 times (2 s, 6 s, 18 s, 54 s, 162 s).
- Be idempotent – use `order.order_id` as a unique key.
- Reject timestamps older than ~10 minutes.
- Can't receive webhooks (static site)? Check `GET /api/orders/{id}` from your server instead.

Full working code: `examples/node-express/server.js`, `examples/python-flask/app.py`.

---

## Admin API

Header: `Authorization: Bearer <ADMIN_TOKEN>`

| Method | Path | What |
|---|---|---|
| GET | `/admin/orders?status=pending&limit=50` | Orders incl. claimed UTR and webhook state |
| GET | `/admin/payments?status=unmatched` | Payments read from e-mail |
| GET | `/admin/emails` | Every e-mail seen and what happened to it |
| POST | `/admin/orders/{id}/approve` | Mark paid by hand (also fires webhook) |
| POST | `/admin/orders/{id}/cancel` | Cancel a pending order |
| POST | `/admin/poll` | Check the mailbox now |
| POST | `/admin/parse-email` | Body = raw e-mail (text/plain). Shows parsed amount/UTR/auth |

---

## Framework notes

- **React / Next.js / Vue:** call `/api/orders` from your API route (mode B), return `order_id` + `pay`
  to the component, render the QR with any QR library, poll the order status.
- **PHP / Laravel / Django / anything:** it's plain HTTP + JSON – create orders with `X-Api-Secret`
  from the server, verify the webhook HMAC exactly as above.
- **Telegram bots:** create the order from the bot, send `pay.upi_link` + amount, ask for the UTR,
  call `/claim`, and deliver when the webhook arrives (or poll the order).
- **Same domain:** put the backend behind your reverse proxy at `/pay/` and set `CORS_ORIGINS` to your site.
