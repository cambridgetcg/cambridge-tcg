/**
 * Cambridge TCG — Trade-In API (Cloudflare Worker)
 *
 * Endpoints:
 *   GET  /buylist              — Return full buylist JSON
 *   POST /tradein              — Submit a trade-in request
 *   GET  /tradein/:ref?email=  — Check submission status
 *   POST /tradein/:ref/cancel  — Cancel a submission
 *
 * Data stored in Cloudflare KV:
 *   BUYLIST  → "buylist" key contains the full card list
 *   SUBMISSIONS → "TI-YYYYMMDD-XXXX" keys contain submission data
 */

export interface Env {
  BUYLIST: KVNamespace;
  SUBMISSIONS: KVNamespace;
  STORE_EMAIL: string;
  ADMIN_SECRET: string;
}

// ── CORS headers ──────────────────────────────────────────
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PATCH, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, X-Admin-Secret",
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders },
  });
}

// ── Reference number generator ────────────────────────────
function generateRef(): string {
  const now = new Date();
  const date = now.toISOString().slice(0, 10).replace(/-/g, "");
  const rand = Math.random().toString(36).slice(2, 6).toUpperCase();
  return `TI-${date}-${rand}`;
}

// ── Route handler ─────────────────────────────────────────
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    try {
      // GET /buylist
      if (request.method === "GET" && path === "/buylist") {
        return handleGetBuylist(env);
      }

      // POST /tradein
      if (request.method === "POST" && path === "/tradein") {
        return handleSubmitTradeIn(request, env);
      }

      // GET /tradein/:ref?email=
      const statusMatch = path.match(/^\/tradein\/(TI-[\w-]+)$/);
      if (request.method === "GET" && statusMatch) {
        const ref = statusMatch[1];
        const email = url.searchParams.get("email") || "";
        return handleGetStatus(ref, email, env);
      }

      // POST /tradein/:ref/cancel
      const cancelMatch = path.match(/^\/tradein\/(TI-[\w-]+)\/cancel$/);
      if (request.method === "POST" && cancelMatch) {
        return handleCancel(cancelMatch[1], request, env);
      }

      // ── Admin endpoints (require X-Admin-Secret header) ──
      const adminSecret = request.headers.get("X-Admin-Secret");
      const isAdmin = adminSecret && adminSecret === env.ADMIN_SECRET;

      // GET /admin/submissions?date=YYYY-MM-DD
      if (request.method === "GET" && path === "/admin/submissions") {
        if (!isAdmin) return json({ error: "Unauthorized" }, 401);
        return handleAdminList(request, env);
      }

      // GET /admin/submission/:ref
      const adminGetMatch = path.match(/^\/admin\/submission\/(TI-[\w-]+)$/);
      if (request.method === "GET" && adminGetMatch) {
        if (!isAdmin) return json({ error: "Unauthorized" }, 401);
        return handleAdminGet(adminGetMatch[1], env);
      }

      // PATCH /admin/submission/:ref — update status / adjust items / prices
      const adminPatchMatch = path.match(/^\/admin\/submission\/(TI-[\w-]+)$/);
      if (request.method === "PATCH" && adminPatchMatch) {
        if (!isAdmin) return json({ error: "Unauthorized" }, 401);
        return handleAdminPatch(adminPatchMatch[1], request, env);
      }

      // Debug KV (REMOVE IN PROD)
      if (request.method === "GET" && path.startsWith("/debug/kv/")) {
        const key = decodeURIComponent(path.replace("/debug/kv/", ""));
        return handleDebugKV(key, env);
      }

      // Health check
      if (path === "/health") {
        return json({ status: "ok", service: "tradein-api" });
      }

      return json({ error: "Not found" }, 404);
    } catch (err: any) {
      console.error("Error:", err);
      return json({ error: err.message || "Internal server error" }, 500);
    }
  },
};

// ── GET /buylist ──────────────────────────────────────────
async function handleGetBuylist(env: Env): Promise<Response> {
  const data = await env.BUYLIST.get("buylist", "text");
  if (!data) {
    return json({ error: "Buylist not available" }, 503);
  }

  return new Response(data, {
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "public, max-age=300", // 5 min cache
      ...corsHeaders,
    },
  });
}

