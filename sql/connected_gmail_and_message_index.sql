-- Multi-mailbox Gmail OAuth + cached message envelopes (run in Supabase SQL Editor).

create table if not exists public.connected_gmail_accounts (
  user_id text not null,
  account_email text not null,
  credentials_encrypted text not null,
  updated_at timestamptz not null default (timezone('utc', now())),
  primary key (user_id, account_email)
);

create index if not exists connected_gmail_accounts_user_idx
  on public.connected_gmail_accounts (user_id);

comment on table public.connected_gmail_accounts is
  'Per-Supabase-user linked Gmail accounts; credentials_encrypted is Fernet blob (see oauth_token_crypto).';

create table if not exists public.gmail_message_index (
  user_id text not null,
  account_email text not null,
  message_id text not null,
  internal_ts bigint not null default 0,
  envelope jsonb not null,
  updated_at timestamptz not null default (timezone('utc', now())),
  primary key (user_id, account_email, message_id)
);

create index if not exists gmail_message_index_mailbox_ts_idx
  on public.gmail_message_index (user_id, account_email, internal_ts desc);

comment on table public.gmail_message_index is
  'Cached Gmail message envelopes (same shape as email_cache) for fast reload after login; not full MIME.';

grant select, insert, update, delete on public.connected_gmail_accounts to service_role, authenticated, anon;
grant select, insert, update, delete on public.gmail_message_index to service_role, authenticated, anon;
