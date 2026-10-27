// A plain landing page for PayFast to redirect the browser to after checkout
// (return_url and cancel_url both point here, with ?status=success or
// ?status=cancelled). This is purely cosmetic -- the actual subscription
// update happens server-to-server via payfast-webhook, independent of
// whether the user even sees this page.

Deno.serve((req) => {
  const status = new URL(req.url).searchParams.get("status") ?? "success";
  const message =
    status === "cancelled"
      ? "Checkout cancelled. No charge was made."
      : "Thanks! Your payment is being processed -- this can take a few seconds to reflect in the app.";

  const html = `<!doctype html>
<html><head><meta charset="utf-8"><title>Bank Statement Converter</title></head>
<body style="font-family: sans-serif; text-align: center; padding: 60px 20px;">
  <h2>${message}</h2>
  <p>You can close this tab and return to the app.</p>
</body></html>`;

  return new Response(html, { headers: { "Content-Type": "text/html" } });
});
