-- =============================================================================
-- MailPulse — Buckets & assignments (run in Supabase: SQL Editor → New query)
-- =============================================================================
-- After this succeeds:
--   1. Backend `.env`: `SUPABASE_KEY` must be the **service_role** secret
--      (Dashboard → Project Settings → API). The anon key will NOT pass RLS for
--      server-side inserts unless you impersonate the user JWT on every call.
--   2. Restart the FastAPI server.
--
-- Safe to run more than once (idempotent seeds + IF NOT EXISTS).
-- =============================================================================

-- ── 1. mail_buckets ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.mail_buckets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES auth.users (id) ON DELETE CASCADE,
    name text NOT NULL,
    description text,
    color text,
    icon text,
    is_system boolean NOT NULL DEFAULT false,
    sort_order integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT mail_buckets_valid_user CHECK (is_system = true OR user_id IS NOT NULL),
    CONSTRAINT mail_buckets_user_name_unique UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_mail_buckets_user_custom
    ON public.mail_buckets (user_id)
    WHERE is_system = false;

-- One system row per bucket name (PostgreSQL UNIQUE(user_id,name) treats NULLs as distinct)
CREATE UNIQUE INDEX IF NOT EXISTS mail_buckets_system_name_lower_uidx
    ON public.mail_buckets (lower(trim(name)))
    WHERE is_system = true;

ALTER TABLE public.mail_buckets ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can view system buckets or their own buckets" ON public.mail_buckets;
DROP POLICY IF EXISTS "Users can insert their own custom buckets" ON public.mail_buckets;
DROP POLICY IF EXISTS "Users can update their own custom buckets" ON public.mail_buckets;
DROP POLICY IF EXISTS "Users can delete their own custom buckets" ON public.mail_buckets;

CREATE POLICY "Users can view system buckets or their own buckets"
    ON public.mail_buckets
    FOR SELECT
    USING (is_system = true OR auth.uid() = user_id);

CREATE POLICY "Users can insert their own custom buckets"
    ON public.mail_buckets
    FOR INSERT
    WITH CHECK (auth.uid() = user_id AND is_system = false);

CREATE POLICY "Users can update their own custom buckets"
    ON public.mail_buckets
    FOR UPDATE
    USING (auth.uid() = user_id AND is_system = false);

CREATE POLICY "Users can delete their own custom buckets"
    ON public.mail_buckets
    FOR DELETE
    USING (auth.uid() = user_id AND is_system = false);

-- Idempotent system bucket seeds (one row per name for is_system = true)
INSERT INTO public.mail_buckets (name, description, is_system, sort_order)
SELECT v.name, v.description, true, v.sort_order
FROM (VALUES
    ('VIP', 'Emails from marked VIP senders.', 2),
    ('Escalations', 'Urgent issues or escalations.', 3),
    ('Approvals', 'Emails requiring your approval or sign-off.', 4),
    ('Follow-ups', 'Emails needing a follow-up or reply.', 5),
    ('Finance', 'Invoices, receipts, and financial documents.', 6),
    ('Delegations', 'Tasks or emails delegated to others or to you.', 7),
    ('System Alerts', 'Automated system notifications and alerts.', 8),
    ('Newsletters', 'Subscriptions, digests, and newsletters.', 9),
    ('Promotions', 'Marketing emails and offers.', 10),
    ('Social', 'Social media notifications.', 11),
    ('Low Priority', 'Emails classified as low priority.', 12),
    ('Primary', 'Important emails that do not fit another specific bucket.', 13),
    ('Other', 'Fallback bucket for unrecognized emails.', 14)
) AS v(name, description, sort_order)
WHERE NOT EXISTS (
    SELECT 1 FROM public.mail_buckets b
    WHERE b.is_system = true AND b.name = v.name
);

-- ── 2. email_bucket_assignments ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.email_bucket_assignments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    gmail_message_id text NOT NULL,
    bucket_id uuid NOT NULL REFERENCES public.mail_buckets (id) ON DELETE CASCADE,
    bucket_source text NOT NULL DEFAULT 'MANUAL',
    bucket_locked boolean NOT NULL DEFAULT true,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, gmail_message_id)
);

CREATE INDEX IF NOT EXISTS idx_email_bucket_assignments_user_msg
    ON public.email_bucket_assignments (user_id, gmail_message_id);

ALTER TABLE public.email_bucket_assignments ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can manage their own bucket assignments" ON public.email_bucket_assignments;

CREATE POLICY "Users can manage their own bucket assignments"
    ON public.email_bucket_assignments
    FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

-- ── 3. mail_bucket_rules ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.mail_bucket_rules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    bucket_id uuid NOT NULL REFERENCES public.mail_buckets (id) ON DELETE CASCADE,
    field_name text NOT NULL,
    operator text NOT NULL,
    value text NOT NULL,
    priority integer NOT NULL DEFAULT 0,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mail_bucket_rules_user ON public.mail_bucket_rules (user_id);

ALTER TABLE public.mail_bucket_rules ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can manage their own bucket rules" ON public.mail_bucket_rules;

CREATE POLICY "Users can manage their own bucket rules"
    ON public.mail_bucket_rules
    FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);
