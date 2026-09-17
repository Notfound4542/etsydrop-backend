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

ETSY_API_KEY = os.getenv("ETSY_API_KEY")
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


# === APPEL GET GÉNÉRIQUE VERS L'API ETSY V3 ===
async def etsy_get(path: str, *, params: Optional[dict] = None, access_token: Optional[str] = None) -> Any:
    """
    `access_token` omis => appel "app-only" (données publiques : boutiques,
    fiches actives). Fourni => appel authentifié pour le compte du vendeur.
    Retry unique après 1s sur un 429 (rate limit Etsy).
    """
    if not ETSY_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Etsy manquante côté serveur.")

    headers = {"x-api-key": ETSY_API_KEY}
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
