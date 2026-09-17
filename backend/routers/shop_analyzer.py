# =====================================================================
# === ROUTERS/SHOP_ANALYZER.PY — Analyse concurrents (Shop Analyzer) ===
# =====================================================================
#
# Analyse publique d'une boutique Etsy concurrente : listings actifs
# triés par ventes estimées. N'utilise que des données publiques Etsy
# (x-api-key), aucun token OAuth du vendeur n'est requis ici.

import logging

from fastapi import APIRouter, Depends, HTTPException

from database import get_current_user
from etsy_client import etsy_get
from models import CurrentUser, ShopAnalyzeRequest, ShopAnalyzeResponse, ShopListingEstimate

router = APIRouter()
logger = logging.getLogger("etsydrop.shop_analyzer")


# === ANALYSE D'UNE BOUTIQUE CONCURRENTE ===
@router.post("/analyze", response_model=ShopAnalyzeResponse)
async def analyze_shop(payload: ShopAnalyzeRequest, user: CurrentUser = Depends(get_current_user)):
    shop = await etsy_get(f"/shops/{payload.shop_name}")
    shop_id = shop.get("shop_id") if isinstance(shop, dict) else None
    if not shop_id:
        raise HTTPException(status_code=404, detail="Boutique Etsy introuvable.")

    listings_payload = await etsy_get(f"/shops/{shop_id}/listings/active", params={"limit": 100})
    listings = listings_payload.get("results", []) if isinstance(listings_payload, dict) else []

    estimates: list[ShopListingEstimate] = []
    for listing in listings:
        try:
            num_favorers = int(listing.get("num_favorers", 0))
            views = int(listing.get("views", 0))
            # Proxy réaliste basé sur le ratio favoris/ventes Etsy connu (voir spec).
            estimated_sales = round(num_favorers * 0.15 + views * 0.008)

            price_data = listing.get("price")
            if isinstance(price_data, dict) and "amount" in price_data:
                price = float(price_data["amount"]) / float(price_data.get("divisor", 100) or 100)
            else:
                price = float(listing.get("price", 0))

            listing_id = str(listing.get("listing_id") or "")
            if not listing_id:
                continue

            estimates.append(
                ShopListingEstimate(
                    listing_id=listing_id,
                    title=listing.get("title", ""),
                    price=round(price, 2),
                    estimated_sales=estimated_sales,
                    num_favorers=num_favorers,
                    tags=listing.get("tags", []) or [],
                    url=listing.get("url") or f"https://www.etsy.com/listing/{listing_id}",
                )
            )
        except (TypeError, ValueError):
            continue

    top20 = sorted(estimates, key=lambda e: e.estimated_sales, reverse=True)[:20]

    return ShopAnalyzeResponse(shop_name=payload.shop_name, listings=top20)
