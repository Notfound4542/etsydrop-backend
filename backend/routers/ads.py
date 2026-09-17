# =====================================================================
# === ROUTERS/ADS.PY — Synchronisation & dashboard publicité ===
# =====================================================================

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict

import httpx
from fastapi import APIRouter, Depends, HTTPException

from database import get_current_user, get_supabase
from etsy_client import etsy_get, etsy_shop_id_from_token, get_etsy_access_token
from models import (
    AdsDashboardResponse,
    AdsDashboardRow,
    AdsSyncRequest,
    AdsSyncResponse,
    CurrentUser,
)

router = APIRouter()
logger = logging.getLogger("etsydrop.ads")

META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID")
META_GRAPH_URL = "https://graph.facebook.com/v18.0"


# === SYNCHRONISATION ETSY ADS (payment ledger) ===
async def _sync_etsy_ads(user_id: str, date_from: str, date_to: str) -> AdsSyncResponse:
    access_token = get_etsy_access_token(user_id)
    etsy_user_id = etsy_shop_id_from_token(access_token)

    shop = await etsy_get(f"/users/{etsy_user_id}/shops", access_token=access_token)
    shop_id = shop.get("shop_id") if isinstance(shop, dict) else None
    if not shop_id:
        raise HTTPException(status_code=404, detail="Boutique Etsy introuvable.")

    ledger = await etsy_get(f"/shops/{shop_id}/payment-ledger", access_token=access_token)
    entries = ledger.get("results", []) if isinstance(ledger, dict) else []

    spend_by_listing: Dict[str, float] = defaultdict(float)
    for entry in entries:
        entry_type = str(entry.get("entry_type") or entry.get("type") or "").upper()
        if "PROMOTION" not in entry_type:
            continue

        listing_id = str(entry.get("listing_id") or "unknown")
        amount_data = entry.get("amount")
        if isinstance(amount_data, dict):
            amount = abs(float(amount_data.get("amount", 0)) / float(amount_data.get("divisor", 100) or 100))
        else:
            amount = abs(float(amount_data or 0))
        spend_by_listing[listing_id] += amount

    total_spend = sum(spend_by_listing.values())

    supabase = get_supabase()
    for listing_id, spend in spend_by_listing.items():
        try:
            supabase.table("promotions").update({"spend_eur": round(spend, 2)}).eq("user_id", user_id).eq(
                "listing_id", listing_id
            ).eq("platform", "etsy_ads").execute()
        except Exception as exc:
            logger.warning("Échec mise à jour promotions (etsy_ads, listing=%s) : %s", listing_id, type(exc).__name__)

    return AdsSyncResponse(
        platform="etsy",
        date_from=date_from,
        date_to=date_to,
        spend_eur=round(total_spend, 2),
        spend_by_listing={lid: round(amount, 2) for lid, amount in spend_by_listing.items()},
    )


# === SYNCHRONISATION META ADS (Graph API insights) ===
async def _sync_meta_ads(user_id: str, date_from: str, date_to: str) -> AdsSyncResponse:
    if not META_ACCESS_TOKEN or not META_AD_ACCOUNT_ID:
        raise HTTPException(status_code=500, detail="Configuration Meta Ads manquante côté serveur.")

    params = {
        "fields": "spend,impressions,clicks,purchase_roas",
        "time_range": json.dumps({"since": date_from, "until": date_to}),
        "access_token": META_ACCESS_TOKEN,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{META_GRAPH_URL}/act_{META_AD_ACCOUNT_ID}/insights", params=params)
    except httpx.HTTPError as exc:
        logger.warning("Meta Ads indisponible : %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Échec de la synchronisation Meta Ads.")

    if response.status_code != 200:
        logger.warning("Meta Ads a répondu %s", response.status_code)
        raise HTTPException(status_code=502, detail="Échec de la synchronisation Meta Ads.")

    rows = response.json().get("data", [])
    row = rows[0] if rows else {}

    spend_eur = float(row.get("spend", 0))
    impressions = int(row.get("impressions", 0))
    clicks = int(row.get("clicks", 0))
    roas_entries = row.get("purchase_roas") or []
    roas = float(roas_entries[0].get("value", 0)) if roas_entries else 0.0

    supabase = get_supabase()
    try:
        supabase.table("promotions").update(
            {"spend_eur": round(spend_eur, 2), "impressions": impressions, "clicks": clicks}
        ).eq("user_id", user_id).eq("platform", "meta").execute()
    except Exception as exc:
        logger.warning("Échec mise à jour promotions (meta) : %s", type(exc).__name__)

    return AdsSyncResponse(
        platform="meta",
        date_from=date_from,
        date_to=date_to,
        spend_eur=round(spend_eur, 2),
        impressions=impressions,
        clicks=clicks,
        roas=round(roas, 2),
    )


@router.post("/sync", response_model=AdsSyncResponse)
async def sync_ads(payload: AdsSyncRequest, user: CurrentUser = Depends(get_current_user)):
    if payload.platform == "etsy":
        return await _sync_etsy_ads(user.id, payload.date_from, payload.date_to)
    return await _sync_meta_ads(user.id, payload.date_from, payload.date_to)


# === DASHBOARD AGRÉGÉ PAR PLATEFORME (30 derniers jours) ===
@router.get("/dashboard", response_model=AdsDashboardResponse)
async def ads_dashboard(user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    # PostgREST ne fait pas de GROUP BY côté requête : on agrège ici côté
    # backend l'équivalent de
    #   SELECT platform, SUM(spend_eur), SUM(revenue_eur), AVG(roas), SUM(clicks)
    #   FROM promotions WHERE user_id=... AND published_at >= NOW() - INTERVAL '30 days'
    #   GROUP BY platform ORDER BY roas DESC
    result = (
        supabase.table("promotions")
        .select("platform, spend_eur, revenue_eur, roas, clicks")
        .eq("user_id", user.id)
        .gte("published_at", since)
        .execute()
    )
    rows = result.data or []

    aggregated: Dict[str, dict] = defaultdict(lambda: {"spend": 0.0, "revenue": 0.0, "roas_sum": 0.0, "roas_count": 0, "clicks": 0})
    for row in rows:
        agg = aggregated[row.get("platform", "unknown")]
        agg["spend"] += float(row.get("spend_eur") or 0)
        agg["revenue"] += float(row.get("revenue_eur") or 0)
        agg["roas_sum"] += float(row.get("roas") or 0)
        agg["roas_count"] += 1
        agg["clicks"] += int(row.get("clicks") or 0)

    dashboard_rows = [
        AdsDashboardRow(
            platform=platform,
            total_spend_eur=round(agg["spend"], 2),
            total_revenue_eur=round(agg["revenue"], 2),
            avg_roas=round(agg["roas_sum"] / agg["roas_count"], 2) if agg["roas_count"] else 0.0,
            total_clicks=agg["clicks"],
        )
        for platform, agg in aggregated.items()
    ]
    dashboard_rows.sort(key=lambda r: r.avg_roas, reverse=True)

    return AdsDashboardResponse(rows=dashboard_rows)
