-- Opt-in "report a bad conversion" feature. Write-only from the client: no
-- select policy exists at all, so a submitted report can only be read back
-- via the Supabase dashboard or the service-role key, never by the anon key
-- -- not even the person who submitted it. Nothing is ever collected here
-- automatically; the app only inserts a row when the user explicitly
-- clicks "Report an Issue" and confirms.

create table public.bug_reports (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users (id) on delete set null,
  bank_name text,
  details text not null,
  app_version text,
  used_ocr boolean,
  -- Only present if the user explicitly opted in to attaching the parsed
  -- table (dates/amounts/descriptions -- never the original PDF itself).
  sample_rows jsonb,
  created_at timestamptz not null default now()
);

alter table public.bug_reports enable row level security;

create policy "anyone can submit a bug report" on public.bug_reports
  for insert
  with check (true);
