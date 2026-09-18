-- =====================================================================
-- === DATABASE_SCHEMA.SQL — Tables Supabase additionnelles ===
-- =====================================================================
-- À exécuter dans l'éditeur SQL de Supabase (ou via une migration).
-- Ne remplace aucune table existante — additif uniquement.
--
-- Idempotent par construction : CREATE TABLE IF NOT EXISTS ne modifie
-- JAMAIS une table déjà existante, même si son schéma diverge (colonne
-- manquante, etc.) — c'est exactement ce qui a fait échouer une exécution
-- précédente avec "column listing_id does not exist" : la table
-- `promotions` existait déjà (créée autrement, sans cette colonne), donc
-- son CREATE TABLE n'a rien fait, et le CREATE INDEX qui suivait a échoué
-- contre la vraie table. Chaque table est donc suivie d'ALTER TABLE ...
-- ADD COLUMN IF NOT EXISTS pour CHAQUE colonne, qui s'applique que la
-- table vienne d'être créée ou qu'elle existait déjà sous une forme
-- incomplète. Les index sont nommés explicitement et posés avec
-- IF NOT EXISTS pour la même raison (Postgres n'a pas de "CREATE INDEX
-- IF NOT EXISTS" sans nom explicite).

-- === AI_COSTS — suivi du coût des générations IA (image + fiche) ===
CREATE TABLE IF NOT EXISTS ai_costs (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id),
  type TEXT NOT NULL,  -- 'image' | 'listing'
  cost_eur DECIMAL(10,4) NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE ai_costs ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE ai_costs ADD COLUMN IF NOT EXISTS type TEXT;
ALTER TABLE ai_costs ADD COLUMN IF NOT EXISTS cost_eur DECIMAL(10,4) DEFAULT 0;
ALTER TABLE ai_costs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_ai_costs_user_created ON ai_costs(user_id, created_at DESC);

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
  published_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS listing_id TEXT DEFAULT '';
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS platform TEXT DEFAULT 'organic';
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS external_id TEXT;
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'draft';
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS impressions INTEGER DEFAULT 0;
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS clicks INTEGER DEFAULT 0;
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS spend_eur DECIMAL(10,2) DEFAULT 0;
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS revenue_eur DECIMAL(10,2) DEFAULT 0;
-- roas est un GENERATED column : ajouté après spend_eur/revenue_eur, qui
-- doivent déjà exister sur la table au moment où celui-ci est évalué.
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS roas DECIMAL(6,2)
  GENERATED ALWAYS AS (CASE WHEN spend_eur > 0 THEN revenue_eur / spend_eur ELSE 0 END) STORED;
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
ALTER TABLE promotions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_promotions_user_listing ON promotions(user_id, listing_id);
CREATE INDEX IF NOT EXISTS idx_promotions_user_platform ON promotions(user_id, platform);

-- === PROFILES — plan d'abonnement Stripe (Free / Pro) ===
CREATE TABLE IF NOT EXISTS profiles (
  id UUID REFERENCES auth.users(id) PRIMARY KEY,
  plan TEXT DEFAULT 'free',
  plan_started_at TIMESTAMPTZ,
  stripe_customer_id TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free';
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS plan_started_at TIMESTAMPTZ;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();

-- =====================================================================
-- Tables fondamentales (Phase 1) — jamais migrées, cause du 500 sur
-- /api/auth/etsy/callback, /api/listings/, /api/orders/, /api/analytics/summary
-- (PGRST205 "Could not find the table ... in the schema cache").
-- =====================================================================

-- === ETSY_TOKENS — tokens OAuth Etsy par utilisateur (voir routers/auth.py) ===
-- Jamais exposée en lecture publique : contient un refresh_token valable
-- plusieurs mois. user_id est la clé primaire car upsert() y est appelé
-- sans on_conflict explicite (une seule boutique Etsy par compte EtsyDrop).
-- shop_id/shop_name : GET /v3/application/users/{user_id}/shops renvoie 403
-- au tier Etsy actuel de cette app (confirmé en prod), et shop_id n'est PAS
-- dans la réponse du token exchange (vérifié contre la doc Etsy réelle).
-- Résolu une fois à la connexion via l'endpoint public GET /shops/{shop_name}
-- (même mécanisme que routers/shop_analyzer.py, pas soumis aux restrictions
-- de tier OAuth) à partir du nom de boutique saisi par l'utilisateur, puis
-- stocké ici pour ne plus jamais avoir à le résoudre.
CREATE TABLE IF NOT EXISTS etsy_tokens (
  user_id UUID REFERENCES auth.users(id) PRIMARY KEY,
  access_token TEXT NOT NULL,
  refresh_token TEXT NOT NULL,
  expires_in INTEGER,
  shop_id BIGINT,
  shop_name TEXT,
  updated_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE etsy_tokens ADD COLUMN IF NOT EXISTS access_token TEXT DEFAULT '';
ALTER TABLE etsy_tokens ADD COLUMN IF NOT EXISTS refresh_token TEXT DEFAULT '';
ALTER TABLE etsy_tokens ADD COLUMN IF NOT EXISTS expires_in INTEGER;
ALTER TABLE etsy_tokens ADD COLUMN IF NOT EXISTS shop_id BIGINT;
ALTER TABLE etsy_tokens ADD COLUMN IF NOT EXISTS shop_name TEXT;
ALTER TABLE etsy_tokens ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- === LISTINGS — fiches produit du catalogue ===
-- etsy_listing_id : renseigné uniquement pour les fiches importées depuis
-- la boutique Etsy connectée (voir routers/auth.py > _sync_etsy_listings) ;
-- NULL pour les fiches créées à la main via POST /api/listings/. Contrainte
-- unique par utilisateur pour permettre un upsert idempotent lors des
-- resynchronisations (sans quoi chaque connexion Etsy dupliquerait les
-- lignes au lieu de les mettre à jour).
CREATE TABLE IF NOT EXISTS listings (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) NOT NULL,
  etsy_listing_id TEXT,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  tags TEXT[] DEFAULT '{}',
  price_min DECIMAL(10,2) NOT NULL,
  price_max DECIMAL(10,2) NOT NULL,
  supplier TEXT NOT NULL,  -- SupplierName (my_catalog | eprolo | cj_dropshipping | printify | printful | zendrop | aliexpress)
  -- Pour une fiche importée depuis Etsy : [{label, price, quantity}] par
  -- combinaison de propriétés (voir etsy_client.py > fetch_listing_variants).
  -- Pour une fiche créée à la main : [{color, size, engraving, supplier_price}]
  -- (voir models.ListingVariant). Les deux formes cohabitent dans la même
  -- colonne JSONB — le frontend distingue via les clés présentes.
  variants JSONB DEFAULT '[]'::jsonb,
  image_url TEXT,  -- url_570xN de l'image principale Etsy (voir etsy_client.py > fetch_listing_image_url) ; NULL pour une fiche créée à la main sans image
  stock_status TEXT NOT NULL DEFAULT 'available',  -- available | low_stock | out_of_stock
  margin_pct DECIMAL(5,2) DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE listings ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE listings ADD COLUMN IF NOT EXISTS etsy_listing_id TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS name TEXT DEFAULT '';
ALTER TABLE listings ADD COLUMN IF NOT EXISTS description TEXT DEFAULT '';
ALTER TABLE listings ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT '{}';
ALTER TABLE listings ADD COLUMN IF NOT EXISTS price_min DECIMAL(10,2) DEFAULT 0;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS price_max DECIMAL(10,2) DEFAULT 0;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS supplier TEXT DEFAULT 'my_catalog';
ALTER TABLE listings ADD COLUMN IF NOT EXISTS variants JSONB DEFAULT '[]'::jsonb;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS image_url TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS stock_status TEXT DEFAULT 'available';
ALTER TABLE listings ADD COLUMN IF NOT EXISTS margin_pct DECIMAL(5,2) DEFAULT 0;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_listings_user_stock ON listings(user_id, stock_status);
-- PAS un index partiel (WHERE etsy_listing_id IS NOT NULL) : l'upsert de
-- _sync_etsy_listings (routers/auth.py) appelle .upsert(rows,
-- on_conflict="user_id,etsy_listing_id"), que supabase-py/PostgREST traduit
-- en "ON CONFLICT (user_id, etsy_listing_id) DO UPDATE ..." SANS clause
-- WHERE. Postgres n'utilise un index partiel comme arbitre ON CONFLICT que
-- si la clause ON CONFLICT porte le même WHERE au caractère près — ce que
-- PostgREST ne permet pas de spécifier. Résultat concret observé en prod :
-- Etsy renvoie bien les fiches (confirmé via test_etsy.py, listings actifs
-- non-vides), mais l'upsert échoue avec "no unique or exclusion constraint
-- matching the ON CONFLICT specification", silencieusement avalé par le
-- try/except de _sync_etsy_listings -> Catalogue reste à 0 fiche. Un index
-- unique PLEIN (sans WHERE) fonctionne ici sans compromis : Postgres traite
-- déjà chaque NULL comme distinct des autres, donc plusieurs fiches créées
-- à la main (etsy_listing_id NULL) restent autorisées.
DROP INDEX IF EXISTS idx_listings_user_etsy_id;
CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_user_etsy_id ON listings(user_id, etsy_listing_id);

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
ALTER TABLE orders ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS etsy_order_id TEXT DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_name TEXT DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS product_name TEXT DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS supplier TEXT DEFAULT 'my_catalog';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS amount DECIMAL(10,2) DEFAULT 0;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending_supplier';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS tracking_number TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at DESC);
-- Index PLEIN (pas de WHERE), pour la même raison que idx_listings_user_etsy_id
-- ci-dessus : nécessaire pour que sync_etsy_orders() puisse upserter avec
-- on_conflict="user_id,etsy_order_id" sans provoquer "no unique or exclusion
-- constraint matching the ON CONFLICT specification".
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_user_etsy_id ON orders(user_id, etsy_order_id);

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
ALTER TABLE analytics_summary ADD COLUMN IF NOT EXISTS revenue_total DECIMAL(12,2) DEFAULT 0;
ALTER TABLE analytics_summary ADD COLUMN IF NOT EXISTS net_margin_pct DECIMAL(5,2) DEFAULT 0;
ALTER TABLE analytics_summary ADD COLUMN IF NOT EXISTS conversion_rate_pct DECIMAL(5,2) DEFAULT 0;
ALTER TABLE analytics_summary ADD COLUMN IF NOT EXISTS net_profit DECIMAL(12,2) DEFAULT 0;
ALTER TABLE analytics_summary ADD COLUMN IF NOT EXISTS history JSONB DEFAULT '[]'::jsonb;
ALTER TABLE analytics_summary ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

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
ALTER TABLE keywords ADD COLUMN IF NOT EXISTS volume_monthly INTEGER DEFAULT 0;
ALTER TABLE keywords ADD COLUMN IF NOT EXISTS total_sales INTEGER DEFAULT 0;
ALTER TABLE keywords ADD COLUMN IF NOT EXISTS competition TEXT;
ALTER TABLE keywords ADD COLUMN IF NOT EXISTS trend_pct DECIMAL(5,2) DEFAULT 0;
ALTER TABLE keywords ADD COLUMN IF NOT EXISTS score INTEGER DEFAULT 0;

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
ALTER TABLE sourcing_cache ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE sourcing_cache ADD COLUMN IF NOT EXISTS product_name TEXT DEFAULT '';
ALTER TABLE sourcing_cache ADD COLUMN IF NOT EXISTS sources JSONB DEFAULT '[]'::jsonb;
ALTER TABLE sourcing_cache ADD COLUMN IF NOT EXISTS best_price JSONB;
ALTER TABLE sourcing_cache ADD COLUMN IF NOT EXISTS recommended_sell_price DECIMAL(10,2);
ALTER TABLE sourcing_cache ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_sourcing_cache_user_product ON sourcing_cache(user_id, product_name);

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
