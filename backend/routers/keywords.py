# =====================================================================
# === ROUTERS/KEYWORDS.PY — SEO & mots-clés ===
# =====================================================================
#
# Recherche de mots-clés (volume, ventes totales Etsy, score) utilisée
# par le module SEO et le Rank Tracker du frontend.

from collections import Counter
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_current_user, get_supabase
from etsy_client import etsy_get
from models import CurrentUser, KeywordMetric, KeywordVolumeEstimate

router = APIRouter()


def _competition_level(count: int) -> str:
    if count < 50:
        return "low"
    if count < 200:
        return "medium"
    return "high"


# === RECHERCHE DE MOTS-CLÉS ===
@router.get("/search", response_model=List[KeywordMetric])
async def search_keywords(
    q: str = Query(..., min_length=2, max_length=80),
    user: CurrentUser = Depends(get_current_user),
):
    supabase = get_supabase()
    result = (
        supabase.table("keywords")
        .select("*")
        .ilike("keyword", f"%{q}%")
        .order("score", desc=True)
        .limit(20)
        .execute()
    )
    return result.data


# === RECHERCHE LIVE PAR MARCHÉ (proxy volume = fréquence des tags Etsy) ===
# Placé avant /{keyword} pour ne pas être masqué par cette route générique.
@router.get("/market-search", response_model=List[KeywordVolumeEstimate])
async def market_search(
    q: str = Query(..., min_length=2, max_length=80),
    market: str = Query("US", pattern="^(US|UK|AU|CA)$"),
    user: CurrentUser = Depends(get_current_user),
):
    # NB : l'API publique Etsy Listings n'offre pas de filtre géographique ;
    # `market` est conservé pour l'évolution future (ex. domaine etsy.co.uk).
    payload = await etsy_get("/listings/active", params={"keywords": q, "limit": 100})
    listings = payload.get("results", []) if isinstance(payload, dict) else []

    tag_counts: Counter = Counter()
    for listing in listings:
        tag_counts.update(tag.lower() for tag in (listing.get("tags") or []) if tag)

    return [
        KeywordVolumeEstimate(keyword=tag, etsy_count=count, competition=_competition_level(count))
        for tag, count in tag_counts.most_common(20)
    ]


# === STATISTIQUES D'UN MOT-CLÉ PRÉCIS ===
@router.get("/{keyword}", response_model=KeywordMetric)
async def get_keyword(keyword: str, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("keywords")
        .select("*")
        .eq("keyword", keyword)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Mot-clé introuvable.")
    return result.data
