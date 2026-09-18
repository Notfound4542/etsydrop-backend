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


# === IMAGE PRINCIPALE D'UNE FICHE ===
# GET /listings/{listing_id}/images est un endpoint public (x-api-key
# seulement) distinct de /shops/{shop_id}/listings/active : Etsy n'inclut
# jamais les images dans la réponse listing elle-même (confirmé contre le
# schéma ShopListing officiel — aucun champ "images"), il faut un appel par
# fiche. url_570xN est un bon compromis qualité/poids pour une card de
# catalogue (url_fullxfull peut peser plusieurs Mo).
async def fetch_listing_image_url(listing_id: str) -> Optional[str]:
    try:
        payload = await etsy_get(f"/listings/{listing_id}/images")
        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not results:
            return None
        return results[0].get("url_570xN") or results[0].get("url_fullxfull")
    except Exception as exc:
        logger.warning("Récupération image échouée pour listing_id=%s : %s: %s", listing_id, type(exc).__name__, exc)
        return None


# === VARIANTES (INVENTAIRE) D'UNE FICHE ===
# GET /listings/{listing_id}/inventory renvoie products[] (une combinaison
# de propriétés, ex. Couleur=Or/Taille=M) x offerings[] (prix/stock pour
# cette combinaison) — pas de "sku"/"variations" plat sur le listing
# lui-même (confirmé contre le schéma officiel ListingInventory). On
# aplatit chaque (produit, offering activée) en une entrée simple ; les
# noms de propriété (property_name) sont ceux de LA boutique (Color, Size,
# Material...), jamais figés à color/size/engraving comme le suppose le
# modèle de saisie manuelle (voir models.Listing.variants).
async def fetch_listing_variants(listing_id: str) -> list:
    try:
        payload = await etsy_get(f"/listings/{listing_id}/inventory")
    except Exception as exc:
        logger.warning("Récupération inventaire échouée pour listing_id=%s : %s: %s", listing_id, type(exc).__name__, exc)
        return []

    products = payload.get("products", []) if isinstance(payload, dict) else []
    variants = []
    for product in products:
        if product.get("is_deleted"):
            continue
        property_values = product.get("property_values", []) or []
        label = " / ".join(
            f"{pv.get('property_name')}: {', '.join(pv.get('values', []))}"
            for pv in property_values
            if pv.get("property_name") and pv.get("values")
        ) or "Standard"

        for offering in product.get("offerings", []) or []:
            if not offering.get("is_enabled") or offering.get("is_deleted"):
                continue
            price_data = offering.get("price") or {}
            try:
                price = float(price_data.get("amount", 0)) / float(price_data.get("divisor", 100) or 100)
            except (TypeError, ValueError):
                price = 0.0
            variants.append(
                {
                    "label": label[:120],
                    "price": round(price, 2),
                    "quantity": int(offering.get("quantity") or 0),
                }
            )
    return variants


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

            # Deux appels Etsy supplémentaires par fiche (image + inventaire) :
            # aucun des deux n'est inclus dans /listings/active (confirmé
            # contre le schéma ShopListing officiel). Chacun est indépendant
            # et n'empêche jamais l'import de la fiche elle-même s'il échoue
            # (voir fetch_listing_image_url / fetch_listing_variants — aucun
            # des deux ne lève).
            image_url = await fetch_listing_image_url(listing_id)
            variants = await fetch_listing_variants(listing_id)
            variant_prices = [v["price"] for v in variants if v.get("price")]
            if variant_prices:
                price_min, price_max = min(variant_prices), max(variant_prices)
            else:
                price_min = price_max = price

            rows.append(
                {
                    "user_id": user_id,
                    "etsy_listing_id": listing_id,
                    "name": title,
                    "description": description,
                    "tags": tags,
                    "price_min": round(price_min, 2),
                    "price_max": round(price_max, 2),
                    "supplier": "my_catalog",
                    "variants": variants,
                    "image_url": image_url,
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


# === STATUT DE COMMANDE (mapping receipt Etsy -> OrderStatus) ===
# Etsy n'a pas d'énum de statut équivalente à la nôtre (pending_supplier |
# in_transit | delivered | delayed | return_requested) : `status` côté Etsy
# ne parle que du PAIEMENT (paid/open/canceled/refunded...), et `is_shipped`
# est un simple booléen — Etsy ne dit jamais "livré" avec certitude (pas de
# preuve de livraison exposée par l'API), donc "delivered" n'est JAMAIS
# déduit ici pour ne pas prétendre savoir ce qu'on ne sait pas.
def _map_receipt_status(receipt: dict) -> str:
    status = (receipt.get("status") or "").lower()
    if status in ("fully refunded", "partially refunded", "canceled"):
        return "return_requested"
    if receipt.get("is_shipped"):
        return "in_transit"
    return "pending_supplier"


# === IMPORT DES COMMANDES (RECEIPTS) D'UNE BOUTIQUE CONNECTÉE ===
# Partagé entre routers/auth.py (déclenché automatiquement juste après le
# callback OAuth, comme sync_etsy_listings) et routers/orders.py > POST /sync
# (déclenchement manuel).
async def sync_etsy_orders(user_id: str, access_token: str, shop_id: Optional[int]) -> int:
    """
    Importe les commandes payées de la boutique Etsy connectée dans la table
    `orders`. Upsert sur (user_id, etsy_order_id) — voir l'index unique dans
    database_schema.sql. Ne lève jamais, pour les mêmes raisons que
    sync_etsy_listings.
    """
    if not shop_id:
        logger.warning("Sync orders Etsy ignorée pour user_id=%s : shop_id non résolu.", user_id)
        return 0

    try:
        payload = await etsy_get(
            f"/shops/{shop_id}/receipts",
            access_token=access_token,
            params={"limit": 25, "was_paid": True},
        )
        receipts = payload.get("results", []) if isinstance(payload, dict) else []
    except Exception as exc:
        logger.warning("Sync orders Etsy échouée pour user_id=%s : %s: %s", user_id, type(exc).__name__, exc)
        return 0

    rows = []
    for receipt in receipts:
        try:
            receipt_id = str(receipt.get("receipt_id") or "")
            if not receipt_id:
                continue

            titles = [t.get("title") for t in (receipt.get("transactions") or []) if t.get("title")]
            product_name = ", ".join(titles)[:140] if titles else "Commande Etsy"

            grandtotal = receipt.get("grandtotal") or {}
            try:
                amount = float(grandtotal.get("amount", 0)) / float(grandtotal.get("divisor", 100) or 100)
            except (TypeError, ValueError):
                amount = 0.0

            shipments = receipt.get("shipments") or []
            tracking_number = shipments[0].get("tracking_code") if shipments else None

            # Jamais l'email acheteur (voir CLAUDE.md > pas de données
            # personnelles sensibles au-delà de ce qui sert le fulfillment) —
            # `name` est déjà le nom du destinataire de l'expédition, requis
            # pour préparer un colis, pas une donnée superflue.
            customer_name = (receipt.get("name") or "Acheteur Etsy").strip()[:120] or "Acheteur Etsy"

            rows.append(
                {
                    "user_id": user_id,
                    "etsy_order_id": receipt_id,
                    "customer_name": customer_name,
                    "product_name": product_name,
                    "supplier": "my_catalog",
                    "amount": round(amount, 2),
                    "status": _map_receipt_status(receipt),
                    "tracking_number": tracking_number,
                }
            )
        except (TypeError, ValueError):
            continue

    if not rows:
        return 0

    try:
        get_supabase().table("orders").upsert(rows, on_conflict="user_id,etsy_order_id").execute()
    except Exception as exc:
        logger.error(
            "Écriture des orders Etsy échouée pour user_id=%s : %s: %s",
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
