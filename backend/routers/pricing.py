# =====================================================================
# === ROUTERS/PRICING.PY — Pricing international ===
# =====================================================================
#
# Pour chaque pays cible, calcule le prix de vente permettant d'atteindre
# la marge nette souhaitée une fois les frais Etsy déduits, puis arrondit
# au prix psychologique (19.99, 24.99, ...). Voir CLAUDE.md > Taxes & Change.
#
# Phase 2 (voir plus bas) : /profiles (profils de livraison), /analyze
# (analyse complète par pays : TVA, change, port, pire cas — barème partagé
# dans etsy_fees.py) et /apply/{listing_id} (pousser un prix sur Etsy).
# /calculate (ci-dessous) est conservé tel quel pour le simulateur historique.

import logging
import math
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from database import get_current_user, get_supabase
from etsy_client import etsy_get, etsy_request, get_etsy_access_token, get_etsy_shop_id
from etsy_fees import (
    COUNTRIES,
    DEFAULT_COUNTRY_CODES,
    compute_sale_breakdown,
    country_info,
    normalize_country,
    psychological_round,
    recommended_price,
)
from models import (
    CurrentUser,
    PricingAnalyzeRequest,
    PricingAnalyzeResponse,
    PricingApplyRequest,
    PricingApplyResponse,
    PricingCalculateRequest,
    PricingCalculateResponse,
    PricingCountryAnalysis,
    PricingCountryResult,
    PricingSummary,
    PricingWorstCase,
    ShippingProfile,
    ShippingProfileCreate,
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


# =====================================================================
# === PHASE 2 — PROFILS DE LIVRAISON ===
# =====================================================================
@router.get("/profiles/", response_model=List[ShippingProfile])
async def list_shipping_profiles(user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("shipping_profiles")
        .select("*")
        .eq("user_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


@router.post("/profiles/", response_model=ShippingProfile, status_code=201)
async def create_shipping_profile(payload: ShippingProfileCreate, user: CurrentUser = Depends(get_current_user)):
    unknown = [c.code for c in payload.countries if not country_info(c.code)]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Pays non supportés : {', '.join(unknown)}")
    record = payload.model_dump(mode="json")
    for c in record["countries"]:
        c["code"] = normalize_country(c["code"])
        c["name"] = c.get("name") or COUNTRIES[c["code"]]["name"]
    record["user_id"] = user.id
    supabase = get_supabase()
    result = supabase.table("shipping_profiles").insert(record).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Création du profil impossible.")
    return result.data[0]


@router.get("/profiles/{profile_id}", response_model=ShippingProfile)
async def get_shipping_profile(profile_id: str, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("shipping_profiles")
        .select("*")
        .eq("id", profile_id)
        .eq("user_id", user.id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise HTTPException(status_code=404, detail="Profil de livraison introuvable.")
    return result.data


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_shipping_profile(profile_id: str, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    supabase.table("shipping_profiles").delete().eq("id", profile_id).eq("user_id", user.id).execute()
    return None


# =====================================================================
# === PHASE 2 — ANALYSE PRIX / MARGE PAR PAYS ===
# =====================================================================
def _margin_status(margin_pct: Optional[float], target_pct: float) -> str:
    if margin_pct is None or margin_pct < target_pct - 10:
        return "bad"
    if margin_pct < target_pct:
        return "close"
    return "good"


def _resolve_countries(payload: PricingAnalyzeRequest, user_id: str) -> tuple:
    """
    Retourne (liste [{code, shipping_cost, days_min, days_max}], free_shipping,
    target_margin). Priorité : saisie manuelle > profil de livraison > défauts.
    """
    free_shipping = payload.free_shipping
    target_margin = payload.target_margin_pct

    if payload.countries:
        rows = []
        for c in payload.countries:
            code = normalize_country(c.code)
            if not country_info(code):
                raise HTTPException(status_code=422, detail=f"Pays non supporté : {c.code}")
            rows.append({"code": code, "shipping_cost": c.shipping_cost, "days_min": c.delivery_days_min, "days_max": c.delivery_days_max})
        return rows, bool(free_shipping), target_margin

    if payload.shipping_profile_id:
        supabase = get_supabase()
        result = (
            supabase.table("shipping_profiles")
            .select("*")
            .eq("id", payload.shipping_profile_id)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if not result or not result.data:
            raise HTTPException(status_code=404, detail="Profil de livraison introuvable.")
        profile = result.data
        if free_shipping is None:
            free_shipping = bool(profile.get("is_free_shipping"))
        if target_margin is None and profile.get("target_margin_pct") is not None:
            target_margin = float(profile["target_margin_pct"])
        rows = []
        for c in profile.get("countries") or []:
            code = normalize_country(c.get("code"))
            if not country_info(code):
                continue
            rows.append(
                {
                    "code": code,
                    "shipping_cost": float(c.get("shipping_cost") or 0),
                    "days_min": c.get("delivery_days_min"),
                    "days_max": c.get("delivery_days_max"),
                }
            )
        if rows:
            return rows, bool(free_shipping), target_margin

    rows = [{"code": code, "shipping_cost": COUNTRIES[code]["shipping_default"], "days_min": None, "days_max": None} for code in DEFAULT_COUNTRY_CODES]
    return rows, bool(free_shipping), target_margin


@router.post("/analyze", response_model=PricingAnalyzeResponse)
async def analyze_pricing(payload: PricingAnalyzeRequest, user: CurrentUser = Depends(get_current_user)):
    """
    Pour chaque pays : prix recommandé (marge cible tenue après commission
    Etsy 6,5 % + 0,20 € + 0,18 €, change 1,8 %, TVA locale, livraison), puis
    décomposition complète à ce prix. "Pire cas" = pays à la marge la plus
    faible. Voir etsy_fees.py pour le barème et la formule.
    """
    listing = None
    if payload.listing_id:
        supabase = get_supabase()
        result = (
            supabase.table("listings")
            .select("id,etsy_listing_id,price_min,target_margin,cost_price")
            .eq("id", payload.listing_id)
            .eq("user_id", user.id)
            .maybe_single()
            .execute()
        )
        if not result or not result.data:
            raise HTTPException(status_code=404, detail="Fiche introuvable.")
        listing = result.data

    countries, free_shipping, target_margin = _resolve_countries(payload, user.id)
    if target_margin is None:
        target_margin = float(listing["target_margin"]) if (listing and listing.get("target_margin") is not None) else 35.0
    current_price = payload.current_price
    if current_price is None and listing and listing.get("price_min"):
        current_price = float(listing["price_min"])

    results: List[PricingCountryAnalysis] = []
    for c in countries:
        code = c["code"]
        info = COUNTRIES[code]
        raw_price = recommended_price(
            cost_price=payload.cost_price,
            target_margin_pct=target_margin,
            country_code=code,
            shipping_cost=c["shipping_cost"],
            free_shipping=free_shipping,
            vat_applicable=payload.vat_applicable,
        )
        base = {
            "country_code": code,
            "country_name": info["name"],
            "flag": info["flag"],
            "currency": info["currency"],
            "shipping_cost_eur": round(c["shipping_cost"], 2),
            "shipping_charged_eur": 0.0 if free_shipping else round(c["shipping_cost"], 2),
            "vat_rate_pct": round(info["vat_rate"] * 100, 1),
            "vat_label": info["vat_label"],
            "vat_collected_by_etsy": info["vat_collected_by_etsy"],
            "delivery_days_min": c.get("days_min") or info["days"][0],
            "delivery_days_max": c.get("days_max") or info["days"][1],
        }
        if raw_price is None:
            results.append(PricingCountryAnalysis(**base, reachable=False, status="bad"))
            continue

        price = psychological_round(raw_price)
        breakdown = compute_sale_breakdown(
            sale_price=price,
            shipping_charged=0.0 if free_shipping else c["shipping_cost"],
            shipping_cost=c["shipping_cost"],
            cost_price=payload.cost_price,
            country_code=code,
            vat_applicable=payload.vat_applicable,
        )
        current_margin_eur = current_margin_pct = None
        if current_price:
            current = compute_sale_breakdown(
                sale_price=current_price,
                shipping_charged=0.0 if free_shipping else c["shipping_cost"],
                shipping_cost=c["shipping_cost"],
                cost_price=payload.cost_price,
                country_code=code,
                vat_applicable=payload.vat_applicable,
            )
            current_margin_eur, current_margin_pct = current["margin_eur"], current["margin_pct"]

        results.append(
            PricingCountryAnalysis(
                **base,
                recommended_price_eur=price,
                recommended_price_local=round(price * info["rate"], 2),
                buyer_total_eur=breakdown["buyer_total_eur"],
                etsy_fees_eur=breakdown["etsy_fees_eur"],
                currency_fee_eur=breakdown["currency_fee_eur"],
                vat_eur=breakdown["vat_eur"],
                seller_net_eur=breakdown["seller_net_eur"],
                margin_eur=breakdown["margin_eur"],
                margin_pct=breakdown["margin_pct"],
                current_margin_eur=current_margin_eur,
                current_margin_pct=current_margin_pct,
                reachable=True,
                # Le statut juge la marge au prix ACTUEL si connu (c'est ce qui
                # se passe vraiment en boutique), sinon au prix recommandé.
                status=_margin_status(current_margin_pct if current_price else breakdown["margin_pct"], target_margin),
            )
        )

    worst = best = None
    reachable = [r for r in results if r.reachable]
    unreachable = [r for r in results if not r.reachable]
    margin_of = (lambda r: r.current_margin_pct) if current_price else (lambda r: r.margin_pct)
    if unreachable:
        r = unreachable[0]
        worst = PricingWorstCase(
            country_code=r.country_code, country_name=r.country_name, flag=r.flag, margin_pct=None,
            reason="Marge cible inatteignable : frais + TVA + marge dépassent 100 % du prix.",
        )
    elif reachable:
        r = min(reachable, key=lambda x: margin_of(x) if margin_of(x) is not None else 999)
        worst = PricingWorstCase(
            country_code=r.country_code, country_name=r.country_name, flag=r.flag, margin_pct=margin_of(r),
            reason=f"Frais Etsy {r.etsy_fees_eur:.2f} € + change {r.currency_fee_eur:.2f} € + TVA {r.vat_eur:.2f} € + port {r.shipping_cost_eur:.2f} €",
        )
    if reachable:
        r = max(reachable, key=lambda x: margin_of(x) if margin_of(x) is not None else -999)
        best = PricingWorstCase(country_code=r.country_code, country_name=r.country_name, flag=r.flag, margin_pct=margin_of(r), reason="Meilleure marge nette")

    suggested = max((r.recommended_price_eur for r in reachable), default=None)

    response = PricingAnalyzeResponse(
        listing_id=payload.listing_id,
        cost_price=payload.cost_price,
        target_margin_pct=target_margin,
        free_shipping=free_shipping,
        vat_applicable=payload.vat_applicable,
        results=results,
        worst_case=worst,
        best_case=best,
        suggested_single_price_eur=suggested,
    )

    # Historique (product_cost_analysis) — best-effort, ne bloque jamais la réponse.
    analysis_id = None
    try:
        etsy_listing_id = None
        if listing and listing.get("etsy_listing_id") and str(listing["etsy_listing_id"]).isdigit():
            etsy_listing_id = int(listing["etsy_listing_id"])
        saved = get_supabase().table("product_cost_analysis").insert(
            {
                "user_id": user.id,
                "etsy_listing_id": etsy_listing_id,
                "cost_price": payload.cost_price,
                "target_margin_pct": target_margin,
                "shipping_profile_id": payload.shipping_profile_id,
                "analysis": response.model_dump(mode="json", exclude={"analysis_id"}),
            }
        ).execute()
        if saved.data:
            analysis_id = saved.data[0].get("id")
    except Exception as exc:
        logger.warning("Historisation de l'analyse prix échouée : %s", type(exc).__name__)

    response.analysis_id = analysis_id
    return response


# =====================================================================
# === PHASE 2 — APPLIQUER UN PRIX SUR ETSY ===
# =====================================================================
@router.post("/apply/{listing_id}", response_model=PricingApplyResponse)
async def apply_price_on_etsy(listing_id: str, payload: PricingApplyRequest, user: CurrentUser = Depends(get_current_user)):
    """
    Etsy n'a qu'UN prix par fiche sans variantes (updateListing, champ
    `price`) ; avec variantes, le prix vit dans l'inventaire (une offering
    par combinaison) → updateListingInventory, toutes les offerings au
    nouveau prix. Dans les deux cas la DB locale (price_min/price_max) est
    alignée ensuite. Scope listings_w requis (demandé dès la connexion).
    """
    supabase = get_supabase()
    result = (
        supabase.table("listings")
        .select("id,etsy_listing_id,variants")
        .eq("id", listing_id)
        .eq("user_id", user.id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise HTTPException(status_code=404, detail="Fiche introuvable.")
    listing = result.data
    etsy_listing_id = listing.get("etsy_listing_id")
    if not etsy_listing_id:
        raise HTTPException(status_code=400, detail="Cette fiche n'est pas liée à Etsy (créée à la main).")

    access_token = await get_etsy_access_token(user.id)
    shop_id = get_etsy_shop_id(user.id)
    price = round(payload.price_eur, 2)

    inventory = await etsy_get(f"/listings/{etsy_listing_id}/inventory", access_token=access_token)
    products = inventory.get("products", []) if isinstance(inventory, dict) else []
    has_variations = any((p.get("property_values") or []) for p in products)

    if not has_variations:
        await etsy_request(
            "PATCH",
            f"/shops/{shop_id}/listings/{etsy_listing_id}",
            access_token=access_token,
            data={"price": f"{price:.2f}"},
        )
        method, updated = "listing", 0
    else:
        # updateListingInventory exige de renvoyer TOUS les produits ; on
        # conserve sku/propriétés/quantités et ne change que le prix.
        new_products = []
        updated = 0
        for p in products:
            if p.get("is_deleted"):
                continue
            offerings = []
            for o in p.get("offerings") or []:
                if o.get("is_deleted"):
                    continue
                offerings.append({"price": price, "quantity": int(o.get("quantity") or 0), "is_enabled": bool(o.get("is_enabled", True))})
                updated += 1
            property_values = [
                {
                    "property_id": pv.get("property_id"),
                    "value_ids": pv.get("value_ids") or [],
                    "scale_id": pv.get("scale_id"),
                    "property_name": pv.get("property_name"),
                    "values": pv.get("values") or [],
                }
                for pv in (p.get("property_values") or [])
            ]
            new_products.append({"sku": p.get("sku") or "", "property_values": property_values, "offerings": offerings})
        body = {
            "products": new_products,
            "price_on_property": inventory.get("price_on_property") or [],
            "quantity_on_property": inventory.get("quantity_on_property") or [],
            "sku_on_property": inventory.get("sku_on_property") or [],
        }
        await etsy_request("PUT", f"/listings/{etsy_listing_id}/inventory", access_token=access_token, json=body)
        method = "inventory"

    # Alignement local + variantes au nouveau prix.
    variants = listing.get("variants") or []
    for v in variants:
        if isinstance(v, dict) and "price" in v:
            v["price"] = price
    try:
        supabase.table("listings").update({"price_min": price, "price_max": price, "variants": variants}).eq("id", listing_id).execute()
    except Exception as exc:
        logger.warning("Alignement local du prix échoué : %s", type(exc).__name__)

    return PricingApplyResponse(listing_id=listing_id, etsy_listing_id=str(etsy_listing_id), price_eur=price, method=method, offerings_updated=updated)
