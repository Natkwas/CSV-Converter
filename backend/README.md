# BankStatementConverter backend

Accounts, subscription state, and a free-tier usage counter, for turning the
desktop app into free-tier-with-limits + paid-subscription. Built on
[Supabase](https://supabase.com) (managed Postgres + Auth + Edge Functions)
so there's no server to run yourself.

Now wired into the desktop app (`app.py` calls `license_client.py` at the
project root -- sign-in dialog, and a usage check before every export). It
won't do anything until you've completed the setup below; `app.py` detects
the unconfigured placeholder URL/key and tells the user instead of failing
oddly.

## What's here

```
backend/
  supabase/
    migrations/0001_init.sql     # schema: profiles, subscriptions, usage_counters
    migrations/0002_bug_reports.sql  # opt-in "report a bad conversion" table (write-only from the client)
    functions/payfast-webhook/   # updates a user's subscription when PayFast confirms payment
  .env.example                   # config template

license_client.py                # at the project root -- imported by app.py
```

## How it works

- Each signed-up user gets a `profiles`, `subscriptions` (defaults to
  `tier='free'`), and `usage_counters` row automatically (a DB trigger on
  signup).
- Before each PDF conversion, the app calls the `check_and_increment_usage`
  Postgres function (via `license_client.check_and_increment_usage()`). It
  atomically checks the user's tier and, for free users, their usage count
  for the current calendar month -- incrementing it and allowing the
  conversion only if they're under the limit (5/month by default). Pro
  users with an active subscription always pass, uncounted.
- That function runs as `SECURITY DEFINER` but only ever touches the
  calling user's own row (via `auth.uid()`) -- no user can check or spend
  another user's quota, and the client never sends a user id itself.
- When PayFast confirms a payment, it POSTs an ITN to the
  `payfast-webhook` Edge Function, which verifies it with PayFast directly
  (not just trusts the POST body) and flips that user's `subscriptions` row
  to `tier='pro'`.

## Setup

1. **Create a Supabase project** at [supabase.com](https://supabase.com) (free tier is enough to start).
2. **Run the migrations**: paste `supabase/migrations/0001_init.sql`, then
   `0002_bug_reports.sql`, into the Supabase SQL editor and run each, or if
   you have the [Supabase CLI](https://supabase.com/docs/guides/cli):
   `supabase db push`.
3. **Set `SUPABASE_URL` and the anon key** (Project Settings -> API) --
   either edit the defaults at the top of `license_client.py` directly, or
   set the `SUPABASE_URL` / `SUPABASE_ANON_KEY` environment variables
   before running the app (see `.env.example`). Safe to ship inside the app
   either way.
4. **Deploy the webhook** (requires the Supabase CLI and `supabase login` / `supabase link`):
   ```bash
   supabase secrets set SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
   supabase secrets set PAYFAST_MERCHANT_ID=your-merchant-id
   supabase secrets set PRO_MONTHLY_PRICE=99.00
   supabase functions deploy payfast-webhook --no-verify-jwt
   ```
   `PAYFAST_MERCHANT_ID`/`PRO_MONTHLY_PRICE` must match `license_client.py`'s
   values -- the webhook rejects any notification for a different merchant
   or a lower amount than expected (otherwise a replay of any other genuine
   PayFast notification, for an unrelated and possibly far cheaper payment,
   would still pass PayFast's own "VALID" check and could grant Pro access
   for free). If you already deployed this function before these checks
   existed, redeploy it.

   Note the deployed function URL -- you'll need it for step 5.
5. **Set up PayFast**: create a merchant account, then when you build the
   checkout request, set `custom_str1` to the user's Supabase auth UUID
   (`license_client.sign_in(...)`'s response has it under `user.id`) so the
   webhook knows which user to credit. Point PayFast's ITN/notify URL at
   the function URL from step 4. **Read `payfast-webhook/index.ts`'s header
   comment** -- the exact ITN field names and billing-cycle handling need
   confirming against PayFast's current docs before going live.
6. **Test**: sign up a test user (`license_client.sign_up(...)`), confirm
   their email (or disable email confirmation in Supabase Auth settings for
   testing), then call `check_and_increment_usage()` a few times and watch
   the `usage_counters` row in the Supabase table editor.

## How it's wired into the app

- An **Account** button in the toolbar opens a sign-in/sign-up dialog
  (`license_client.sign_in()` / `sign_up()`); the session token is cached
  in `~/.bankstatementconverter/session.json` so you don't sign in every
  launch.
- **Export CSV** now calls `license_client.check_and_increment_usage()`
  first. Not signed in -> prompts to sign in. Free tier exhausted ->
  blocks with an upgrade message. No internet / backend down -> blocks
  with a clear error rather than failing silently (this is a deliberate
  choice to protect the subscription model over convenience; revisit if
  you'd rather allow a few offline exports as a grace fallback).
- The status bar shows remaining free-tier exports after each one.
- An **Upgrade to Pro...** button (in the Account dialog, and offered
  automatically when the free tier is exhausted) opens a real signed
  PayFast checkout in the browser.

## Security notes

- **Row Level Security**: every table (`profiles`, `subscriptions`,
  `usage_counters`) has RLS enabled with only a `select ... using (auth.uid()
  = ...)` policy -- no insert/update/delete policy exists for the
  `anon`/`authenticated` roles at all, so the anon key can read a signed-in
  user's own row and nothing else, and can't write anything directly. All
  writes happen through `check_and_increment_usage()` (`SECURITY DEFINER`,
  scoped to the calling `auth.uid()` only) or the webhook's service-role
  key. To confirm this yourself against your live project: sign in as two
  different test users, and with user A's access token, try
  `GET {SUPABASE_URL}/rest/v1/subscriptions?select=*` with header
  `Authorization: Bearer <user A's token>` -- it should return only user
  A's row, never user B's, no matter what `user_id` you put in the query
  string.
- **PayFast verification**: the webhook never trusts the client. It
  round-trips every notification to PayFast's own `validate` endpoint (the
  officially documented check), and additionally rejects anything where
  `merchant_id` or the paid amount don't match what's expected (see the
  Deploy step above) -- only then does it flip `tier` to `'pro'`, and only
  from inside the Edge Function using the service-role key.
- **Free tier limit**: enforced by `check_and_increment_usage()` server-side
  against `usage_counters`, not a local counter -- resetting the app or
  editing local files can't grant more exports.

## Costs

Supabase free tier covers early-stage usage; ~$25/month once you outgrow
it. PayFast has no monthly fee, just a per-transaction percentage --
confirm current rates on their site. See the chat history for the fuller
cost breakdown (Stripe isn't available for South African merchants
directly, which is why this uses PayFast).
