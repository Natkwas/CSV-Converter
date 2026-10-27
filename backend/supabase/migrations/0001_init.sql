-- BankStatementConverter backend schema
-- Accounts (via Supabase Auth), subscriptions, and a free-tier usage counter.
--
-- Run this once in the Supabase SQL editor (or `supabase db push`) on a
-- fresh project. Safe to re-run is NOT guaranteed -- it's a first migration.

create extension if not exists pgcrypto;

-- One row per signed-up user, auto-created on signup (see trigger below).
create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  email text not null,
  created_at timestamptz not null default now()
);

-- Current subscription state per user. Updated only by the payment webhook
-- (via the service-role key), never directly by the client.
create table public.subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references auth.users (id) on delete cascade,
  tier text not null default 'free' check (tier in ('free', 'pro')),
  status text not null default 'active' check (status in ('active', 'past_due', 'canceled')),
  provider text,
  provider_customer_id text,
  provider_subscription_id text,
  current_period_end timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- How many free-tier conversions this user has used in the current
-- calendar month. Reset lazily by check_and_increment_usage() below.
create table public.usage_counters (
  user_id uuid primary key references auth.users (id) on delete cascade,
  period_start date not null default date_trunc('month', now())::date,
  count integer not null default 0,
  updated_at timestamptz not null default now()
);

alter table public.profiles enable row level security;
alter table public.subscriptions enable row level security;
alter table public.usage_counters enable row level security;

-- Users may only ever read their own rows. Writes happen exclusively
-- through the SECURITY DEFINER function and the webhook's service-role key.
create policy "select own profile" on public.profiles
  for select using (auth.uid() = id);

create policy "select own subscription" on public.subscriptions
  for select using (auth.uid() = user_id);

create policy "select own usage" on public.usage_counters
  for select using (auth.uid() = user_id);

-- Bootstrap a profile/subscription/usage row for every new signup.
create function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, email) values (new.id, new.email);
  insert into public.subscriptions (user_id) values (new.id);
  insert into public.usage_counters (user_id) values (new.id);
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- The one function the desktop app actually calls, once per conversion.
-- Runs as SECURITY DEFINER so it can update usage_counters despite RLS,
-- but it only ever touches the CALLING user's own row (auth.uid()) -- it
-- never takes a user id as a parameter, so no caller can check or spend
-- another user's quota.
create function public.check_and_increment_usage(p_limit integer default 5)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_tier text;
  v_status text;
  v_period_end timestamptz;
  v_period_start date;
  v_count integer;
  v_current_month date := date_trunc('month', now())::date;
begin
  if v_user_id is null then
    raise exception 'not authenticated';
  end if;

  select tier, status, current_period_end
    into v_tier, v_status, v_period_end
    from public.subscriptions
    where user_id = v_user_id;

  -- Active, unexpired pro subscription: unlimited, nothing to count.
  if v_tier = 'pro' and v_status = 'active'
     and (v_period_end is null or v_period_end > now()) then
    return jsonb_build_object('allowed', true, 'tier', 'pro', 'remaining', null);
  end if;

  -- Free tier: lock the row, roll the counter over if we're in a new
  -- month, then check-and-increment atomically.
  select period_start, count
    into v_period_start, v_count
    from public.usage_counters
    where user_id = v_user_id
    for update;

  if v_period_start is null or v_period_start <> v_current_month then
    update public.usage_counters
      set period_start = v_current_month, count = 0, updated_at = now()
      where user_id = v_user_id;
    v_count := 0;
  end if;

  if v_count >= p_limit then
    return jsonb_build_object('allowed', false, 'tier', 'free', 'remaining', 0);
  end if;

  update public.usage_counters
    set count = count + 1, updated_at = now()
    where user_id = v_user_id;

  return jsonb_build_object(
    'allowed', true,
    'tier', 'free',
    'remaining', p_limit - (v_count + 1)
  );
end;
$$;

grant execute on function public.check_and_increment_usage(integer) to authenticated;
