-- =====================================================================
-- === MIGRATION 2026-09-18 — PHASE 2 : fournisseurs, pricing, SEO, test ===
-- =====================================================================
-- À exécuter dans l'éditeur SQL Supabase (projet wlqzhoixoxrtueldsqkv).
-- Strictement additif et idempotent (IF NOT EXISTS / DROP POLICY IF EXISTS
-- partout) : rejouable sans risque. Le backend utilise la clé service_role
-- (bypass RLS) ; la RLS ci-dessous protège contre un accès direct PostgREST
-- avec la clé anon depuis le navigateur (voir database_schema.sql).

-- === SUPPLIERS — fournisseurs de l'utilisateur (Eprolo, AliExpress, autre) ===
-- api_key / api_secret ne sortent JAMAIS de la DB : les modèles de réponse
-- (models.Supplier) n'exposent qu'un booléen has_api_key.
CREATE TABLE IF NOT EXISTS suppliers (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) NOT NULL,
  name TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT 'autre', -- 'eprolo' | 'aliexpress' | 'cj' | 'autre'
  api_key TEXT,
  api_secret TEXT,
  contact_email TEXT,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_suppliers_user ON suppliers(user_id, created_at DESC);
ALTER TABLE suppliers ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own rows only" ON suppliers;
CREATE POLICY "own rows only" ON suppliers FOR ALL USING (auth.uid() = user_id);

-- === SUPPLIER_PRODUCTS — catalogue fournisseur (saisie manuelle ou import API) ===
CREATE TABLE IF NOT EXISTS supplier_products (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) NOT NULL,
  supplier_id UUID REFERENCES suppliers(id) ON DELETE CASCADE NOT NULL,
  supplier_product_id TEXT,          -- identifiant côté fournisseur (import API)
  name TEXT NOT NULL,
  description TEXT,
  base_price DECIMAL(10,2) NOT NULL DEFAULT 0,
  currency TEXT DEFAULT 'EUR',
  weight_grams INTEGER,
  dimensions_cm JSONB,               -- {length, width, height}
  variants JSONB DEFAULT '[]'::jsonb, -- [{color, size, stock, price}]
  images JSONB DEFAULT '[]'::jsonb,   -- [url1, url2]
  moq INTEGER DEFAULT 1,             -- minimum order quantity
  lead_time_days INTEGER,
  linked_etsy_listing_id BIGINT,     -- = listings.etsy_listing_id (TEXT côté listings, casté en lecture)
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_supplier_products_user ON supplier_products(user_id, supplier_id);
CREATE INDEX IF NOT EXISTS idx_supplier_products_listing ON supplier_products(user_id, linked_etsy_listing_id);
-- Un même produit fournisseur (import API) n'est jamais dupliqué à la resync.
-- Index PLEIN (pas de WHERE) : PostgREST génère ON CONFLICT (cols) sans
-- prédicat, ce qu'un index partiel ne permet pas d'inférer (même leçon que
-- idx_listings_user_etsy_id). Les NULL (produits saisis à la main) sont
-- distincts entre eux pour un index unique : aucun conflit.
CREATE UNIQUE INDEX IF NOT EXISTS idx_supplier_products_ext_id
  ON supplier_products(supplier_id, supplier_product_id);
ALTER TABLE supplier_products ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own rows only" ON supplier_products;
CREATE POLICY "own rows only" ON supplier_products FOR ALL USING (auth.uid() = user_id);

-- === SUPPLIER_CONVERSATIONS — questions / devis envoyés aux fournisseurs ===
CREATE TABLE IF NOT EXISTS supplier_conversations (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) NOT NULL,
  supplier_id UUID REFERENCES suppliers(id) ON DELETE CASCADE NOT NULL,
  supplier_product_id UUID REFERENCES supplier_products(id) ON DELETE SET NULL,
  subject TEXT NOT NULL,
  message TEXT NOT NULL,
  direction TEXT NOT NULL DEFAULT 'sent',  -- 'sent' | 'received'
  status TEXT NOT NULL DEFAULT 'open',     -- 'open' | 'answered' | 'closed'
  email_sent BOOLEAN DEFAULT FALSE,        -- true si l'email SMTP est parti
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_supplier_conversations_user ON supplier_conversations(user_id, created_at DESC);
ALTER TABLE supplier_conversations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own rows only" ON supplier_conversations;
CREATE POLICY "own rows only" ON supplier_conversations FOR ALL USING (auth.uid() = user_id);

-- === SHIPPING_PROFILES — profils de livraison par pays ===
CREATE TABLE IF NOT EXISTS shipping_profiles (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) NOT NULL,
  name TEXT NOT NULL,                       -- ex. "Livraison gratuite Europe", "Mondial Standard"
  is_free_shipping BOOLEAN DEFAULT FALSE,
  target_margin_pct DECIMAL(5,2) DEFAULT 35.0,
  countries JSONB DEFAULT '[]'::jsonb,      -- [{code, name, shipping_cost, delivery_days_min, delivery_days_max}]
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shipping_profiles_user ON shipping_profiles(user_id, created_at DESC);
ALTER TABLE shipping_profiles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own rows only" ON shipping_profiles;
CREATE POLICY "own rows only" ON shipping_profiles FOR ALL USING (auth.uid() = user_id);

-- === PRODUCT_COST_ANALYSIS — historique des analyses prix/marge par pays ===
CREATE TABLE IF NOT EXISTS product_cost_analysis (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) NOT NULL,
  etsy_listing_id BIGINT,
  supplier_product_id UUID REFERENCES supplier_products(id) ON DELETE SET NULL,
  cost_price DECIMAL(10,2) NOT NULL,        -- prix fournisseur
  target_margin_pct DECIMAL(5,2) DEFAULT 35.0,
  shipping_profile_id UUID REFERENCES shipping_profiles(id) ON DELETE SET NULL,
  analysis JSONB,                           -- résultat calculé par pays (models.PricingAnalyzeResponse)
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_product_cost_analysis_user ON product_cost_analysis(user_id, etsy_listing_id, created_at DESC);
ALTER TABLE product_cost_analysis ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own rows only" ON product_cost_analysis;
CREATE POLICY "own rows only" ON product_cost_analysis FOR ALL USING (auth.uid() = user_id);

-- === PRODUCT_TEST_RESULTS — historique des simulations d'achat ===
-- Alimente GET /api/product-test/results/{listing_id}. Une simulation ne
-- passe JAMAIS de commande Etsy (l'API ne le permet pas) : seuls les calculs
-- de frais sont stockés.
CREATE TABLE IF NOT EXISTS product_test_results (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) NOT NULL,
  listing_id UUID REFERENCES listings(id) ON DELETE CASCADE,
  etsy_listing_id BIGINT,
  destination_country TEXT NOT NULL,
  variant_selected TEXT,
  buyer_total DECIMAL(10,2) NOT NULL,
  seller_net DECIMAL(10,2) NOT NULL,
  margin_eur DECIMAL(10,2),
  margin_pct DECIMAL(6,2),
  report JSONB,                             -- rapport complet (models.ProductTestResult)
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_product_test_results_user ON product_test_results(user_id, listing_id, created_at DESC);
ALTER TABLE product_test_results ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own rows only" ON product_test_results;
CREATE POLICY "own rows only" ON product_test_results FOR ALL USING (auth.uid() = user_id);

-- Force PostgREST à recharger le cache de schéma (sinon les nouvelles tables
-- restent invisibles pour l'API jusqu'au prochain redémarrage).
NOTIFY pgrst, 'reload schema';
