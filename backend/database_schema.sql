-- =====================================================================
-- === DATABASE_SCHEMA.SQL — Tables Supabase additionnelles ===
-- =====================================================================
-- À exécuter dans l'éditeur SQL de Supabase (ou via une migration).
-- Ne remplace aucune table existante — additif uniquement.

-- === AI_COSTS — suivi du coût des générations IA (image + fiche) ===
CREATE TABLE IF NOT EXISTS ai_costs (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id),
  type TEXT NOT NULL,  -- 'image' | 'listing'
  cost_eur DECIMAL(10,4) NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON ai_costs(user_id, created_at DESC);

-- === PROMOTIONS — publications & performance publicitaire par plateforme ===
CREATE TABLE IF NOT EXISTS promotions (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id),
  listing_id TEXT NOT NULL,
  platform TEXT NOT NULL,  -- 'pinterest' | 'meta' | 'etsy_ads' | 'organic'
  external_id TEXT,        -- pin_id, ad_id, etc.
  status TEXT DEFAULT 'draft',
  impressions INTEGER DEFAULT 0,
  clicks INTEGER DEFAULT 0,
  spend_eur DECIMAL(10,2) DEFAULT 0,
  revenue_eur DECIMAL(10,2) DEFAULT 0,
  roas DECIMAL(6,2) GENERATED ALWAYS AS (CASE WHEN spend_eur > 0 THEN revenue_eur / spend_eur ELSE 0 END) STORED,
  published_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON promotions(user_id, listing_id);
CREATE INDEX ON promotions(user_id, platform);

-- === PROFILES — plan d'abonnement Stripe (Free / Pro) ===
CREATE TABLE IF NOT EXISTS profiles (
  id UUID REFERENCES auth.users(id) PRIMARY KEY,
  plan TEXT DEFAULT 'free',
  plan_started_at TIMESTAMPTZ,
  stripe_customer_id TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
