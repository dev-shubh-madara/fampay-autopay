// Checkout page logic. Served from the same origin under a strict CSP:
// no third-party scripts, no inline script, no innerHTML with data.
(() => {
  "use strict";
  const API = location.origin;
  const $ = (id) => document.getElementById(id);
  const ORDER_RE = /^[0-9a-f]{20}$/;
  const params = new URLSearchParams(location.search);
  let selected = (params.get("product") || "").toLowerCase().slice(0, 40) || null;
  let order = null, pollTimer = null;

  function store(op, k, v) { // storage can be blocked; never let that break checkout
    try { return op === "get" ? sessionStorage.getItem(k) : op === "set" ? sessionStorage.setItem(k, v) : sessionStorage.removeItem(k); }
    catch (e) { return null; }
  }

  async function api(path, opts = {}) {
    const r = await fetch(API + path, {
      headers: { "Content-Type": "application/json" },
      credentials: "omit",
      cache: "no-store",
      ...opts,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.message || "Request failed (" + r.status + ")");
    return data;
  }

  async function loadProducts() {
    const { products } = await api("/api/products");
    const box = $("products");
    products.forEach((p) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "product" + (p.id === selected ? " sel" : "");
      const name = document.createElement("span");
      name.textContent = p.title;
      const price = document.createElement("b");
      price.textContent = "₹" + p.amount_inr;
      b.append(name, price);
      b.onclick = () => {
        selected = p.id;
        [...box.children].forEach((c) => c.classList.remove("sel"));
        b.classList.add("sel");
        $("btn-create").disabled = false;
      };
      box.appendChild(b);
    });
    if (!products.some((p) => p.id === selected)) selected = null;
    $("btn-create").disabled = !selected;
  }

  function safeUpiLink(link) {
    return typeof link === "string" && link.startsWith("upi://pay?") ? link : "#";
  }

  function showPay(o) {
    order = o;
    store("set", "fampay_order", o.order_id);
    $("step-choose").classList.add("hidden");
    $("step-pay").classList.remove("hidden");
    $("desc").textContent = o.description || "";
    $("amount").textContent = "₹" + o.amount_inr;
    $("upi").textContent = o.pay.upi_id || "UPI ID not set";
    $("paylink").href = safeUpiLink(o.pay.upi_link);
    $("qr").src = "/api/orders/" + encodeURIComponent(o.order_id) + "/qr.svg";
  }

  function showDone(o) {
    clearInterval(pollTimer);
    store("remove", "fampay_order");
    $("step-pay").classList.add("hidden");
    $("step-choose").classList.add("hidden");
    $("step-done").classList.remove("hidden");
    $("done-info").textContent = "Order " + o.code + " · ₹" + o.amount_inr;
  }

  function setMsg(text, cls) { $("msg").className = "msg " + (cls || ""); $("msg").textContent = text; }

  function startPolling() {
    clearInterval(pollTimer);
    let tries = 0;
    pollTimer = setInterval(async () => {
      if (++tries > 300) { clearInterval(pollTimer); return setMsg("Still not confirmed. Contact the seller with your UTR.", "bad"); }
      try {
        const o = await api("/api/orders/" + order.order_id);
        if (o.status === "paid") showDone(o);
        else if (o.status === "expired") { clearInterval(pollTimer); setMsg("Order expired. Reload to start again.", "bad"); }
      } catch (e) { /* keep polling */ }
    }, 4000);
  }

  $("btn-create").onclick = async () => {
    $("btn-create").disabled = true;
    try { showPay(await api("/api/orders", { method: "POST", body: JSON.stringify({ product: selected }) })); }
    catch (e) { alert(e.message); $("btn-create").disabled = false; }
  };

  $("btn-claim").onclick = async () => {
    const utr = $("utr").value.replace(/\s+/g, "");
    if (!/^\d{12}$/.test(utr)) return setMsg("Enter the 12-digit UTR number.", "bad");
    $("btn-claim").disabled = true;
    setMsg("Checking…");
    try {
      const o = await api("/api/orders/" + order.order_id + "/claim", { method: "POST", body: JSON.stringify({ utr }) });
      if (o.status === "paid") return showDone(o);
      setMsg(o.message || "UTR saved. Waiting for the payment confirmation…", "ok");
      startPolling();
    } catch (e) { setMsg(e.message, "bad"); }
    finally { $("btn-claim").disabled = false; }
  };

  $("copy").onclick = () => { try { navigator.clipboard.writeText($("upi").textContent); } catch (e) {} };

  (async () => {
    const saved = store("get", "fampay_order");
    if (saved && ORDER_RE.test(saved)) {
      try {
        const o = await api("/api/orders/" + saved);
        if (o.status === "paid") return showDone(o);
        if (o.status === "pending") { showPay(o); if (o.pay.utr_submitted) startPolling(); return; }
      } catch (e) { /* start fresh */ }
    }
    loadProducts().catch((e) => alert("Cannot reach payment server: " + e.message));
  })();
})();
