// Example: your Node.js / Express website talking to FamPay Auto-Pay.
//
//   npm i express
//   FAMPAY_API=http://localhost:8000 API_SECRET=... WEBHOOK_SECRET=... node server.js
//
// 1. POST /buy        -> creates an order on the payment backend (server-side, with API_SECRET)
// 2. POST /webhook    -> receives "order.paid", verifies the signature, unlocks the purchase

const express = require("express");
const crypto = require("crypto");

const FAMPAY_API = process.env.FAMPAY_API || "http://localhost:8000";
const API_SECRET = process.env.API_SECRET; // same value as API_SECRET in the backend .env
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET; // same value as WEBHOOK_SECRET in the backend .env

const app = express();

// ── 1. create an order (any amount, any metadata) ─────────────────────
app.post("/buy", express.json(), async (req, res) => {
  const userId = "user-123"; // take this from your own login/session
  const r = await fetch(`${FAMPAY_API}/api/orders`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Api-Secret": API_SECRET },
    body: JSON.stringify({
      amount_inr: 149,
      description: "Course: Python Basics",
      customer_ref: userId,
      metadata: { userId, courseId: "py-101" },
    }),
  });
  const order = await r.json();
  if (!r.ok) return res.status(r.status).json(order);
  // Send only what the browser needs. Show order.pay.upi_link as a QR + button.
  res.json({ order_id: order.order_id, amount_inr: order.amount_inr, pay: order.pay });
});

// ── 2. webhook: the backend tells you the order is paid ───────────────
// IMPORTANT: verify against the RAW body, so use express.raw() here.
app.post("/webhook", express.raw({ type: "application/json" }), (req, res) => {
  const ts = req.get("X-FamPay-Timestamp") || "";
  const sig = req.get("X-FamPay-Signature") || "";
  const expected =
    "sha256=" + crypto.createHmac("sha256", WEBHOOK_SECRET).update(ts + "." + req.body).digest("hex");

  const ok =
    sig.length === expected.length && crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected));
  if (!ok) return res.status(401).send("bad signature");
  if (Math.abs(Date.now() / 1000 - Number(ts)) > 600) return res.status(401).send("too old");

  const { event, order } = JSON.parse(req.body);
  if (event === "order.paid") {
    // Idempotent: the same order can arrive twice. Use order.order_id as a unique key.
    console.log("PAID:", order.order_id, order.amount_inr, order.metadata);
    // e.g. db.unlockCourse(order.metadata.userId, order.metadata.courseId)
  }
  res.sendStatus(200);
});

app.listen(3000, () => console.log("shop on http://localhost:3000"));
