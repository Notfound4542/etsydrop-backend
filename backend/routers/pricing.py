# =====================================================================
# === ROUTERS/PRICING.PY — Pricing international ===
# =====================================================================
#
# Pour chaque pays cible, calcule le prix de vente permettant d'atteindre
# la marge nette souhaitée une fois les frais Etsy déduits, puis arrondit
# au prix psychologique (19.99, 24.99, ...). Voir CLAUDE.md > Taxes & Change.

import logging
import math
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from database import get_current_user
from models import (
    CurrentUser,
    PricingCalculateRequest,
    PricingCalculateResponse,
    PricingCountryResult,
    PricingSummary,
)

router = APIRouter()
logger = logging.getLogger("etsydrop.pricing")

# === TAUX DE CHANGE (hardcodé, mis à jour manuellement) ===
# TODO: brancher sur une API de change en temps réel.
EXCHANGE_RATES = {
    "US": 1.08, "UK": 0.86, "AU": 1.65, "CA": 1.47,
    "DE": 1.0, "FR": 1.0, "IT": 1.0, "ES": 1.0, "NL": 1.0,
    "BE": 1.0, "CH": 0.96, "SE": 11.5, "NO": 11.4, "DK": 7.45, "PL": 4.28,
}

CURRENCIES = {
    "US": "USD", "UK": "GBP", "AU": "AUD", "CA": "CAD",
    "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR", "BE": "EUR",
    "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK", "PL": "PLN",
}

EURO_ZONE_COUNTRIES = {"DE", "FR", "IT", "ES", "NL", "BE"}

ETSY_TRANSACTION_FEE_PCT = 0.065
ETSY_PAYMENT_PROCESSING_PCT = 0.04
ETSY_LISTING_FEE_EUR = 0.20
CURRENCY_CONVERSION_FEE_PCT = 0.025

MIN_VIABLE_MARGIN_PCT = 25.0


def _psychological_round(price: float) -> float:
    base = math.ceil(price)
    return base - 0.01 if base - 0.01 >= price else base + 0.99


def _customs_note(country: str, sale_price_eur: float) -> Optional[str]:
    if country == "US" and sale_price_eur > 800:
        return "Customs declaration required >$800"
    if country == "UK" and sale_price_eur > 135:
        return "UK customs duties may apply >£135"
    if country == "AU" and sale_price_eur > 1000:
        return "AU GST import threshold >A$1000"
    return None


# === CALCUL DU PRIX PAR PAYS ===
@router.post("/calculate", response_model=PricingCalculateResponse)
async def calculate_pricing(
    payload: PricingCalculateRequest,
    user: CurrentUser = Depends(get_current_user),
):
    unknown = [c for c in payload.countries if c.upper() not in EXCHANGE_RATES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Pays non supportés : {', '.join(unknown)}")

    total_cost = payload.supplier_cost_eur + payload.shipping_cost_eur

    results: List[PricingCountryResult] = []
    for raw_country in payload.countries:
        country = raw_country.upper()
        conversion_fee_pct = 0.0 if country in EURO_ZONE_COUNTRIES else CURRENCY_CONVERSION_FEE_PCT
        etsy_fees_rate = ETSY_TRANSACTION_FEE_PCT + ETSY_PAYMENT_PROCESSING_PCT + conversion_fee_pct

        denominator = 1 - (payload.desired_margin_pct / 100) - etsy_fees_rate
        if denominator <= 0:
            # Marge cible inatteignable pour ce pays (frais + marge >= 100%) : on l'exclut
            # plutôt que de renvoyer un prix de vente négatif ou une division par zéro.
            logger.warning("Marge cible inatteignable pour %s.", country)
            continue

        sale_price_eur = (total_cost + ETSY_LISTING_FEE_EUR) / denominator
        sale_price_local = _psychological_round(sale_price_eur * EXCHANGE_RATES[country])

        etsy_fees_eur = (sale_price_eur * etsy_fees_rate) + ETSY_LISTING_FEE_EUR
        net_margin_eur = sale_price_eur - total_cost - etsy_fees_eur
        net_margin_pct = (net_margin_eur / sale_price_eur * 100) if sale_price_eur else 0.0

        results.append(
            PricingCountryResult(
                country=country,
                sale_price_eur=round(sale_price_eur, 2),
                sale_price_local=round(sale_price_local, 2),
                currency=CURRENCIES[country],
                net_margin_eur=round(net_margin_eur, 2),
                net_margin_pct=round(net_margin_pct, 2),
                etsy_fees_eur=round(etsy_fees_eur, 2),
                viable=net_margin_pct >= MIN_VIABLE_MARGIN_PCT,
                customs_note=_customs_note(country, sale_price_eur),
            )
        )

    recommended_countries = [
        r.country for r in sorted((r for r in results if r.viable), key=lambda r: r.net_margin_pct, reverse=True)
    ]

    if results:
        best = max(results, key=lambda r: r.net_margin_pct)
        summary = PricingSummary(
            best_country=best.country,
            best_margin_pct=best.net_margin_pct,
            avg_sale_price_eur=round(sum(r.sale_price_eur for r in results) / len(results), 2),
        )
    else:
        summary = PricingSummary()

    return PricingCalculateResponse(
        results=results,
        recommended_countries=recommended_countries,
        summary=summary,
    )