// ── POST /tradein ─────────────────────────────────────────
interface TradeInRequest {
  customerName: string;
  customerEmail: string;
  customerPhone?: string;
  paymentMethod: "cash" | "credit";
  bankDetails?: string;
  deliveryMethod: "mail" | "instore";
  isOver18: boolean;
  conditionDeclared: "nm" | "mixed";
  notes?: string;
  items: Array<{
    sku: string;
    quantity: number;
    condition: "nm" | "a-";
  }>;
}

async function handleSubmitTradeIn(request: Request, env: Env): Promise<Response> {
  const body: TradeInRequest = await request.json();

  // Validate required fields
  if (!body.customerName?.trim()) return json({ error: "Name is required" }, 400);
  if (!body.customerEmail?.trim()) return json({ error: "Email is required" }, 400);
  if (!body.paymentMethod) return json({ error: "Payment method is required" }, 400);
  if (!body.deliveryMethod) return json({ error: "Delivery method is required" }, 400);
  if (!body.isOver18) return json({ error: "Age declaration is required" }, 400);
  if (!body.items?.length) return json({ error: "Cart is empty" }, 400);

  // Email format check
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(body.customerEmail)) {
    return json({ error: "Invalid email format" }, 400);
  }

  // Rate limit: max 3 submissions per email per day
  const today = new Date().toISOString().slice(0, 10);
  const rateLimitKey = `ratelimit:${body.customerEmail}:${today}`;
  const submissions = parseInt((await env.SUBMISSIONS.get(rateLimitKey)) || "0");
  if (submissions >= 3) {
    return json({ error: "Maximum 3 submissions per day. Please try again tomorrow." }, 429);
  }

  // Load current buylist for price validation
  const buylistRaw = await env.BUYLIST.get("buylist", "text");
  if (!buylistRaw) return json({ error: "Buylist temporarily unavailable" }, 503);
  const buylist = JSON.parse(buylistRaw);
  const cardIndex: Record<string, any> = {};
  for (const item of buylist.items) {
    cardIndex[item.sku] = item;
  }

  // Validate items and calculate totals
  const validatedItems: any[] = [];
  let totalCash = 0;
  let totalCredit = 0;

  for (const item of body.items) {
    const card = cardIndex[item.sku];
    if (!card) {
      return json({ error: `Card ${item.sku} not on buylist` }, 400);
    }
    if (item.quantity < 1 || item.quantity > 10) {
      return json({ error: `Invalid quantity for ${item.sku}` }, 400);
    }

    // Use MINT price if condition is NM, otherwise A- price
    const cashUnit = card.cashPrice;
    const creditUnit = card.creditPrice;

    validatedItems.push({
      sku: item.sku,
      cardNumber: card.cardNumber,
      name: card.name,
      setCode: card.setCode,
      rarity: card.rarity,
      isParallel: card.isParallel,
      quantity: item.quantity,
      condition: item.condition,
      cashUnitPrice: cashUnit,
      creditUnitPrice: creditUnit,
      cashSubtotal: Math.round(cashUnit * item.quantity * 100) / 100,
      creditSubtotal: Math.round(creditUnit * item.quantity * 100) / 100,
    });

    totalCash += cashUnit * item.quantity;
    totalCredit += creditUnit * item.quantity;
  }

  totalCash = Math.round(totalCash * 100) / 100;
  totalCredit = Math.round(totalCredit * 100) / 100;

  // Minimum value check: £3 cash or £5 credit
  const minCheck = body.paymentMethod === "cash" ? totalCash >= 3 : totalCredit >= 5;
  if (!minCheck) {
    const minAmount = body.paymentMethod === "cash" ? "£3 (cash)" : "£5 (credit)";
    return json({ error: `Minimum trade-in value is ${minAmount}` }, 400);
  }

  // Generate reference
  const reference = generateRef();
  const now = new Date();
  const expiresAt = new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000); // 7 days

  const submission = {
    reference,
    status: "submitted",
    customerName: body.customerName.trim(),
    customerEmail: body.customerEmail.trim().toLowerCase(),
    customerPhone: body.customerPhone?.trim() || null,
    paymentMethod: body.paymentMethod,
    bankDetails: body.bankDetails?.trim() || null,
    deliveryMethod: body.deliveryMethod,
    isOver18: body.isOver18,
    conditionDeclared: body.conditionDeclared || "nm",
    notes: body.notes?.trim() || null,
    items: validatedItems,
    quotedCashTotal: totalCash,
    quotedCreditTotal: totalCredit,
    fxRate: buylist.fxRate,
    quoteExpiresAt: expiresAt.toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };

  // Store submission (expires after 90 days)
  await env.SUBMISSIONS.put(reference, JSON.stringify(submission), {
    expirationTtl: 90 * 24 * 60 * 60,
  });

  // Update rate limit counter
  await env.SUBMISSIONS.put(rateLimitKey, String(submissions + 1), {
    expirationTtl: 24 * 60 * 60,
  });

  // Store in "all submissions" index for admin
  const indexKey = `index:${today}`;
  const existingIndex = JSON.parse((await env.SUBMISSIONS.get(indexKey)) || "[]");
  existingIndex.push({
    reference,
    name: submission.customerName,
    email: submission.customerEmail,
    items: validatedItems.length,
    cashTotal: totalCash,
    creditTotal: totalCredit,
    paymentMethod: body.paymentMethod,
    deliveryMethod: body.deliveryMethod,
    createdAt: now.toISOString(),
  });
  await env.SUBMISSIONS.put(indexKey, JSON.stringify(existingIndex), {
    expirationTtl: 90 * 24 * 60 * 60,
  });

  return json({
    reference,
    status: "submitted",
    paymentMethod: body.paymentMethod,
    quotedCashTotal: totalCash,
    quotedCreditTotal: totalCredit,
    selectedTotal: body.paymentMethod === "cash" ? totalCash : totalCredit,
    itemCount: validatedItems.length,
    quoteExpiresAt: expiresAt.toISOString(),
    deliveryMethod: body.deliveryMethod,
    items: validatedItems,
  }, 201);
}

