# =====================================================================
# === ROUTERS/ANALYTICS.PY — Revenus, marges, historique ===
# =====================================================================
#
# Toutes les métriques de revenu sont agrégées depuis la table `orders`
# (commandes Etsy synchronisées — voir etsy_client.py > sync_etsy_orders),
# JAMAIS depuis des valeurs en dur ni depuis un appel Etsy en direct à
# chaque affichage du dashboard : la DB est la source de vérité, la sync
# (manuelle ou post-OAuth) la remplit.
#
# Ce qu'Etsy Open API v3 n'expose PAS à une appli tierce (et qu'on ne
# devine donc jamais) : visiteurs, taux de conversion, sources de trafic,
# note boutique. Ces champs restent None, le frontend affiche "–".

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_current_user, get_supabase
from etsy_client import etsy_get, get_etsy_access_token
from models import (
    AnalyticsSummary,
    CurrentUser,
    MarginAnalyticsResponse,
    MarginCountryResult,
    RevenueAnalytics,
    RevenuePoint,
    RevenueTopProduct,
)

router = APIRouter()

# === FRAIS ETSY (voir CLAUDE.md > Taxes & Change) ===
ETSY_TRANSACTION_FEE_PCT = 0.065
ETSY_LISTING_FEE_EUR = 0.20
CURRENCY_CONVERSION_FEE_PCT = 0.018
EURO_ZONE_COUNTRIES = {"FR", "DE", "ES", "IT", "PT", "NL", "BE", "IE", "AT", "FI", "LU", "GR"}

_PERIOD_DAYS = {"30d": 30, "90d": 90, "12m": 365}
_MONTHS_FR = ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin", "Juil", "Août", "Sept", "Oct", "Nov", "Déc"]


# === LECTURE DES COMMANDES D'UN UTILISATEUR SUR UNE PÉRIODE ===
def _fetch_orders(user_id: str, days: int) -> List[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = (
        get_supabase()
        .table("orders")
        .select("id,amount,total_price,status,items,created_at")
        .eq("user_id", user_id)
        .gte("created_at", since)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


def _order_amount(order: dict) -> float:
    value = order.get("total_price")
    if value is None:
        value = order.get("amount")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _etsy_fees(revenue_gross: float, orders_count: int) -> float:
    return (
        (revenue_gross * ETSY_TRANSACTION_FEE_PCT)
        + (orders_count * ETSY_LISTING_FEE_EUR)
        + (revenue_gross * CURRENCY_CONVERSION_FEE_PCT)
    )


# === HISTORIQUE MENSUEL (12 derniers mois, mois vides inclus) ===
def _monthly_history(orders: List[dict], months: int = 12) -> List[RevenuePoint]:
    now = datetime.now(timezone.utc)
    buckets: dict[str, dict] = {}
    keys: List[str] = []
    for offset in range(months - 1, -1, -1):
        year = now.year
        month = now.month - offset
        while month <= 0:
            month += 12
            year -= 1
        key = f"{year:04d}-{month:02d}"
        keys.append(key)
        buckets[key] = {"revenue": 0.0, "orders": 0, "label": _MONTHS_FR[month - 1]}

    for order in orders:
        raw = order.get("created_at")
        if not raw:
            continue
        try:
            created = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        key = f"{created.year:04d}-{created.month:02d}"
        if key in buckets:
            buckets[key]["revenue"] += _order_amount(order)
            buckets[key]["orders"] += 1

    return [
        RevenuePoint(
            period=buckets[k]["label"],
            revenue=round(buckets[k]["revenue"], 2),
            profit=round(buckets[k]["revenue"] - _etsy_fees(buckets[k]["revenue"], buckets[k]["orders"]), 2),
            orders=buckets[k]["orders"],
        )
        for k in keys
    ]


# === REVENUS (commandes synchronisées sur une période) ===
@router.get("/revenue", response_model=RevenueAnalytics)
async def get_revenue(
    period: str = Query("30d", pattern="^(30d|90d|12m)$"),
    user: CurrentUser = Depends(get_current_user),
):
    orders = _fetch_orders(user.id, _PERIOD_DAYS[period])

    revenue_gross = sum(_order_amount(o) for o in orders)
    orders_count = len(orders)
    avg_order_value = (revenue_gross / orders_count) if orders_count else 0.0
    revenue_net = revenue_gross - _etsy_fees(revenue_gross, orders_count)

    product_stats: dict[str, dict] = defaultdict(lambda: {"title": "", "revenue": 0.0, "units": 0})
    status_breakdown: dict[str, int] = defaultdict(int)
    for order in orders:
        status_breakdown[str(order.get("status") or "pending_supplier")] += 1
        for item in order.get("items") or []:
            listing_id = str(item.get("listing_id") or "")
            if not listing_id:
                continue
            try:
                quantity = int(item.get("quantity") or 1)
                price = float(item.get("price") or 0)
            except (TypeError, ValueError):
                continue
            stats = product_stats[listing_id]
            stats["title"] = (item.get("title") or stats["title"])[:200]
            stats["revenue"] += price * quantity
            stats["units"] += quantity

    top_products = sorted(
        (
            RevenueTopProduct(listing_id=lid, title=s["title"], revenue=round(s["revenue"], 2), units=s["units"])
            for lid, s in product_stats.items()
        ),
        key=lambda p: (p.units, p.revenue),
        reverse=True,
    )[:5]

    # L'historique est toujours calculé sur 12 mois quelle que soit la
    # période demandée : le graphique du dashboard en a besoin en entier.
    history = _monthly_history(_fetch_orders(user.id, 365) if period != "12m" else orders)

    return RevenueAnalytics(
        period=period,
        revenue_gross=round(revenue_gross, 2),
        revenue_net=round(revenue_net, 2),
        orders_count=orders_count,
        avg_order_value=round(avg_order_value, 2),
        top_products=top_products,
        status_breakdown=dict(status_breakdown),
        history=history,
    )


# === RÉSUMÉ ANALYTICS (Dashboard) ===
@router.get("/summary", response_model=AnalyticsSummary)
async def get_analytics_summary(user: CurrentUser = Depends(get_current_user)):
    """
    net_margin_pct = marge après frais Etsy uniquement, PAS un vrai bénéfice
    net : les fiches importées depuis Etsy n'ont pas de coût fournisseur
    connu sauf saisie manuelle (listings.cost_price). conversion_rate_pct,
    visitors et shop_rating restent None : non exposés par l'API Etsy v3.
    """
    revenue = await get_revenue(period="30d", user=user)
    net_margin_pct = (revenue.revenue_net / revenue.revenue_gross * 100) if revenue.revenue_gross else 0.0
    return AnalyticsSummary(
        revenue_total=revenue.revenue_gross,
        net_margin_pct=round(max(0.0, min(100.0, net_margin_pct)), 2),
        conversion_rate_pct=None,
        visitors=None,
        shop_rating=None,
        net_profit=revenue.revenue_net,
        orders_count=revenue.orders_count,
        avg_order_value=revenue.avg_order_value,
        top_product_title=revenue.top_products[0].title if revenue.top_products else None,
        history=revenue.history,
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

    access_token = await get_etsy_access_token(user.id)
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
