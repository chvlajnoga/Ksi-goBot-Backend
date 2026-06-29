-- Uruchom w Supabase SQL Editor

CREATE TABLE IF NOT EXISTS emails (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    client_email TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'inne',
    sender TEXT,
    subject TEXT,
    date DATE,
    summary TEXT,
    priority TEXT DEFAULT 'normalny',
    action_needed BOOLEAN DEFAULT false,
    action_desc TEXT,
    has_attachment BOOLEAN DEFAULT false,
    status TEXT DEFAULT 'nowe',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS inquiries (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    client_email TEXT NOT NULL,
    sender TEXT,
    subject TEXT,
    date DATE,
    summary TEXT,
    suggested_response TEXT,
    status TEXT DEFAULT 'nowe',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE emails DISABLE ROW LEVEL SECURITY;
ALTER TABLE inquiries DISABLE ROW LEVEL SECURITY;
