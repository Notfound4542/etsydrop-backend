# =====================================================================
# === ROUTERS/SOURCING.PY — Comparateur multi-fournisseurs ===
# =====================================================================
#
# Compare le prix d'un produit sur toutes les sources connectées
# (Mon catalogue, Eprolo, CJ Dropshipping, Printify, Printful, Zendrop,
# AliExpress). Les appels réels aux APIs fournisseurs seront branchés ici
# lors de l'étape "Fulfillment auto" du plan ; pour l'instant, lecture
# d'un cache Supabase alimenté par les jobs de synchronisation.

import asyncio
import hashlib
import logging
import os
import time
from typing import List

import httpx
from fastapi import APIRouter, Depends, Query

from database import get_current_user, get_supabase
from models import (
    CurrentUser,
    SourcingCompareResult,
    SourcingSearchRequest,
    SourcingSearchResponse,
    SourcingSupplierResult,
)

router = APIRouter()
logger = logging.getLogger("etsydrop.sourcing")

# === CONFIGURATION FOURNISSEURS (jamais loggée, jamais renvoyée au client) ===
EPROLO_TOKEN = os.getenv("EPROLO_TOKEN")
CJ_ACCESS_TOKEN = os.getenv("CJ_ACCESS_TOKEN")
ALIEXPRESS_APP_KEY = os.getenv("ALIEXPRESS_APP_KEY")
ALIEXPRESS_APP_SECRET = os.getenv("ALIEXPRESS_APP_SECRET")

EPROLO_SEARCH_URL = "https://open.eprolo.com/api/product/search"
CJ_SEARCH_URL = "https://developers.cjdropshipping.com/api2.0/v1/product/list"
ALIEXPRESS_SYNC_URL = "https://api-sg.aliexpress.com/sync"


# === COMPARATEUR DE PRIX ===
@router.get("/compare", response_model=List[SourcingCompareResult])
async def compare_sources(
    q: str = Query(..., min_length=2, max_length=100, description="Nom ou mot-clé du produit à comparer"),
    user: CurrentUser = Depends(get_current_user),
):
    supabase = get_supabase()
    # `%` est ici une valeur du motif ILIKE, transmise en paramètre par le
    # query builder PostgREST — ce n'est pas une concaténation de SQL.
    result = (
        supabase.table("sourcing_cache")
        .select("*")
        .ilike("product_name", f"%{q}%")
        .eq("user_id", user.id)
        .execute()
    )
    return result.data


# === RECHERCHE LIVE MULTI-FOURNISSEURS ===
async def _search_eprolo(query: str, country_code: str) -> List[SourcingSupplierResult]:
    if not EPROLO_TOKEN:
        logger.warning("EPROLO_TOKEN manquant — source Eprolo ignorée.")
        return []

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                EPROLO_SEARCH_URL,
                headers={"Authorization": f"Bearer {EPROLO_TOKEN}"},
                params={"keyword": query, "country": country_code},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Eprolo indisponible : %s", type(exc).__name__)
        return []

    items = payload.get("data", {}).get("list", []) if isinstance(payload, dict) else []
    results: List[SourcingSupplierResult] = []
    for item in items:
        try:
            product_id = str(item.get("productId") or item.get("id") or "")
            if not product_id:
                continue
            results.append(
                SourcingSupplierResult(
                    source="eprolo",
                    product_id=product_id,
                    title=item.get("productName") or item.get("title") or "",
                    cost_price=float(item.get("price") or 0),
                    shipping_cost=float(item.get("shippingFee") or 0),
                    shipping_days=int(item.get("shippingDays") or item.get("deliveryDays") or 10),
                    supplier_rating=float(item.get("rating") or 4.0),
                    stock=int(item.get("stock") or 0),
                    url=item.get("productUrl") or f"https://www.eprolo.com/product/{product_id}",
                )
            )
        except (TypeError, ValueError):
            continue
    return results


