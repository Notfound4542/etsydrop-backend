-- =====================================================================
-- === MIGRATION 2026-09-18 — Refonte catalogue / commandes / réseaux ===
-- =====================================================================
-- Delta à exécuter dans l'éditeur SQL Supabase. Strictement additif et
-- idempotent (IF NOT EXISTS partout) : rejouable sans risque. Le schéma
-- complet de référence reste backend/database_schema.sql (lui aussi
-- rejouable, mais plus long).

-- === LISTINGS — coût fournisseur & marge cible (saisie manuelle) ===
ALTER TABLE listings ADD COLUMN IF NOT EXISTS image_url TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS variants JSONB DEFAULT '[]'::jsonb;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS cost_price DECIMAL(10,2);
ALTER TABLE listings ADD COLUMN IF NOT EXISTS target_margin DECIMAL(5,2) DEFAULT 35.0;

-- === ORDERS — données brutes du receipt Etsy ===
CREATE TABLE IF NOT EXISTS orders (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) NOT NULL,
  etsy_order_id TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  product_name TEXT NOT NULL,
  supplier TEXT NOT NULL,
  amount DECIMAL(10,2) NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_supplier',
  tracking_number TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_price DECIMAL(10,2);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'EUR';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS etsy_status TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS buyer_email TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS items JSONB DEFAULT '[]'::jsonb;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_timestamp BIGINT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_user_etsy_id ON orders(user_id, etsy_order_id);
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own rows only" ON orders;
CREATE POLICY "own rows only" ON orders FOR ALL USING (auth.uid() = user_id);

-- === SOCIAL_POSTS — section "Réseaux" (préparée, pas encore alimentée) ===
CREATE TABLE IF NOT EXISTS social_posts (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id),
  listing_id BIGINT,
  platform TEXT,  -- 'instagram' | 'tiktok' | 'pinterest'
  content TEXT,
  scheduled_at TIMESTAMPTZ,
  status TEXT DEFAULT 'draft',
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_social_posts_user_scheduled ON social_posts(user_id, scheduled_at);
ALTER TABLE social_posts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own rows only" ON social_posts;
CREATE POLICY "own rows only" ON social_posts FOR ALL USING (auth.uid() = user_id);

-- Force PostgREST à recharger le cache de schéma (sinon les nouvelles
-- colonnes restent invisibles pour l'API jusqu'au prochain redémarrage).
NOTIFY pgrst, 'reload schema';
