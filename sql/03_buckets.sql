-- backend/sql/03_buckets.sql
-- Prefer **supabase_mail_buckets_setup.sql** for Supabase SQL Editor (idempotent policies + seeds).
-- This file remains as a historical reference.

-- 1. Create mail_buckets table
CREATE TABLE IF NOT EXISTS public.mail_buckets (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
    name text NOT NULL,
    description text,
    color text,
    icon text,
    is_system boolean DEFAULT false,
    sort_order integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT valid_user_for_custom CHECK (is_system = true OR user_id IS NOT NULL),
    UNIQUE(user_id, name) -- Prevent duplicate bucket names per user
);

-- RLS for mail_buckets
ALTER TABLE public.mail_buckets ENABLE ROW LEVEL SECURITY;

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


-- 2. Create email_bucket_assignments table
CREATE TABLE IF NOT EXISTS public.email_bucket_assignments (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    gmail_message_id text NOT NULL,
    bucket_id uuid NOT NULL REFERENCES public.mail_buckets(id) ON DELETE CASCADE,
    bucket_source text NOT NULL DEFAULT 'MANUAL',
    bucket_locked boolean DEFAULT true,
    reason text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    UNIQUE(user_id, gmail_message_id)
);

-- RLS for email_bucket_assignments
ALTER TABLE public.email_bucket_assignments ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage their own bucket assignments"
    ON public.email_bucket_assignments
    FOR ALL
    USING (auth.uid() = user_id);


-- 3. Create mail_bucket_rules table
CREATE TABLE IF NOT EXISTS public.mail_bucket_rules (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    bucket_id uuid NOT NULL REFERENCES public.mail_buckets(id) ON DELETE CASCADE,
    field_name text NOT NULL,
    operator text NOT NULL,
    value text NOT NULL,
    priority integer DEFAULT 0,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

-- RLS for mail_bucket_rules
ALTER TABLE public.mail_bucket_rules ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage their own bucket rules"
    ON public.mail_bucket_rules
    FOR ALL
    USING (auth.uid() = user_id);


-- 4. Initial System Buckets
INSERT INTO public.mail_buckets (name, description, is_system, sort_order)
VALUES 
    ('Primary', 'Important emails that do not fit another specific bucket.', true, 13),
    ('VIP', 'Emails from marked VIP senders.', true, 2),
    ('Approvals', 'Emails requiring your approval or sign-off.', true, 4),
    ('Escalations', 'Urgent issues or escalations.', true, 3),
    ('Finance', 'Invoices, receipts, and financial documents.', true, 6),
    ('Follow-ups', 'Emails needing a follow-up or reply.', true, 5),
    ('Delegations', 'Tasks or emails delegated to others or to you.', true, 7),
    ('System Alerts', 'Automated system notifications and alerts.', true, 8),
    ('Newsletters', 'Subscriptions, digests, and newsletters.', true, 9),
    ('Promotions', 'Marketing emails and offers.', true, 10),
    ('Social', 'Social media notifications.', true, 11),
    ('Low Priority', 'Emails classified as low priority.', true, 12),
    ('Other', 'Fallback bucket for unrecognized emails.', true, 14)
ON CONFLICT DO NOTHING;