// ── GET /tradein/:ref ─────────────────────────────────────
async function handleGetStatus(ref: string, email: string, env: Env): Promise<Response> {
  if (!email) return json({ error: "Email parameter required" }, 400);

  const data = await env.SUBMISSIONS.get(ref, "text");
  if (!data) return json({ error: "Submission not found" }, 404);

  const submission = JSON.parse(data);

  // Verify email matches (case-insensitive)
  if (submission.customerEmail.toLowerCase() !== email.toLowerCase()) {
    return json({ error: "Submission not found" }, 404); // Don't reveal it exists
  }

  // Return public-safe fields
  return json({
    reference: submission.reference,
    status: submission.status,
    paymentMethod: submission.paymentMethod,
    deliveryMethod: submission.deliveryMethod,
    quotedCashTotal: submission.quotedCashTotal,
    quotedCreditTotal: submission.quotedCreditTotal,
    selectedTotal: submission.paymentMethod === "cash" ? submission.quotedCashTotal : submission.quotedCreditTotal,
    items: submission.items,
    quoteExpiresAt: submission.quoteExpiresAt,
    createdAt: submission.createdAt,
    trackingNumber: submission.trackingNumber || null,
    paymentReference: submission.paymentReference || null,
  });
}

// ── POST /tradein/:ref/cancel ─────────────────────────────
async function handleCancel(ref: string, request: Request, env: Env): Promise<Response> {
  const body: { email: string } = await request.json();
  if (!body.email) return json({ error: "Email required" }, 400);

  const data = await env.SUBMISSIONS.get(ref, "text");
  if (!data) return json({ error: "Submission not found" }, 404);

  const submission = JSON.parse(data);
  if (submission.customerEmail.toLowerCase() !== body.email.toLowerCase()) {
    return json({ error: "Submission not found" }, 404);
  }

  if (!["submitted", "shipped"].includes(submission.status)) {
    return json({ error: `Cannot cancel — status is ${submission.status}` }, 400);
  }

  submission.status = "cancelled";
  submission.updatedAt = new Date().toISOString();
  await env.SUBMISSIONS.put(ref, JSON.stringify(submission), {
    expirationTtl: 90 * 24 * 60 * 60,
  });

  return json({ reference: ref, status: "cancelled" });
}

