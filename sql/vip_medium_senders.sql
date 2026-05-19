-- VIP / medium sender lists for PriorityEngine + Admin UI (SenderRepository).
-- Run once in Supabase → SQL Editor if logs show "Failed to load VIP senders".
--
-- The MailPulse API uses SUPABASE_KEY (service_role); service_role bypasses RLS.
-- RLS stays enabled so anonymous JWT clients cannot read these lists unless you add policies.

create table if not exists public.vip_senders (
  email text primary key not null
);

create table if not exists public.medium_priority_senders (
  email text primary key not null
);

alter table public.vip_senders enable row level security;
alter table public.medium_priority_senders enable row level security;

comment on table public.vip_senders is 'Lowercase normalized VIP sender emails.';
comment on table public.medium_priority_senders is 'Lowercase normalized medium-priority sender emails.';
