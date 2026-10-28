// Receives PayFast's ITN (Instant Transaction Notification) and updates the
// matching user's subscription row. Deploy with:
//
//   supabase functions deploy payfast-webhook --no-verify-jwt
//
// (--no-verify-jwt because PayFast posts here directly, with no Supabase
// auth token -- this function authenticates the request by round-tripping
// it to PayFast's own validate endpoint instead; see below.)
//
// Required secrets (set with `supabase secrets set`):
//   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY  (service role, NOT the anon key)
//   PAYFAST_MERCHANT_ID, PRO_MONTHLY_PRICE   (must match license_client.py's
//   values -- these gate which notifications are trusted, see below)
//
// NOTE: this is a starting point, not a drop-in. Confirm the exact ITN
// field names and the subscription billing-cycle fields against PayFast's
// current docs before going live.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const PAYFAST_VALIDATE_URL =
  Deno.env.get("PAYFAST_VALIDATE_URL") ?? "https://www.payfast.co.za/eng/query/validate";
const EXPECTED_MERCHANT_ID = Deno.env.get("PAYFAST_MERCHANT_ID") ?? "10056596";
const EXPECTED_AMOUNT = parseFloat(Deno.env.get("PRO_MONTHLY_PRICE") ?? "99.00");

const supabaseAdmin = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  const bodyText = await req.text();

  // 1. Confirm this notification genuinely came from PayFast before
  //    trusting anything in it -- PayFast's own documented check.
  const validateResp = await fetch(PAYFAST_VALIDATE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: bodyText,
  });
  const validateText = (await validateResp.text()).trim();
  if (validateText !== "VALID") {
    return new Response("Invalid ITN", { status: 400 });
  }

  const params = new URLSearchParams(bodyText);

  // Set `custom_str1` (or `m_payment_id`) to the user's Supabase auth UUID
  // when you create the PayFast payment/subscription request client-side.
  const userId = params.get("custom_str1");
  const paymentStatus = params.get("payment_status"); // COMPLETE, FAILED, CANCELLED, ...
  const subscriptionToken = params.get("token"); // PayFast recurring-billing token
  const pfPaymentId = params.get("pf_payment_id");
  const merchantId = params.get("merchant_id");
  const amountGross = parseFloat(params.get("amount_gross") ?? "0");

  if (!userId) {
    return new Response("Missing custom_str1 (user id)", { status: 400 });
  }

  // The "VALID" round-trip above only confirms PayFast itself sent this
  // notification -- it does NOT confirm it's for OUR product at OUR price.
  // Without these two checks, a replay of any other genuine (but unrelated,
  // e.g. far cheaper) PayFast notification against this same endpoint would
  // still pass validation and could grant Pro access for free.
  if (merchantId !== EXPECTED_MERCHANT_ID) {
    console.error(`merchant_id mismatch: got ${merchantId}`);
    return new Response("Merchant ID mismatch", { status: 400 });
  }
  const isActive = paymentStatus === "COMPLETE";
  if (isActive && amountGross < EXPECTED_AMOUNT - 0.01) {
    console.error(`amount_gross too low: got ${amountGross}, expected >= ${EXPECTED_AMOUNT}`);
    return new Response("Amount mismatch", { status: 400 });
  }

  const { error } = await supabaseAdmin
    .from("subscriptions")
    .update({
      tier: isActive ? "pro" : "free",
      status: isActive ? "active" : "past_due",
      provider: "payfast",
      provider_subscription_id: subscriptionToken,
      provider_customer_id: pfPaymentId,
      // Tune to your actual billing cycle -- this assumes monthly billing.
      current_period_end: isActive
        ? new Date(Date.now() + 31 * 24 * 60 * 60 * 1000).toISOString()
        : null,
      updated_at: new Date().toISOString(),
    })
    .eq("user_id", userId);

  if (error) {
    console.error(error);
    return new Response("DB error", { status: 500 });
  }

  return new Response("OK", { status: 200 });
});
