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

-- =====================================================================
-- Tables fondamentales (Phase 1) — jamais migrées, cause du 500 sur
-- /api/auth/etsy/callback, /api/listings/, /api/orders/, /api/analytics/summary
-- (PGRST205 "Could not find the table ... in the schema cache").
-- =====================================================================

-- === ETSY_TOKENS — tokens OAuth Etsy par utilisateur (voir routers/auth.py) ===
-- Jamais exposée en lecture publique : contient un refresh_token valable
-- plusieurs mois. user_id est la clé primaire car upsert() y est appelé
-- sans on_conflict explicite (une seule boutique Etsy par compte EtsyDrop).
CREATE TABLE IF NOT EXISTS etsy_tokens (
  user_id UUID REFERENCES auth.users(id) PRIMARY KEY,
  access_token TEXT NOT NULL,
  refresh_token TEXT NOT NULL,
  expires_in INTEGER,
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- === LISTINGS — fiches produit du catalogue ===
CREATE TABLE IF NOT EXISTS listings (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  tags TEXT[] DEFAULT '{}',
  price_min DECIMAL(10,2) NOT NULL,
  price_max DECIMAL(10,2) NOT NULL,
  supplier TEXT NOT NULL,  -- SupplierName (my_catalog | eprolo | cj_dropshipping | printify | printful | zendrop | aliexpress)
  variants JSONB DEFAULT '[]'::jsonb,  -- [{color, size, engraving, supplier_price}]
  stock_status TEXT NOT NULL DEFAULT 'available',  -- available | low_stock | out_of_stock
  margin_pct DECIMAL(5,2) DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON listings(user_id, stock_status);

-- === ORDERS — commandes Etsy synchronisées + fulfillment ===
CREATE TABLE IF NOT EXISTS orders (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) NOT NULL,
  etsy_order_id TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  product_name TEXT NOT NULL,
  supplier TEXT NOT NULL,  -- SupplierName
  amount DECIMAL(10,2) NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_supplier',  -- pending_supplier | in_transit | delivered | delayed | return_requested
  tracking_number TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON orders(user_id, created_at DESC);

-- === ANALYTICS_SUMMARY — snapshot revenus/marge par utilisateur ===
-- Une ligne par utilisateur (voir .maybe_single() dans routers/analytics.py) —
-- alimentée par un job de synchronisation Etsy, pas encore branché.
CREATE TABLE IF NOT EXISTS analytics_summary (
  user_id UUID REFERENCES auth.users(id) PRIMARY KEY,
  revenue_total DECIMAL(12,2) DEFAULT 0,
  net_margin_pct DECIMAL(5,2) DEFAULT 0,
  conversion_rate_pct DECIMAL(5,2) DEFAULT 0,
  net_profit DECIMAL(12,2) DEFAULT 0,
  history JSONB DEFAULT '[]'::jsonb,  -- [{period, revenue, profit}]
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- === KEYWORDS — référentiel mots-clés partagé (pas de scoping par utilisateur) ===
CREATE TABLE IF NOT EXISTS keywords (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  keyword TEXT UNIQUE NOT NULL,
  volume_monthly INTEGER DEFAULT 0,
  total_sales INTEGER DEFAULT 0,
  competition TEXT,
  trend_pct DECIMAL(5,2) DEFAULT 0,
  score INTEGER DEFAULT 0
);

-- === SOURCING_CACHE — cache de comparaison fournisseurs (voir routers/sourcing.py > /compare) ===
CREATE TABLE IF NOT EXISTS sourcing_cache (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) NOT NULL,
  product_name TEXT NOT NULL,
  sources JSONB DEFAULT '[]'::jsonb,  -- [{supplier, price, delay_days_min, delay_days_max, available}]
  best_price JSONB,
  recommended_sell_price DECIMAL(10,2),
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON sourcing_cache(user_id, product_name);

-- =====================================================================
-- Row Level Security — la clé "anon/publishable" Supabase est exposée
-- publiquement dans le frontend (indispensable pour Supabase Auth). Sans
-- RLS, n'importe quel utilisateur authentifié pourrait interroger
-- /rest/v1/etsy_tokens (ou toute autre table ci-dessous) directement et
-- lire/modifier les lignes de n'importe qui — le filtrage .eq("user_id", …)
-- fait côté backend ne protège en rien un appel REST direct à Supabase.
-- Le backend utilise la clé service_role (SUPABASE_KEY), qui contourne
-- RLS par conception : ces policies n'affectent que les accès directs
-- au projet Supabase (anon/authenticated), jamais l'API FastAPI elle-même.
-- =====================================================================
ALTER TABLE ai_costs ENABLE ROW LEVEL SECURITY;
ALTER TABLE promotions ENABLE ROW LEVEL SECURITY;
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE etsy_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE analytics_summary ENABLE ROW LEVEL SECURITY;
ALTER TABLE keywords ENABLE ROW LEVEL SECURITY;
ALTER TABLE sourcing_cache ENABLE ROW LEVEL SECURITY;

-- DROP POLICY IF EXISTS + CREATE POLICY (au lieu de CREATE POLICY seul) :
-- contrairement à CREATE TABLE, Postgres n'a pas de "CREATE POLICY IF NOT
-- EXISTS" — ce script doit rester rejouable sans erreur.
DROP POLICY IF EXISTS "own rows only" ON ai_costs;
CREATE POLICY "own rows only" ON ai_costs FOR ALL USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "own rows only" ON promotions;
CREATE POLICY "own rows only" ON promotions FOR ALL USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "own row only" ON profiles;
CREATE POLICY "own row only" ON profiles FOR ALL USING (auth.uid() = id);
DROP POLICY IF EXISTS "own row only" ON etsy_tokens;
CREATE POLICY "own row only" ON etsy_tokens FOR ALL USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "own rows only" ON listings;
CREATE POLICY "own rows only" ON listings FOR ALL USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "own rows only" ON orders;
CREATE POLICY "own rows only" ON orders FOR ALL USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "own row only" ON analytics_summary;
CREATE POLICY "own row only" ON analytics_summary FOR ALL USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "own rows only" ON sourcing_cache;
CREATE POLICY "own rows only" ON sourcing_cache FOR ALL USING (auth.uid() = user_id);
-- keywords : référentiel partagé, lecture seule pour tout utilisateur connecté
-- (les écritures passent par le backend avec la clé service_role, qui
-- contourne RLS — aucune policy d'écriture n'est donc nécessaire ici).
DROP POLICY IF EXISTS "read for authenticated" ON keywords;
CREATE POLICY "read for authenticated" ON keywords FOR SELECT USING (auth.role() = 'authenticated');
