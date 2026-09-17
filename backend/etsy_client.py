# =====================================================================
# === ETSY_CLIENT.PY — Appels partagés vers l'API Etsy v3 ===
# =====================================================================
#
# Centralise ce que routers/analytics.py, routers/shop_analyzer.py et
# routers/keywords.py ont en commun : authentification (x-api-key +
# éventuellement le token OAuth du vendeur) et le retry sur 429.
# Voir routers/auth.py pour le flow OAuth qui alimente la table
# `etsy_tokens` consommée ici par get_etsy_access_token().

import asyncio
import logging
import os
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from database import get_supabase

logger = logging.getLogger("etsydrop.etsy_client")


def _clean_env(name: str) -> Optional[str]:
    return os.getenv(name, "").strip().strip("\"'") or None


# Etsy exige, depuis un changement de plateforme début février 2026, que
# x-api-key porte "keystring:shared_secret" et plus seulement le keystring
# seul — sur TOUT appel, OAuth ou non. Sans le secret, Etsy renvoie
# 403 {"error":"Shared secret is required in x-api-key header."} — constaté
# en prod sur un appel public (/shops/{shop_name}), donc ça s'applique bien
# indépendamment du token OAuth du vendeur.
ETSY_API_KEY = _clean_env("ETSY_API_KEY")
ETSY_API_SECRET = _clean_env("ETSY_API_SECRET")
ETSY_API_BASE = "https://openapi.etsy.com/v3/application"


