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
import html
import logging
import os
from datetime import datetime, timezone
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
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"

# Un access token Etsy v3 ne vit qu'UNE HEURE (expires_in=3600). Sans refresh,
# tout appel authentifié échoue en 401 "access token is expired" dès la
# deuxième heure après la connexion — c'est exactement ce qui faisait renvoyer
# synced: 0 à POST /api/listings/sync en prod. On rafraîchit un peu avant
# l'échéance pour ne jamais partir avec un token à la limite.
_TOKEN_REFRESH_MARGIN_SECONDS = 120

# Etsy applique une limite par SECONDE nettement plus basse que les 10 req/s
# annoncés pour une app au tier actuel : 4 fiches en parallèle (= 8 appels
# simultanés image+inventaire) déclenchaient déjà des 429 en rafale, testé
# en direct. 2 fiches en parallèle + backoff sur 429 (voir etsy_get) passe.
_ETSY_CONCURRENCY = 2
_ETSY_429_RETRIES = 3


# === REFRESH DU TOKEN OAUTH (grant_type=refresh_token) ===
async def refresh_etsy_token(user_id: str, refresh_token: str) -> str:
    if not ETSY_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Etsy manquante côté serveur.")

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            ETSY_TOKEN_URL,
            data={"grant_type": "refresh_token", "client_id": ETSY_API_KEY, "refresh_token": refresh_token},
        )

    if response.status_code != 200:
        # Le corps Etsy est loggé (jamais renvoyé au client) : un refresh_token
        # révoqué (utilisateur qui a retiré l'app côté Etsy) et un client_id
        # invalide donnent le même 400 côté HTTP mais pas le même message.
        logger.warning("Refresh du token Etsy échoué pour user_id=%s : %s %s", user_id, response.status_code, response.text[:300])
        raise HTTPException(status_code=401, detail="Session Etsy expirée — reconnecte ta boutique.")

    tokens = response.json()
    new_access = tokens.get("access_token")
    new_refresh = tokens.get("refresh_token") or refresh_token
    if not new_access:
        raise HTTPException(status_code=502, detail="Réponse Etsy invalide lors du refresh du token.")

    try:
        get_supabase().table("etsy_tokens").update(
            {
                "access_token": new_access,
                "refresh_token": new_refresh,
                "expires_in": tokens.get("expires_in"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("user_id", user_id).execute()
    except Exception as exc:
        logger.error("Persistance du token Etsy rafraîchi échouée pour user_id=%s : %s: %s", user_id, type(exc).__name__, exc, exc_info=True)

    logger.info("Token Etsy rafraîchi pour user_id=%s (expires_in=%s).", user_id, tokens.get("expires_in"))
    return new_access


# === TOKEN OAUTH DU VENDEUR (stocké en base, jamais côté client) ===
async def get_etsy_access_token(user_id: str) -> str:
    """Retourne un access token VALIDE : rafraîchi automatiquement s'il est expiré ou sur le point de l'être."""
    supabase = get_supabase()
    result = (
        supabase.table("etsy_tokens")
        .select("access_token,refresh_token,expires_in,updated_at")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    row = result.data if result and result.data else None
    if not row or not row.get("access_token"):
        raise HTTPException(status_code=400, detail="Boutique Etsy non connectée.")

    expires_in = int(row.get("expires_in") or 3600)
    updated_at_raw = row.get("updated_at")
    expired = True
    if updated_at_raw:
        try:
            updated_at = datetime.fromisoformat(str(updated_at_raw).replace("Z", "+00:00"))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
            expired = age >= (expires_in - _TOKEN_REFRESH_MARGIN_SECONDS)
        except (TypeError, ValueError):
            expired = True

    if expired and row.get("refresh_token"):
        return await refresh_etsy_token(user_id, row["refresh_token"])
    return row["access_token"]


# === TOKEN OAUTH « BEST EFFORT » (jamais d'exception) ===
# Pour les chemins où OAuth est un BONUS et pas un prérequis (sync des
# fiches : listings/active et /images sont publics, seul /inventory exige le
# token). Retourne (token_ou_None, raison_lisible_ou_None) — la raison est
# renvoyée au frontend dans SyncResult.errors pour qu'un synced=0 ne soit plus
# jamais silencieux.
async def try_get_etsy_access_token(user_id: str) -> tuple:
    try:
        return await get_etsy_access_token(user_id), None
    except HTTPException as exc:
        reason = f"OAuth Etsy indisponible ({exc.detail}) — reconnexion Etsy requise pour les variantes et les commandes."
        logger.warning("try_get_etsy_access_token user_id=%s : %s", user_id, exc.detail)
        return None, reason
    except Exception as exc:  # noqa: BLE001 — jamais bloquant
        logger.warning("try_get_etsy_access_token user_id=%s : %s: %s", user_id, type(exc).__name__, exc)
        return None, "OAuth Etsy indisponible (erreur interne) — reconnexion Etsy requise pour les variantes et les commandes."


# === LECTURE PUBLIQUE (x-api-key SEULEMENT, jamais de token OAuth) ===
# Endpoints Etsy v3 qui n'exigent PAS d'OAuth (vérifié en direct le
# 2026-09-18 sur shop_id 67891193) :
#   - GET /shops/{shop_id}/listings/active
#   - GET /listings/{listing_id}/images
#   - GET /shops?shop_name=...
# En revanche GET /listings/{id}/inventory renvoie 401 « requires scope
# listings_r » en app-only : il reste sur etsy_get(access_token=...).
# Isoler ces lectures garantit que la sync du catalogue ne dépend plus de
# l'état du token OAuth (cause historique de {synced:0, received:0}).
async def fetch_public(endpoint: str, *, params: Optional[dict] = None) -> Any:
    return await etsy_get(endpoint, params=params, access_token=None)


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
        payload = await fetch_public(f"/listings/{listing_id}/images")
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
#
# Contrairement à /images, /inventory EXIGE le token OAuth du vendeur
# (scope listings_r) : en app-only Etsy renvoie 401 — vérifié en direct,
# c'est pour ça que toutes les fiches importées avaient variants=[].
async def fetch_listing_variants(listing_id: str, access_token: Optional[str]) -> list:
    # Sans token OAuth valide (expiré + refresh échoué) on ne tente même pas
    # l'appel : Etsy répondrait 401 et la fiche serait importée sans variantes
    # de toute façon — voir sync_etsy_listings (errors[]).
    if not access_token:
        return []
    try:
        payload = await etsy_get(f"/listings/{listing_id}/inventory", access_token=access_token)
    except Exception as exc:
        logger.warning("Récupération inventaire échouée pour listing_id=%s : %s: %s", listing_id, type(exc).__name__, exc)
        return []

    products = payload.get("products", []) if isinstance(payload, dict) else []
    variants = []
    for product in products:
        if product.get("is_deleted"):
            continue
        property_values = product.get("property_values", []) or []
        properties = {
            str(pv.get("property_name"))[:60]: ", ".join(str(v) for v in pv.get("values", []))[:80]
            for pv in property_values
            if pv.get("property_name") and pv.get("values")
        }
        label = " / ".join(f"{k}: {v}" for k, v in properties.items()) or "Standard"

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
                    # Forme structurée en plus du label : permet au frontend
                    # de résumer "Color: Gold, Silver / Size: S, M, L" sans
                    # re-parser une chaîne.
                    "properties": properties,
                }
            )
    return variants


# === IMPORT DES FICHES ACTIVES D'UNE BOUTIQUE CONNECTÉE ===
# Partagé entre routers/auth.py (déclenché automatiquement juste après le
# callback OAuth) et routers/listings.py > POST /sync (déclenchement manuel,
# nécessaire quand shop_id/le token ont été mis à jour autrement qu'en
# repassant par tout le flow OAuth — ex. un correctif SQL direct en base).
async def sync_etsy_listings(user_id: str, access_token: Optional[str], shop_id: Optional[int]) -> dict:
    """
    Importe les fiches actives de la boutique Etsy connectée dans la table
    `listings`. Upsert sur (user_id, etsy_listing_id) — voir l'index unique
    (PLEIN, pas partiel — voir database_schema.sql) dans database_schema.sql —
    donc une resynchronisation met à jour les fiches déjà importées au lieu
    de les dupliquer. `synced` compte toutes les fiches écrites (INSERT et
    UPDATE confondus), pas seulement les nouvelles.

    Retourne {synced, received, with_image, with_variants, oauth_used, errors}.
    Ne lève jamais : appelée aussi bien depuis le callback OAuth (qui ne doit
    jamais planter pour un souci de sync) que depuis un endpoint manuel (qui,
    lui, doit pouvoir remonter un compte de 0 sans crasher).

    `access_token` est OPTIONNEL : la liste des fiches actives et les images
    sont lues avec la clé d'app seule (fetch_public) ; le token ne sert qu'aux
    variantes (/inventory). Un token expiré/None n'empêche donc plus l'import
    du catalogue. `errors` explique en clair chaque compteur à 0 (aucune fiche
    active ? erreur API ? rate limit ? écriture DB ?) — renvoyé au frontend.
    """
    errors: list = []
    empty = {"synced": 0, "received": 0, "with_image": 0, "with_variants": 0, "oauth_used": bool(access_token), "errors": errors}
    if not shop_id:
        logger.warning("Sync listings Etsy ignorée pour user_id=%s : shop_id non résolu.", user_id)
        errors.append("shop_id non résolu en base — reconnecte ta boutique en indiquant son nom exact.")
        return empty
    if not access_token:
        errors.append("Token OAuth absent ou expiré : fiches et images importées avec la clé d'app, variantes ignorées.")

    try:
        payload = await fetch_public(f"/shops/{shop_id}/listings/active", params={"limit": 100})
        etsy_listings = payload.get("results", []) if isinstance(payload, dict) else []
    except HTTPException as exc:
        logger.warning("Sync listings Etsy échouée pour user_id=%s : HTTP %s %s", user_id, exc.status_code, exc.detail)
        if exc.status_code == 429:
            errors.append("Rate limit Etsy atteint sur /listings/active — réessaie dans une minute.")
        elif exc.status_code == 500:
            errors.append("ETSY_API_KEY / ETSY_API_SECRET manquants côté serveur.")
        else:
            errors.append(f"Etsy a refusé GET /shops/{shop_id}/listings/active ({exc.detail}) — voir les logs serveur.")
        return empty
    except Exception as exc:
        logger.warning("Sync listings Etsy échouée pour user_id=%s : %s: %s", user_id, type(exc).__name__, exc)
        errors.append(f"Appel Etsy /listings/active impossible ({type(exc).__name__}).")
        return empty

    logger.info("Sync listings : %d fiche(s) reçue(s) d'Etsy pour shop_id=%s (oauth=%s).", len(etsy_listings), shop_id, bool(access_token))
    if not etsy_listings:
        errors.append(f"Etsy ne renvoie aucune fiche ACTIVE pour shop_id={shop_id} (fiches en brouillon/inactives, ou mauvais shop_id).")
        return empty

    # Deux appels Etsy supplémentaires par fiche (image + inventaire) : aucun
    # des deux n'est inclus dans /listings/active (confirmé contre le schéma
    # ShopListing officiel). Lancés en parallèle (bornés par _ETSY_CONCURRENCY)
    # — en séquentiel, 100 fiches = 200 appels = ~1 min de sync. Chacun est
    # indépendant et n'empêche jamais l'import de la fiche elle-même s'il
    # échoue (voir fetch_listing_image_url / fetch_listing_variants — aucun
    # des deux ne lève).
    semaphore = asyncio.Semaphore(_ETSY_CONCURRENCY)

    async def _enrich(listing_id: str) -> tuple:
        async with semaphore:
            return await asyncio.gather(
                fetch_listing_image_url(listing_id),
                fetch_listing_variants(listing_id, access_token),
            )

    listing_ids = [str(item.get("listing_id") or "") for item in etsy_listings]
    enriched = await asyncio.gather(*(_enrich(lid) for lid in listing_ids if lid))
    enriched_by_id = dict(zip([lid for lid in listing_ids if lid], enriched))

    # Les fiches importées doivent rester compatibles avec le modèle Listing
    # (voir models.py) — sans ça, la lecture ultérieure via GET /api/listings/
    # plante en ResponseValidationError (500 générique) au lieu de renvoyer
    # les données. D'où les tailles/valeurs par défaut ci-dessous.
    rows = []
    with_image = with_variants = 0
    for item in etsy_listings:
        try:
            listing_id = str(item.get("listing_id") or "")
            if not listing_id:
                continue

            # Etsy renvoie titres/descriptions HTML-échappés (&#39;, &amp;…) ;
            # le frontend affiche en textContent (jamais innerHTML), donc on
            # décode ici sinon les entités apparaissent telles quelles.
            title = html.unescape(item.get("title") or "Fiche Etsy sans titre").strip()[:140] or "Fiche Etsy"
            if len(title) < 3:
                title = title.ljust(3, ".")

            description = html.unescape(item.get("description") or "").strip()[:2000]
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

            image_url, variants = enriched_by_id.get(listing_id, (None, []))
            if image_url:
                with_image += 1
            if variants:
                with_variants += 1
            variant_prices = [v["price"] for v in variants if v.get("price")]
            if variant_prices:
                price_min, price_max = min(variant_prices), max(variant_prices)
            else:
                price_min = price_max = price

            # cost_price / target_margin (saisis à la main dans la fiche —
            # voir routers/listings.py > update_listing_pricing) ne sont
            # volontairement PAS dans ce dict : PostgREST ne touche que les
            # colonnes envoyées lors d'un ON CONFLICT DO UPDATE, donc une
            # resync n'écrase jamais ce que l'utilisateur a renseigné.
            row = {
                "user_id": user_id,
                "etsy_listing_id": listing_id,
                "name": title,
                "description": description,
                "tags": tags,
                "price_min": round(price_min, 2),
                "price_max": round(price_max, 2),
                "supplier": "my_catalog",
                "image_url": image_url,
                "stock_status": stock_status,
                "margin_pct": 0,
            }
            # Sans token OAuth, /inventory n'a pas été interrogé : on OMET la
            # colonne pour que l'ON CONFLICT DO UPDATE de PostgREST conserve
            # les variantes déjà en base (une resync « clé d'app seule » ne
            # doit jamais effacer ce qu'une sync OAuth précédente a importé).
            # À l'INSERT, la colonne prend son DEFAULT '[]'::jsonb.
            if access_token:
                row["variants"] = variants
            rows.append(row)
        except (TypeError, ValueError):
            continue

    if not rows:
        errors.append("Aucune fiche exploitable dans la réponse Etsy (listing_id manquant ou données invalides).")
        return {**empty, "received": len(etsy_listings)}
    if access_token and with_variants == 0:
        errors.append("Aucune variante récupérée malgré un token OAuth : scope listings_r manquant ou rate limit sur /inventory.")

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
        errors.append(f"Écriture en base échouée ({type(exc).__name__}) — index unique (user_id, etsy_listing_id) présent ? Voir logs serveur.")
        return {**empty, "received": len(etsy_listings)}

    logger.info(
        "Sync listings terminée pour user_id=%s : %d reçue(s), %d upsertée(s), %d avec image, %d avec variantes.",
        user_id, len(etsy_listings), len(rows), with_image, with_variants,
    )
    return {
        "synced": len(rows),
        "received": len(etsy_listings),
        "with_image": with_image,
        "with_variants": with_variants,
        "oauth_used": bool(access_token),
        "errors": errors,
    }


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
def _money(value: Any) -> float:
    """Etsy v3 renvoie tous les montants en {amount, divisor, currency_code} — jamais un nombre nu."""
    if isinstance(value, dict):
        try:
            return float(value.get("amount", 0)) / float(value.get("divisor", 100) or 100)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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

    logger.info("Sync orders : %d receipt(s) payé(s) reçu(s) d'Etsy pour shop_id=%s.", len(receipts), shop_id)

    rows = []
    for receipt in receipts:
        try:
            receipt_id = str(receipt.get("receipt_id") or "")
            if not receipt_id:
                continue

            transactions = receipt.get("transactions") or []
            titles = [html.unescape(t.get("title")) for t in transactions if t.get("title")]
            product_name = ", ".join(titles)[:140] if titles else "Commande Etsy"

            # items : ce qui sert aux analytics (fiche la plus vendue, CA par
            # fiche) — listing_id/quantity/prix unitaire, rien de plus.
            items = [
                {
                    "listing_id": str(t.get("listing_id") or ""),
                    "title": html.unescape(t.get("title") or "")[:140],
                    "quantity": int(t.get("quantity") or 1),
                    "price": round(_money(t.get("price")), 2),
                    "variations": [
                        f"{v.get('formatted_name')}: {v.get('formatted_value')}"
                        for v in (t.get("variations") or [])
                        if v.get("formatted_name")
                    ],
                }
                for t in transactions
            ]

            grandtotal = receipt.get("grandtotal") or receipt.get("total_price") or {}
            amount = _money(grandtotal)
            currency = (grandtotal.get("currency_code") if isinstance(grandtotal, dict) else None) or "EUR"

            shipments = receipt.get("shipments") or []
            tracking_number = shipments[0].get("tracking_code") if shipments else None

            customer_name = (receipt.get("name") or "Acheteur Etsy").strip()[:120] or "Acheteur Etsy"
            created_timestamp = int(receipt.get("created_timestamp") or receipt.get("create_timestamp") or 0) or None

            rows.append(
                {
                    "user_id": user_id,
                    "etsy_order_id": receipt_id,
                    "customer_name": customer_name,
                    "product_name": product_name,
                    "supplier": "my_catalog",
                    "amount": round(amount, 2),
                    "total_price": round(amount, 2),
                    "currency": currency,
                    "status": _map_receipt_status(receipt),
                    "etsy_status": (receipt.get("status") or None),
                    "tracking_number": tracking_number,
                    "buyer_email": receipt.get("buyer_email") or None,
                    "items": items,
                    "created_timestamp": created_timestamp,
                    # created_at = date réelle de la commande côté Etsy, pas la
                    # date de sync : sinon les périodes (30j/90j) des analytics
                    # et le tri du tableau Commandes n'ont aucun sens.
                    "created_at": datetime.fromtimestamp(created_timestamp, tz=timezone.utc).isoformat()
                    if created_timestamp else datetime.now(timezone.utc).isoformat(),
                    "synced_at": datetime.now(timezone.utc).isoformat(),
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

    logger.info("Sync orders terminée pour user_id=%s : %d commande(s) upsertée(s).", user_id, len(rows))
    return len(rows)


# === APPEL GET GÉNÉRIQUE VERS L'API ETSY V3 ===
async def etsy_get(path: str, *, params: Optional[dict] = None, access_token: Optional[str] = None) -> Any:
    """
    `access_token` omis => appel "app-only" (données publiques : boutiques,
    fiches actives). Fourni => appel authentifié pour le compte du vendeur.
    Jusqu'à _ETSY_429_RETRIES nouvelles tentatives avec backoff (1s, 2s, 3s)
    sur un 429 (rate limit Etsy par seconde).
    """
    if not ETSY_API_KEY or not ETSY_API_SECRET:
        raise HTTPException(status_code=500, detail="Configuration Etsy manquante côté serveur.")

    headers = {"x-api-key": f"{ETSY_API_KEY}:{ETSY_API_SECRET}"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    url = f"{ETSY_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, headers=headers, params=params)
        for attempt in range(1, _ETSY_429_RETRIES + 1):
            if response.status_code != 429:
                break
            await asyncio.sleep(attempt)
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
        if response.status_code == 401 and access_token:
            # Token invalide/expiré malgré le refresh préventif : l'appelant doit
            # afficher un message actionnable, pas un « échec Etsy » générique.
            raise HTTPException(status_code=401, detail="Reconnexion Etsy requise (token OAuth expiré ou révoqué).")
        if response.status_code == 429:
            raise HTTPException(status_code=429, detail="Rate limit Etsy atteint — réessaie dans une minute.")
        raise HTTPException(status_code=502, detail="Échec de la requête vers l'API Etsy.")

    return response.json()


# === APPEL ÉCRITURE GÉNÉRIQUE (PATCH / PUT / POST) VERS L'API ETSY V3 ===
# Utilisé par routers/seo.py (updateListing : titre/tags/description) et
# routers/pricing.py (updateListing price / updateListingInventory).
#
# Deux encodages selon l'endpoint Etsy — confirmé contre la doc officielle :
#   - updateListing (PATCH /shops/{shop_id}/listings/{listing_id}) attend un
#     corps application/x-www-form-urlencoded (`data=`), tags séparés par des
#     virgules. Ce n'est PAS un PUT (la spec Phase 2 parle de PUT, mais Etsy
#     v3 rejette PUT sur cette route avec 405).
#   - updateListingInventory (PUT /listings/{listing_id}/inventory) attend un
#     corps JSON (`json=`).
# Toujours authentifié (scope listings_w — déjà demandé dans routers/auth.py).
async def etsy_request(
    method: str,
    path: str,
    *,
    access_token: str,
    json: Optional[dict] = None,
    data: Optional[dict] = None,
) -> Any:
    if not ETSY_API_KEY or not ETSY_API_SECRET:
        raise HTTPException(status_code=500, detail="Configuration Etsy manquante côté serveur.")

    headers = {
        "x-api-key": f"{ETSY_API_KEY}:{ETSY_API_SECRET}",
        "Authorization": f"Bearer {access_token}",
    }
    url = f"{ETSY_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.request(method.upper(), url, headers=headers, json=json, data=data)
        for attempt in range(1, _ETSY_429_RETRIES + 1):
            if response.status_code != 429:
                break
            await asyncio.sleep(attempt)
            response = await client.request(method.upper(), url, headers=headers, json=json, data=data)

    if response.status_code not in (200, 201):
        try:
            error_body = response.text[:500]
        except Exception:
            error_body = "<illisible>"
        # Loggé, jamais renvoyé tel quel au client (règle CLAUDE.md).
        logger.warning("Etsy API %s %s a répondu %s : %s", method.upper(), path, response.status_code, error_body)
        if response.status_code in (401, 403):
            raise HTTPException(
                status_code=403,
                detail="Etsy a refusé la modification (token expiré ou scope listings_w manquant — reconnecte ta boutique).",
            )
        raise HTTPException(status_code=502, detail="Échec de la mise à jour sur Etsy.")

    try:
        return response.json()
    except ValueError:
        return {}
