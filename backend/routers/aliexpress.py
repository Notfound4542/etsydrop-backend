# =====================================================================
# === ROUTERS/ALIEXPRESS.PY — Sourcing AliExpress (recherche, détail, import) ===
# =====================================================================
#
# Deux modes, choisis automatiquement :
#   1. affiliate_api — clés ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET définies :
#      API Affiliate officielle (aliexpress.affiliate.product.query /
#      aliexpress.affiliate.productdetail.get) via la passerelle TOP
#      https://api-sg.aliexpress.com/sync, signature MD5 (même schéma que
#      routers/sourcing.py > _sign_aliexpress_params).
#   2. scrape — sans clés : lecture des pages publiques de recherche
#      https://www.aliexpress.com/w/wholesale-{mots}.html. La page embarque un
#      JSON complet (window._dida_config_._init_data_ → data.root.fields.mods.
#      itemList.content[]) : on le parse en JSON plutôt qu'en HTML (plus
#      robuste qu'un sélecteur CSS — pas de BeautifulSoup nécessaire). Vérifié
#      en direct le 2026-09-18 : 60 résultats/page, prix en USD via le cookie
#      aep_usuc_f. La PAGE PRODUIT, elle, est rendue côté client (isCSR) et ses
#      données passent par une API mtop signée : le détail en mode scrape se
#      limite donc aux infos déjà collectées par la recherche (title, prix,
#      image, note, ventes) — variantes et délais par pays exigent la clé API.
#
# Cache : table aliexpress_cache (query TEXT, results JSONB, cached_at) —
# 1 h, clé "search:{q}:{page}:{limit}" ou "detail:{product_id}". Évite de
# marteler AliExpress (anti-bot) et sert de source au détail/import en mode
# scrape.
#
# Sécurité : clés lues côté serveur uniquement ; toutes les entrées passent
# par Pydantic (models.Aliexpress*) ; les écritures DB sont scopées user_id ;
# aucun corps de réponse tierce n'est renvoyé brut.

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple
from urllib.parse import quote_plus

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_current_user, get_supabase
from models import (
    AliexpressImportRequest,
    AliexpressImportResponse,
    AliexpressProduct,
    AliexpressProductDetail,
    AliexpressQuantityPrice,
    AliexpressSearchResponse,
    AliexpressShippingOption,
    AliexpressVariant,
    CurrentUser,
)

router = APIRouter()
logger = logging.getLogger("etsydrop.aliexpress")


def _clean_env(name: str) -> Optional[str]:
    return os.getenv(name, "").strip().strip("\"'") or None


ALIEXPRESS_APP_KEY = _clean_env("ALIEXPRESS_APP_KEY")
ALIEXPRESS_APP_SECRET = _clean_env("ALIEXPRESS_APP_SECRET")
# Identifiant de tracking Affiliate (optionnel) — sans lui l'API répond quand même.
ALIEXPRESS_TRACKING_ID = _clean_env("ALIEXPRESS_TRACKING_ID") or "etsydrop"
ALIEXPRESS_SYNC_URL = "https://api-sg.aliexpress.com/sync"

CACHE_TTL = timedelta(hours=1)
# Délai indicatif Chine → France en livraison standard (AliExpress Standard
# Shipping) quand le détail exact n'est pas disponible.
DEFAULT_SHIPPING_DAYS_FR = (12, 25)

_SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
# Force devise USD + interface anglaise quel que soit le pays du serveur
# (Railway) — sans ce cookie AliExpress localise selon l'IP (EUR, français).
_SCRAPE_COOKIES = {"aep_usuc_f": "site=glo&c_tp=USD&region=FR&b_locale=en_US", "intl_locale": "en_US"}


def api_available() -> bool:
    return bool(ALIEXPRESS_APP_KEY and ALIEXPRESS_APP_SECRET)


# =====================================================================
# === CACHE (table aliexpress_cache) ===
# =====================================================================
# Cache mémoire en première ligne (process courant) : sert de repli si la
# table aliexpress_cache n'est pas encore migrée ou si Supabase est indisponible,
# et évite un aller-retour DB pour les clics détail/import qui suivent une
# recherche. Non partagé entre workers — la table Supabase reste la référence.
_MEM_CACHE: dict = {}
_MEM_CACHE_MAX = 2000