# === TOKEN OAUTH DU VENDEUR (stocké en base, jamais côté client) ===
def get_etsy_access_token(user_id: str) -> str:
    supabase = get_supabase()
    result = (
        supabase.table("etsy_tokens")
        .select("access_token")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not result.data or not result.data.get("access_token"):
        raise HTTPException(status_code=400, detail="Boutique Etsy non connectée.")
    return result.data["access_token"]


# === ID ETSY DU VENDEUR ===
# Un access token Etsy v3 est de la forme "{user_id}.{secret}" : le
# préfixe avant le point est l'ID utilisateur Etsy du propriétaire.
def etsy_shop_id_from_token(access_token: str) -> str:
    return access_token.split(".")[0]


# === SHOP_ID DE LA BOUTIQUE CONNECTÉE ===
# GET /v3/application/users/{user_id}/shops renvoie 403 au tier Etsy actuel
# de cette app — shop_id est donc résolu une seule fois à la connexion (voir
# routers/auth.py > etsy_callback, via l'endpoint public /shops/{shop_name})
# et lu ici depuis la DB plutôt que re-résolu à chaque appel.
def get_etsy_shop_id(user_id: str) -> int:
    supabase = get_supabase()
    result = (
        supabase.table("etsy_tokens")
        .select("shop_id")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    shop_id = result.data.get("shop_id") if result.data else None
    if not shop_id:
        raise HTTPException(
            status_code=400,
            detail="Shop Etsy non résolu — reconnecte ta boutique en indiquant son nom exact.",
        )
    return shop_id


# === IMPORT DES FICHES ACTIVES D'UNE BOUTIQUE CONNECTÉE ===
# Partagé entre routers/auth.py (déclenché automatiquement juste après le
# callback OAuth) et routers/listings.py > POST /sync (déclenchement manuel,
# nécessaire quand shop_id/le token ont été mis à jour autrement qu'en
# repassant par tout le flow OAuth — ex. un correctif SQL direct en base).
async def sync_etsy_listings(user_id: str, access_token: str, shop_id: Optional[int]) -> int:
    """
    Importe les fiches actives de la boutique Etsy connectée dans la table
    `listings`. Upsert sur (user_id, etsy_listing_id) — voir l'index unique
    (PLEIN, pas partiel — voir database_schema.sql) dans database_schema.sql —
    donc une resynchronisation met à jour les fiches déjà importées au lieu
    de les dupliquer.

    Ne lève jamais : appelée aussi bien depuis le callback OAuth (qui ne doit
    jamais planter pour un souci de sync) que depuis un endpoint manuel (qui,
    lui, doit pouvoir remonter un compte de 0 sans crasher).
    """
    if not shop_id:
        logger.warning("Sync listings Etsy ignorée pour user_id=%s : shop_id non résolu.", user_id)
        return 0

    try:
        payload = await etsy_get(
            f"/shops/{shop_id}/listings/active", access_token=access_token, params={"limit": 100}
        )
        etsy_listings = payload.get("results", []) if isinstance(payload, dict) else []
    except Exception as exc:
        logger.warning("Sync listings Etsy échouée pour user_id=%s : %s: %s", user_id, type(exc).__name__, exc)
        return 0

    # Les fiches importées doivent rester compatibles avec le modèle Listing
    # (voir models.py) — sans ça, la lecture ultérieure via GET /api/listings/
    # plante en ResponseValidationError (500 générique) au lieu de renvoyer
    # les données. D'où les tailles/valeurs par défaut ci-dessous.
    rows = []
    for item in etsy_listings:
        try:
            listing_id = str(item.get("listing_id") or "")
            if not listing_id:
                continue

            title = (item.get("title") or "Fiche Etsy sans titre").strip()[:140] or "Fiche Etsy"
            if len(title) < 3:
                title = title.ljust(3, ".")

            description = (item.get("description") or "").strip()[:2000]
            if len(description) < 10:
                description = f"{title} — fiche importée depuis Etsy."

            price_data = item.get("price")
            if isinstance(price_data, dict) and "amount" in price_data:
                price = float(price_data["amount"]) / float(price_data.get("divisor", 100) or 100)
            else:
                price = float(item.get("price") or 0)

            tags = [t for t in (item.get("tags") or []) if t][:13] or ["etsy import"]

            quantity = int(item.get("quantity") or 0)
            if quantity <= 0:
                stock_status = "out_of_stock"
            elif quantity < 5:
                stock_status = "low_stock"
            else:
                stock_status = "available"

            rows.append(
                {
                    "user_id": user_id,
                    "etsy_listing_id": listing_id,
                    "name": title,
                    "description": description,
                    "tags": tags,
                    "price_min": round(price, 2),
                    "price_max": round(price, 2),
                    "supplier": "my_catalog",
                    "variants": [],
                    "stock_status": stock_status,
                    "margin_pct": 0,
                }
            )
        except (TypeError, ValueError):
            continue

    if not rows:
        return 0

    try:
        get_supabase().table("listings").upsert(rows, on_conflict="user_id,etsy_listing_id").execute()
    except Exception as exc:
        # error (pas warning) + exc_info : ce upsert échouait silencieusement en
        # prod (index unique partiel incompatible avec ON CONFLICT sans WHERE —
        # voir database_schema.sql > idx_listings_user_etsy_id) sans que rien ne
        # le distingue d'un simple rate limit dans les logs.
        logger.error(
            "Écriture des listings Etsy échouée pour user_id=%s : %s: %s",
            user_id, type(exc).__name__, exc,
            exc_info=True,
        )
        return 0

    return len(rows)


# === APPEL GET GÉNÉRIQUE VERS L'API ETSY V3 ===
async def etsy_get(path: str, *, params: Optional[dict] = None, access_token: Optional[str] = None) -> Any:
    """
    `access_token` omis => appel "app-only" (données publiques : boutiques,
    fiches actives). Fourni => appel authentifié pour le compte du vendeur.
    Retry unique après 1s sur un 429 (rate limit Etsy).
    """
    if not ETSY_API_KEY or not ETSY_API_SECRET:
        raise HTTPException(status_code=500, detail="Configuration Etsy manquante côté serveur.")

    headers = {"x-api-key": f"{ETSY_API_KEY}:{ETSY_API_SECRET}"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    url = f"{ETSY_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, headers=headers, params=params)
        if response.status_code == 429:
            await asyncio.sleep(1)
            response = await client.get(url, headers=headers, params=params)

    if response.status_code != 200:
        # Etsy renvoie généralement un corps JSON expliquant le rejet (scope
        # manquant, tier d'app insuffisant, etc.) — sans ce détail, un 403/404
        # générique de leur côté est indiscernable d'un autre depuis nos logs.
        # Jamais renvoyé au client (juste loggé) : voir la règle CLAUDE.md sur
        # ne pas exposer les réponses d'API tierces telles quelles.
        try:
            error_body = response.text[:500]
        except Exception:
            error_body = "<illisible>"
        logger.warning("Etsy API %s a répondu %s : %s", path, response.status_code, error_body)
        raise HTTPException(status_code=502, detail="Échec de la requête vers l'API Etsy.")

    return response.json()