// ── GET /admin/submissions ────────────────────────────────
async function handleAdminList(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const date = url.searchParams.get("date") || new Date().toISOString().slice(0, 10);

  // List all submissions for the date
  const indexKey = `index:${date}`;
  const index = JSON.parse((await env.SUBMISSIONS.get(indexKey)) || "[]");

  // Also scan for any refs not in the index (e.g. from other dates)
  const status = url.searchParams.get("status");
  const results = status ? index.filter((s: any) => s.status === status) : index;

  return json({ date, count: results.length, submissions: results });
}

// ── GET /admin/submission/:ref ────────────────────────────
async function handleAdminGet(ref: string, env: Env): Promise<Response> {
  const data = await env.SUBMISSIONS.get(ref, "text");
  if (!data) return json({ error: "Not found" }, 404);
  return json(JSON.parse(data));
}

// ── PATCH /admin/submission/:ref ──────────────────────────
interface AdminPatch {
  status?: "submitted" | "received" | "accepted" | "paid" | "rejected" | "cancelled";
  items?: Array<{
    sku: string;
    quantity?: number;       // 0 = remove item
    cashUnitPrice?: number;  // override price
    creditUnitPrice?: number;
  }>;
  adminNote?: string;
  trackingNumber?: string;
  paymentReference?: string;
}

async function handleAdminPatch(ref: string, request: Request, env: Env): Promise<Response> {
  const data = await env.SUBMISSIONS.get(ref, "text");
  if (!data) return json({ error: "Not found" }, 404);

  const submission = JSON.parse(data);
  const patch: AdminPatch = await request.json();

  // Update status
  if (patch.status) submission.status = patch.status;
  if (patch.adminNote !== undefined) submission.adminNote = patch.adminNote;
  if (patch.trackingNumber !== undefined) submission.trackingNumber = patch.trackingNumber;
  if (patch.paymentReference !== undefined) submission.paymentReference = patch.paymentReference;

  // Adjust items
  if (patch.items?.length) {
    for (const itemPatch of patch.items) {
      const idx = submission.items.findIndex((i: any) => i.sku === itemPatch.sku);
      if (idx === -1) continue;

      if (itemPatch.quantity === 0) {
        // Remove item
        submission.items.splice(idx, 1);
      } else {
        const item = submission.items[idx];
        if (itemPatch.quantity !== undefined) item.quantity = itemPatch.quantity;
        if (itemPatch.cashUnitPrice !== undefined) item.cashUnitPrice = itemPatch.cashUnitPrice;
        if (itemPatch.creditUnitPrice !== undefined) item.creditUnitPrice = itemPatch.creditUnitPrice;
        // Recalculate subtotals
        item.cashSubtotal = Math.round(item.cashUnitPrice * item.quantity * 100) / 100;
        item.creditSubtotal = Math.round(item.creditUnitPrice * item.quantity * 100) / 100;
        item.adminAdjusted = true;
        submission.items[idx] = item;
      }
    }

    // Recalculate totals
    submission.finalCashTotal = Math.round(
      submission.items.reduce((s: number, i: any) => s + i.cashSubtotal, 0) * 100
    ) / 100;
    submission.finalCreditTotal = Math.round(
      submission.items.reduce((s: number, i: any) => s + i.creditSubtotal, 0) * 100
    ) / 100;
  }

  submission.updatedAt = new Date().toISOString();

  await env.SUBMISSIONS.put(ref, JSON.stringify(submission), {
    expirationTtl: 90 * 24 * 60 * 60,
  });

  return json(submission);
}

// ── DEBUG: direct KV test ──────────────────────────────────
// REMOVE IN PRODUCTION
async function handleDebugKV(key: string, env: Env): Promise<Response> {
  const val = await env.SUBMISSIONS.get(key, "text");
  return json({ key, found: val !== null, length: val?.length });
}