def _mem_get(key: str) -> Optional[Any]:
    entry = _MEM_CACHE.get(key)
    if not entry:
        return None
    cached_at, value = entry
    if datetime.now(timezone.utc) - cached_at > CACHE_TTL:
        _MEM_CACHE.pop(key, None)
        return None
    return value


def _mem_set(key: str, value: Any) -> None:
    if len(_MEM_CACHE) >= _MEM_CACHE_MAX:
        # Éviction simple : on retire les entrées les plus anciennes.
        for old_key in sorted(_MEM_CACHE, key=lambda k: _MEM_CACHE[k][0])[: _MEM_CACHE_MAX // 4]:
            _MEM_CACHE.pop(old_key, None)
    _MEM_CACHE[key] = (datetime.now(timezone.utc), value)


def _cache_get(key: str) -> Optional[Any]:
    hit = _mem_get(key)
    if hit is not None:
        return hit
    try:
        result = (
            get_supabase()
            .table("aliexpress_cache")
            .select("results,cached_at")
            .eq("query", key)
            .order("cached_at", desc=True)
            .limit(1)
            .execute()
        )
        row = result.data[0] if result.data else None
        if not row:
            return None
        cached_at = datetime.fromisoformat(str(row["cached_at"]).replace("Z", "+00:00"))
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - cached_at > CACHE_TTL:
            return None
        _mem_set(key, row["results"])
        return row["results"]
    except Exception as exc:  # noqa: BLE001 — le cache n'est jamais bloquant
        logger.warning("Lecture aliexpress_cache échouée (%s) : %s — migration 2026-09-18_aliexpress.sql exécutée ?", key, type(exc).__name__)
        return None


def _cache_set(key: str, results: Any) -> None:
    _mem_set(key, results)
    try:
        supabase = get_supabase()
        supabase.table("aliexpress_cache").delete().eq("query", key).execute()
        supabase.table("aliexpress_cache").insert(
            {"query": key, "results": results, "cached_at": datetime.now(timezone.utc).isoformat()}
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Écriture aliexpress_cache échouée (%s) : %s", key, type(exc).__name__)


# =====================================================================
# === MODE 1 — API AFFILIATE OFFICIELLE ===
# =====================================================================
def _sign(params: dict) -> str:
    ordered = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    return hashlib.md5(f"{ALIEXPRESS_APP_SECRET}{ordered}{ALIEXPRESS_APP_SECRET}".encode("utf-8")).hexdigest().upper()


async def _affiliate_call(method: str, extra: dict) -> dict:
    params = {
        "method": method,
        "app_key": ALIEXPRESS_APP_KEY,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "md5",
        "format": "json",
        "v": "2.0",
        **{k: v for k, v in extra.items() if v not in (None, "")},
    }
    params["sign"] = _sign(params)
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(ALIEXPRESS_SYNC_URL, params=params)
    except httpx.HTTPError as exc:
        logger.warning("AliExpress Affiliate injoignable : %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="API AliExpress injoignable.")
    if response.status_code != 200:
        logger.warning("AliExpress Affiliate %s a répondu %s : %s", method, response.status_code, response.text[:300])
        raise HTTPException(status_code=502, detail="Échec de la requête vers l'API AliExpress.")
    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Réponse AliExpress illisible.")
    if isinstance(payload, dict) and "error_response" in payload:
        logger.warning("AliExpress Affiliate erreur : %s", json.dumps(payload.get("error_response"))[:300])
        raise HTTPException(status_code=502, detail="L'API AliExpress a renvoyé une erreur (clé/signature ou quota).")
    return payload


def _pct_to_rating(value: Any) -> Optional[float]:
    """evaluate_rate Affiliate = "95.5%" → note sur 5."""
    try:
        pct = float(str(value).replace("%", "").strip())
        return round(min(5.0, max(0.0, pct / 20.0)), 2)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return round(float(str(value).replace(",", "").replace("US $", "").replace("$", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    try:
        digits = re.sub(r"[^\d]", "", str(value))
        return int(digits) if digits else None
    except (TypeError, ValueError):
        return None


def _affiliate_product(item: dict) -> Optional[AliexpressProduct]:
    product_id = str(item.get("product_id") or "").strip()
    if not product_id.isdigit():
        return None
    return AliexpressProduct(
        product_id=product_id,
        title=(item.get("product_title") or "Produit AliExpress")[:300],
        price_usd=_to_float(item.get("target_sale_price") or item.get("sale_price")),
        original_price_usd=_to_float(item.get("target_original_price") or item.get("original_price")),
        image_url=(item.get("product_main_image_url") or None),
        url=(item.get("promotion_link") or item.get("product_detail_url") or f"https://www.aliexpress.com/item/{product_id}.html")[:600],
        shipping_to_fr=None,
        rating=_pct_to_rating(item.get("evaluate_rate")),
        orders_count=_to_int(item.get("lastest_volume")),
        store_name=(item.get("shop_name") or None),
    )


async def _affiliate_search(query: str, page: int, limit: int) -> Tuple[List[AliexpressProduct], Optional[int]]:
    payload = await _affiliate_call(
        "aliexpress.affiliate.product.query",
        {
            "keywords": query,
            "page_no": page,
            "page_size": limit,
            "target_currency": "USD",
            "target_language": "EN",
            "ship_to_country": "FR",
            "tracking_id": ALIEXPRESS_TRACKING_ID,
            "sort": "SALE_PRICE_ASC",
        },
    )
    result = (
        payload.get("aliexpress_affiliate_product_query_response", {})
        .get("resp_result", {})
        .get("result", {})
    )
    items = (result.get("products") or {}).get("product") or []
    products = [p for p in (_affiliate_product(i) for i in items) if p]
    total = _to_int(result.get("total_record_count"))
    return products, total


async def _affiliate_detail(product_id: str) -> Optional[AliexpressProduct]:
    payload = await _affiliate_call(
        "aliexpress.affiliate.productdetail.get",
        {
            "product_ids": product_id,
            "target_currency": "USD",
            "target_language": "EN",
            "ship_to_country": "FR",
            "tracking_id": ALIEXPRESS_TRACKING_ID,
        },
    )
    result = (
        payload.get("aliexpress_affiliate_productdetail_get_response", {})
        .get("resp_result", {})
        .get("result", {})
    )
    items = (result.get("products") or {}).get("product") or []
    return _affiliate_product(items[0]) if items else None


# =====================================================================
# === MODE 2 — REPLI : PAGES PUBLIQUES DE RECHERCHE (JSON embarqué) ===
# =====================================================================
def _parse_search_html(html_text: str) -> Tuple[List[dict], Optional[int]]:
    """
    Extrait data.root.fields.mods.itemList.content[] et pageInfo.totalResults
    depuis window._dida_config_._init_data_ (JSON valide après le préfixe
    `{ data: `). raw_decode s'arrête au premier objet complet, ce qui évite
    tout parsing fragile par regex sur 800 Ko de page.
    """
    start = html_text.find("window._dida_config_._init_data_")
    if start < 0:
        return [], None
    brace = html_text.find('{"hierarchy"', start)
    if brace < 0:
        brace = html_text.find("{", html_text.find("data:", start))
    if brace < 0:
        return [], None
    try:
        data, _ = json.JSONDecoder().raw_decode(html_text[brace:])
    except ValueError:
        return [], None
    fields = ((data.get("data") or {}).get("root") or {}).get("fields") or {}
    items = ((fields.get("mods") or {}).get("itemList") or {}).get("content") or []
    total = _to_int((fields.get("pageInfo") or {}).get("totalResults"))
    return items, total


def _scraped_product(item: dict) -> Optional[AliexpressProduct]:
    product_id = str(item.get("productId") or item.get("redirectedId") or "").strip()
    if not product_id.isdigit():
        return None
    prices = item.get("prices") or {}
    sale = prices.get("salePrice") or {}
    original = prices.get("originalPrice") or {}
    image = (item.get("image") or {}).get("imgUrl") or None
    if image and image.startswith("//"):
        image = "https:" + image
    trade = (item.get("trade") or {}).get("tradeDesc")  # "457 sold" / "1,000+ sold"
    orders = _to_int(trade) if trade else None
    rating = (item.get("evaluation") or {}).get("starRating")
    store = (item.get("store") or {}).get("storeName")
    # Texte de livraison si présent (varie selon la carte : "Free shipping"…)
    shipping_text = None
    for selling in item.get("sellingPoints") or []:
        text = ((selling.get("tagContent") or {}).get("tagText") or "")
        if "ship" in text.lower() or "livraison" in text.lower():
            shipping_text = text[:120]
            break
    return AliexpressProduct(
        product_id=product_id,
        title=((item.get("title") or {}).get("displayTitle") or "Produit AliExpress")[:300],
        price_usd=_to_float(sale.get("minPrice")) if sale.get("currencyCode", "USD") == "USD" else None,
        original_price_usd=_to_float(original.get("minPrice")) if original else None,
        image_url=image,
        url=f"https://www.aliexpress.com/item/{product_id}.html",
        shipping_to_fr=shipping_text,
        rating=round(float(rating), 2) if isinstance(rating, (int, float)) else None,
        orders_count=orders,
        store_name=store[:160] if isinstance(store, str) else None,
    )


async def _scrape_search(query: str, page: int, limit: int) -> Tuple[List[AliexpressProduct], Optional[int], List[str]]:
    warnings: List[str] = []
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-") or "product"
    url = f"https://www.aliexpress.com/w/wholesale-{slug}.html?SearchText={quote_plus(query)}&page={page}"
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            response = await client.get(url, headers=_SCRAPE_HEADERS, cookies=_SCRAPE_COOKIES)
    except httpx.HTTPError as exc:
        logger.warning("Scraping AliExpress injoignable : %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="AliExpress injoignable pour le moment.")
    if response.status_code != 200:
        logger.warning("Scraping AliExpress a répondu %s pour %r", response.status_code, query)
        raise HTTPException(status_code=502, detail="AliExpress a refusé la requête (anti-bot ou indisponibilité).")

    text = response.text
    if "punish" in str(response.url) or "captcha" in text[:5000].lower():
        raise HTTPException(status_code=503, detail="AliExpress demande une vérification anti-bot — réessaie plus tard ou configure la clé API.")

    items, total = _parse_search_html(text)
    if not items:
        warnings.append("Structure de page AliExpress non reconnue — aucun résultat extrait (le format a peut-être changé).")
    products = [p for p in (_scraped_product(i) for i in items) if p]
    if products and all(p.price_usd is None for p in products):
        warnings.append("Prix renvoyés dans une autre devise que l'USD — affichés vides.")
    warnings.append("Mode repli (pages publiques) : sans clé API AliExpress, variantes et délais par pays ne sont pas disponibles.")
    return products[:limit], total, warnings


# =====================================================================
# === ENDPOINTS ===
# =====================================================================
@router.get("/search", response_model=AliexpressSearchResponse)
async def search_products(
    q: str = Query(..., min_length=2, max_length=120),
    page: int = Query(1, ge=1, le=50),
    limit: int = Query(20, ge=1, le=50),
    user: CurrentUser = Depends(get_current_user),
):
    query = re.sub(r"\s+", " ", q).strip()
    source = "affiliate_api" if api_available() else "scrape"
    cache_key = f"search:{source}:{query.lower()}:{page}:{limit}"

    cached = _cache_get(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("results"), list):
        return AliexpressSearchResponse(
            query=query, page=page, limit=limit, total=cached.get("total"), source=source, cached=True,
            results=[AliexpressProduct(**r) for r in cached["results"]],
            warnings=cached.get("warnings") or [],
        )

    warnings: List[str] = []
    if source == "affiliate_api":
        products, total = await _affiliate_search(query, page, limit)
    else:
        products, total, warnings = await _scrape_search(query, page, limit)

    _cache_set(cache_key, {"results": [p.model_dump() for p in products], "total": total, "warnings": warnings})
    # Index par produit pour /product/{id} et /import en mode scrape.
    for p in products:
        _cache_set(f"detail:{p.product_id}", p.model_dump())

    return AliexpressSearchResponse(
        query=query, page=page, limit=limit, total=total, source=source, cached=False, results=products, warnings=warnings
    )


async def _resolve_base_product(product_id: str) -> Tuple[Optional[AliexpressProduct], str, List[str]]:
    """Produit de base (title/prix/image…) depuis le cache, sinon l'API Affiliate."""
    warnings: List[str] = []
    cached = _cache_get(f"detail:{product_id}")
    if isinstance(cached, dict) and cached.get("product_id"):
        try:
            return AliexpressProduct(**cached), ("affiliate_api" if api_available() else "scrape"), warnings
        except Exception:  # noqa: BLE001 — cache d'une version antérieure
            pass
    if api_available():
        product = await _affiliate_detail(product_id)
        if product:
            _cache_set(f"detail:{product_id}", product.model_dump())
        return product, "affiliate_api", warnings
    warnings.append("Produit absent du cache de recherche : relance une recherche contenant ce produit (page produit AliExpress non lisible sans clé API).")
    return None, "scrape", warnings


@router.get("/product/{product_id}", response_model=AliexpressProductDetail)
async def product_detail(product_id: str, user: CurrentUser = Depends(get_current_user)):
    if not re.fullmatch(r"[0-9]{3,40}", product_id):
        raise HTTPException(status_code=422, detail="product_id AliExpress invalide.")

    base, source, warnings = await _resolve_base_product(product_id)
    if not base:
        raise HTTPException(status_code=404, detail="Produit AliExpress introuvable — relance une recherche qui le contient.")

    variants: List[AliexpressVariant] = []
    quantity_prices: List[AliexpressQuantityPrice] = []
    if base.price_usd is not None:
        # Palier unique connu ; les remises quantité réelles exigent l'API DS.
        quantity_prices.append(AliexpressQuantityPrice(min_quantity=1, price_usd=base.price_usd))
    shipping = [
        AliexpressShippingOption(
            country_code="FR", method="AliExpress Standard Shipping (estimation)",
            cost_usd=None, delivery_days_min=DEFAULT_SHIPPING_DAYS_FR[0], delivery_days_max=DEFAULT_SHIPPING_DAYS_FR[1],
        ),
        AliexpressShippingOption(country_code="DE", method="AliExpress Standard Shipping (estimation)", delivery_days_min=12, delivery_days_max=25),
        AliexpressShippingOption(country_code="US", method="AliExpress Standard Shipping (estimation)", delivery_days_min=10, delivery_days_max=20),
        AliexpressShippingOption(country_code="GB", method="AliExpress Standard Shipping (estimation)", delivery_days_min=10, delivery_days_max=22),
    ]
    if source == "affiliate_api":
        warnings.append("Variantes et grilles quantité non exposées par l'API Affiliate : délais indicatifs (Standard Shipping).")
    else:
        warnings.append("Délais indicatifs (Standard Shipping Chine → destination) : la page produit AliExpress n'est pas lisible sans clé API.")

    return AliexpressProductDetail(
        **base.model_dump(),
        description=None,
        images=[base.image_url] if base.image_url else [],
        variants=variants,
        quantity_prices=quantity_prices,
        shipping=shipping,
        source=source,
        warnings=warnings,
    )


def _get_or_create_aliexpress_supplier(user_id: str) -> str:
    supabase = get_supabase()
    result = (
        supabase.table("suppliers")
        .select("id")
        .eq("user_id", user_id)
        .eq("platform", "aliexpress")
        .order("created_at")
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["id"]
    created = (
        supabase.table("suppliers")
        .insert({
            "user_id": user_id,
            "name": "AliExpress",
            "platform": "aliexpress",
            "notes": "Créé automatiquement par le sourcing AliExpress (Sourcing > AliExpress).",
        })
        .execute()
    )
    if not created.data:
        raise HTTPException(status_code=500, detail="Création du fournisseur AliExpress impossible.")
    return created.data[0]["id"]


@router.post("/import", response_model=AliexpressImportResponse, status_code=201)
async def import_product(payload: AliexpressImportRequest, user: CurrentUser = Depends(get_current_user)):
    """
    Enregistre un produit AliExpress dans supplier_products (source='aliexpress')
    sous le fournisseur « AliExpress » du compte (créé au premier import), et le
    lie éventuellement à une fiche Etsy (listing_id = UUID interne → etsy_listing_id).
    Idempotent : un second import du même product_id met la ligne à jour.
    """
    supabase = get_supabase()

    linked_etsy_listing_id: Optional[int] = None
    if payload.listing_id:
        listing = (
            supabase.table("listings")
            .select("etsy_listing_id")
            .eq("id", payload.listing_id)
            .eq("user_id", user.id)
            .maybe_single()
            .execute()
        )
        if not listing or not listing.data:
            raise HTTPException(status_code=404, detail="Fiche Etsy introuvable.")
        try:
            linked_etsy_listing_id = int(listing.data.get("etsy_listing_id") or 0) or None
        except (TypeError, ValueError):
            linked_etsy_listing_id = None

    detail = await product_detail(payload.product_id, user)  # 404 si inconnu
    supplier_id = _get_or_create_aliexpress_supplier(user.id)

    existing = (
        supabase.table("supplier_products")
        .select("id,linked_etsy_listing_id")
        .eq("user_id", user.id)
        .eq("supplier_id", supplier_id)
        .eq("supplier_product_id", payload.product_id)
        .limit(1)
        .execute()
    )
    existing_row = existing.data[0] if existing.data else None

    shipping_fr = next((s for s in detail.shipping if s.country_code == "FR"), None)
    record = {
        "user_id": user.id,
        "supplier_id": supplier_id,
        "supplier_product_id": payload.product_id,
        "name": detail.title[:200],
        "description": (detail.description or f"Imported from AliExpress — {detail.url}")[:3000],
        "base_price": detail.price_usd if detail.price_usd is not None else 0,
        "currency": "USD",
        "images": detail.images[:20],
        "variants": [
            {"color": v.label[:60], "size": None, "stock": v.stock, "price": v.price_usd} for v in detail.variants[:200]
        ],
        "moq": 1,
        "lead_time_days": shipping_fr.delivery_days_max if shipping_fr else DEFAULT_SHIPPING_DAYS_FR[1],
        "source": "aliexpress",
        "external_id": payload.product_id,
        "shipping_days_estimate": shipping_fr.delivery_days_max if shipping_fr else DEFAULT_SHIPPING_DAYS_FR[1],
        "rating": detail.rating,
        "orders_count": detail.orders_count,
        # Ne jamais écraser une liaison existante par NULL si l'import ne précise rien.
        "linked_etsy_listing_id": linked_etsy_listing_id or (existing_row or {}).get("linked_etsy_listing_id"),
    }

    try:
        if existing_row:
            saved = (
                supabase.table("supplier_products")
                .update(record)
                .eq("id", existing_row["id"])
                .eq("user_id", user.id)
                .execute()
            )
        else:
            saved = supabase.table("supplier_products").insert(record).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("Import AliExpress échoué (user_id=%s, product_id=%s) : %s: %s", user.id, payload.product_id, type(exc).__name__, exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Enregistrement impossible — la migration 2026-09-18_aliexpress.sql (colonnes source/external_id/rating…) a-t-elle été exécutée ?",
        )
    if not saved.data:
        raise HTTPException(status_code=500, detail="Enregistrement du produit impossible.")

    row = saved.data[0]
    return AliexpressImportResponse(
        supplier_id=supplier_id,
        supplier_product_id=row["id"],
        product_id=payload.product_id,
        linked_etsy_listing_id=row.get("linked_etsy_listing_id"),
        updated=bool(existing_row),
        product=row,
    )
