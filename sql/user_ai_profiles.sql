-- CXO inbox profile for MailPulse AI classification (JSON payload from OpenRouter inference).
-- Run once: Supabase Dashboard → SQL Editor → paste → Run.
-- Fixes PostgREST PGRST205 "Could not find the table 'public.user_ai_profiles' in the schema cache".

create table if not exists public.user_ai_profiles (
  account_email text primary key,
  profile jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default (timezone('utc', now()))
);

create index if not exists user_ai_profiles_updated_at_idx
  on public.user_ai_profiles (updated_at desc);

comment on table public.user_ai_profiles is 'Per-Gmail-account AI profile JSON: inferred role/industry/vip/topics plus optional admin_persona {designation, field, focus_notes} for prompt overrides.';

-- PostgREST access (backend uses SUPABASE_KEY from .env; service_role bypasses RLS)
grant select, insert, update, delete on table public.user_ai_profiles to service_role;
grant select, insert, update, delete on table public.user_ai_profiles to authenticated;
grant select, insert, update, delete on table public.user_ai_profiles to anon;
