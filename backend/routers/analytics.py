# =====================================================================
# === ROUTERS/ANALYTICS.PY — Revenus, marges, historique ===
# =====================================================================

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_current_user, get_supabase
from etsy_client import etsy_get, get_etsy_access_token, get_etsy_shop_id
from models import (
    AnalyticsSummary,
    CurrentUser,
    MarginAnalyticsResponse,
    MarginCountryResult,
    RevenueAnalytics,
    RevenueTopProduct,
)

router = APIRouter()

# Valeurs par défaut renvoyées tant qu'aucune donnée n'a encore été calculée
# pour ce compte (nouvel utilisateur, boutique Etsy pas encore synchronisée).
_EMPTY_SUMMARY = {
    "revenue_total": 0,
    "net_margin_pct": 0,
    "conversion_rate_pct": 0,
    "net_profit": 0,
    "history": [],
}

# === FRAIS ETSY (voir CLAUDE.md > Taxes & Change) ===
ETSY_TRANSACTION_FEE_PCT = 0.065
ETSY_LISTING_FEE_EUR = 0.20
CURRENCY_CONVERSION_FEE_PCT = 0.018
EURO_ZONE_COUNTRIES = {"FR", "DE", "ES", "IT", "PT", "NL", "BE", "IE", "AT", "FI", "LU", "GR"}

_PERIOD_DAYS = {"30d": 30, "90d": 90, "12m": 365}


# === RÉSUMÉ ANALYTICS ===
@router.get("/summary", response_model=AnalyticsSummary)
async def get_analytics_summary(user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("analytics_summary")
        .select("*")
        .eq("user_id", user.id)
        .maybe_single()
        .execute()
    )
    return result.data or _EMPTY_SUMMARY


# === REVENUS (receipts Etsy sur une période) ===
@router.get("/revenue", response_model=RevenueAnalytics)
async def get_revenue(
    period: str = Query("30d", pattern="^(30d|90d|12m)$"),
    user: CurrentUser = Depends(get_current_user),
):
    access_token = get_etsy_access_token(user.id)
    # shop_id est résolu une fois à la connexion (voir routers/auth.py >
    # etsy_callback) et lu ici depuis la DB : GET /users/{user_id}/shops
    # renvoie 403 au tier Etsy actuel de cette app.
    shop_id = get_etsy_shop_id(user.id)

    since = datetime.now(timezone.utc) - timedelta(days=_PERIOD_DAYS[period])
    receipts_payload = await etsy_get(
        f"/shops/{shop_id}/receipts",
        access_token=access_token,
        params={"min_created": int(since.timestamp()), "limit": 100},
    )
    receipts = receipts_payload.get("results", []) if isinstance(receipts_payload, dict) else []

    revenue_gross = sum(float(r.get("total_price", 0)) for r in receipts)
    orders_count = len(receipts)
    avg_order_value = (revenue_gross / orders_count) if orders_count else 0.0

    etsy_fees = (
        (revenue_gross * ETSY_TRANSACTION_FEE_PCT)
        + (orders_count * ETSY_LISTING_FEE_EUR)
        + (revenue_gross * CURRENCY_CONVERSION_FEE_PCT)
    )
    revenue_net = revenue_gross - etsy_fees

    product_stats: dict[str, dict] = defaultdict(lambda: {"title": "", "revenue": 0.0, "units": 0})
    for receipt in receipts:
        for transaction in receipt.get("transactions", []):
            listing_id = str(transaction.get("listing_id") or "")
            if not listing_id:
                continue
            quantity = int(transaction.get("quantity", 1))
            stats = product_stats[listing_id]
            stats["title"] = transaction.get("title", "")
            stats["revenue"] += float(transaction.get("price", 0)) * quantity
            stats["units"] += quantity

    top_products = sorted(
        (
            RevenueTopProduct(listing_id=lid, title=stats["title"], revenue=stats["revenue"], units=stats["units"])
            for lid, stats in product_stats.items()
        ),
        key=lambda p: p.revenue,
        reverse=True,
    )[:5]

    return RevenueAnalytics(
        revenue_gross=round(revenue_gross, 2),
        revenue_net=round(revenue_net, 2),
        orders_count=orders_count,
        avg_order_value=round(avg_order_value, 2),
        top_products=top_products,
    )


# === MARGE NETTE PAR PAYS ===
@router.get("/margin", response_model=MarginAnalyticsResponse)
async def get_margin(
    listing_id: str = Query(..., min_length=1, max_length=40),
    supplier_cost: float = Query(..., ge=0),
    shipping_cost: float = Query(..., ge=0),
    countries: List[str] = Query(..., description="Codes pays ISO 2 lettres, ex : ?countries=FR&countries=US"),
    user: CurrentUser = Depends(get_current_user),
):
    if not countries:
        raise HTTPException(status_code=422, detail="Au moins un pays est requis.")

    access_token = get_etsy_access_token(user.id)
    listing = await etsy_get(f"/listings/{listing_id}", access_token=access_token)

    price_data = listing.get("price") if isinstance(listing, dict) else None
    if isinstance(price_data, dict) and "amount" in price_data:
        sale_price = float(price_data["amount"]) / float(price_data.get("divisor", 100) or 100)
    else:
        sale_price = float(listing.get("price", 0)) if isinstance(listing, dict) else 0.0

    results: List[MarginCountryResult] = []
    for raw_country in countries:
        country_code = raw_country.upper()
        transaction_fee = sale_price * ETSY_TRANSACTION_FEE_PCT
        conversion_fee = 0.0 if country_code in EURO_ZONE_COUNTRIES else sale_price * CURRENCY_CONVERSION_FEE_PCT
        etsy_fees = transaction_fee + ETSY_LISTING_FEE_EUR + conversion_fee
        net_margin_eur = sale_price - supplier_cost - shipping_cost - etsy_fees
        margin_pct = (net_margin_eur / sale_price * 100) if sale_price else 0.0

        results.append(
            MarginCountryResult(
                country_code=country_code,
                sale_price=round(sale_price, 2),
                etsy_fees=round(etsy_fees, 2),
                net_margin_eur=round(net_margin_eur, 2),
                margin_pct=round(margin_pct, 2),
            )
        )

    return MarginAnalyticsResponse(
        listing_id=listing_id,
        supplier_cost=supplier_cost,
        shipping_cost=shipping_cost,
        results=results,
    )