async def _search_cj(query: str, country_code: str) -> List[SourcingSupplierResult]:
    if not CJ_ACCESS_TOKEN:
        logger.warning("CJ_ACCESS_TOKEN manquant — source CJ Dropshipping ignorée.")
        return []

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                CJ_SEARCH_URL,
                headers={"CJ-Access-Token": CJ_ACCESS_TOKEN},
                params={"productName": query, "countryCode": country_code},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("CJ Dropshipping indisponible : %s", type(exc).__name__)
        return []

    items = payload.get("data", {}).get("list", []) if isinstance(payload, dict) else []
    results: List[SourcingSupplierResult] = []
    for item in items:
        try:
            product_id = str(item.get("pid") or item.get("productId") or "")
            if not product_id:
                continue
            results.append(
                SourcingSupplierResult(
                    source="cj",
                    product_id=product_id,
                    title=item.get("productNameEn") or item.get("productName") or "",
                    cost_price=float(item.get("sellPrice") or item.get("price") or 0),
                    shipping_cost=float(item.get("shippingFee") or 0),
                    shipping_days=int(item.get("shippingDays") or 12),
                    supplier_rating=float(item.get("rating") or 4.0),
                    stock=int(item.get("stock") or item.get("inventory") or 0),
                    url=item.get("productUrl") or f"https://cjdropshipping.com/product/{product_id}",
                )
            )
        except (TypeError, ValueError):
            continue
    return results


def _sign_aliexpress_params(params: dict) -> str:
    """Signature TOP (Taobao Open Platform) : secret + params triés + secret, MD5 uppercase."""
    ordered = "".join(f"{key}{value}" for key, value in sorted(params.items()))
    signable = f"{ALIEXPRESS_APP_SECRET}{ordered}{ALIEXPRESS_APP_SECRET}"
    return hashlib.md5(signable.encode("utf-8")).hexdigest().upper()


async def _search_aliexpress(query: str, country_code: str) -> List[SourcingSupplierResult]:
    if not ALIEXPRESS_APP_KEY or not ALIEXPRESS_APP_SECRET:
        logger.warning("Clés AliExpress manquantes — source AliExpress ignorée.")
        return []

    # NB : aliexpress.ds.product.get est documentée par produit (product_id).
    # Conformément à la spec, on l'appelle ici avec un mot-clé de recherche ;
    # à affiner avec aliexpress.ds.text.search une fois l'accès sandbox obtenu.
    params = {
        "method": "aliexpress.ds.product.get",
        "app_key": ALIEXPRESS_APP_KEY,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "md5",
        "format": "json",
        "v": "2.0",
        "keyWord": query,
        "shipToCountry": country_code,
    }
    params["sign"] = _sign_aliexpress_params(params)

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(ALIEXPRESS_SYNC_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("AliExpress indisponible : %s", type(exc).__name__)
        return []

    if isinstance(payload, dict) and "error_response" in payload:
        logger.warning("AliExpress a renvoyé une erreur API.")
        return []

    items = (
        payload.get("aliexpress_ds_product_get_response", {}).get("result", {}).get("products", [])
        if isinstance(payload, dict)
        else []
    )
    results: List[SourcingSupplierResult] = []
    for item in items:
        try:
            product_id = str(item.get("product_id") or "")
            if not product_id:
                continue
            results.append(
                SourcingSupplierResult(
                    source="aliexpress",
                    product_id=product_id,
                    title=item.get("subject") or item.get("title") or "",
                    cost_price=float(item.get("target_sale_price") or item.get("price") or 0),
                    shipping_cost=float(item.get("shipping_fee") or 0),
                    shipping_days=int(item.get("delivery_days") or 15),
                    supplier_rating=float(item.get("evaluate_rate") or 4.0),
                    stock=int(item.get("stock") or 0),
                    url=item.get("product_url") or f"https://www.aliexpress.com/item/{product_id}.html",
                )
            )
        except (TypeError, ValueError):
            continue
    return results


def _score_supplier(item: SourcingSupplierResult) -> float:
    cost = max(item.cost_price, 0.01)
    days = max(item.shipping_days, 1)
    return (item.supplier_rating * 0.4) + ((1 / cost) * 0.35) + ((1 / days) * 0.25)


@router.post("/search", response_model=SourcingSearchResponse)
async def search_multi_supplier(
    payload: SourcingSearchRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """
    Interroge Eprolo, CJ Dropshipping et AliExpress en parallèle. Une source
    en échec (timeout, erreur, clé manquante) est ignorée sans jamais
    bloquer les autres — voir chaque _search_* ci-dessus.
    """
    eprolo_results, cj_results, aliexpress_results = await asyncio.gather(
        _search_eprolo(payload.query, payload.country_code),
        _search_cj(payload.query, payload.country_code),
        _search_aliexpress(payload.query, payload.country_code),
    )

    suppliers = [*eprolo_results, *cj_results, *aliexpress_results]
    if not suppliers:
        return SourcingSearchResponse(suppliers=[], best_pick=-1)

    best_pick = max(range(len(suppliers)), key=lambda i: _score_supplier(suppliers[i]))
    return SourcingSearchResponse(suppliers=suppliers, best_pick=best_pick)
