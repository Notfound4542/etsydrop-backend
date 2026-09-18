-- =====================================================================
-- === MIGRATION 2026-09-18 — SOURCING ALIEXPRESS ===
-- =====================================================================
-- À exécuter dans l'éditeur SQL Supabase (projet wlqzhoixoxrtueldsqkv),
-- APRÈS 2026-09-18_phase2.sql (table supplier_products). Additif et
-- idempotent : rejouable sans risque.

-- === SUPPLIER_PRODUCTS — provenance et métadonnées AliExpress ===
-- source : 'manual' (saisie) | 'eprolo' (import API) | 'aliexpress'
ALTER TABLE supplier_products ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'manual';
ALTER TABLE supplier_products ADD COLUMN IF NOT EXISTS external_id VARCHAR(255);
ALTER TABLE supplier_products ADD COLUMN IF NOT EXISTS shipping_days_estimate INT;
ALTER TABLE supplier_products ADD COLUMN IF NOT EXISTS rating DECIMAL(3,2);
ALTER TABLE supplier_products ADD COLUMN IF NOT EXISTS orders_count INT;
CREATE INDEX IF NOT EXISTS idx_supplier_products_source ON supplier_products(user_id, source);

-- === ALIEXPRESS_CACHE — résultats de recherche / détail (TTL 1 h côté backend) ===
-- Pas de user_id : les résultats AliExpress sont publics et partagés entre
-- comptes (une même recherche ne re-sollicite pas AliExpress pour chacun).
-- Accès uniquement via la clé service_role du backend : RLS activée sans
-- policy = aucun accès PostgREST anon depuis le navigateur.
CREATE TABLE IF NOT EXISTS aliexpress_cache (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  query TEXT,
  results JSONB,
  cached_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_aliexpress_cache_query ON aliexpress_cache(query, cached_at DESC);
ALTER TABLE aliexpress_cache ENABLE ROW LEVEL SECURITY;

NOTIFY pgrst, 'reload schema';
