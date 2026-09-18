# =====================================================================
# === ROUTERS/PRODUCT_TEST.PY — Simulation d'achat (frais cachés) ===
# =====================================================================
#
# Simule un achat complet d'une fiche vers un pays donné pour révéler TOUS
# les frais : ce que paie l'acheteur (prix + port réel du profil
# d'expédition Etsy), ce qu'Etsy prélève, ce que tu reçois, ta marge.
#
# IMPORTANT : l'API Etsy v3 ne permet PAS de créer une commande de test ni
# de simuler un paiement. Ce module ne passe JAMAIS commande : il lit la
# fiche et son profil d'expédition réels, puis applique le barème partagé
# (etsy_fees.py). Les résultats sont historisés dans product_test_results.
#
# Endpoints Etsy utilisés :
#   - GET /listings/{listing_id}                         (public, app-only)
#   - GET /listings/{listing_id}/inventory               (OAuth, listings_r)
#   - GET /shops/{shop_id}/shipping-profiles/{profile}   (OAuth, shops_r)
# Il n'existe pas de route /listings/{id}/shipping en v3 : le port vient du
# shipping_profile_id de la fiche → destinations du profil (primary_cost par
# pays ou région, min/max_delivery_days).

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from database import get_current_user, get_supabase
from etsy_client import etsy_get, get_etsy_access_token, get_etsy_shop_id
from etsy_fees import (
    ETSY_CURRENCY_CONVERSION_PCT,
    ETSY_FIXED_TRANSACTION_FEE_EUR,
    ETSY_LISTING_FEE_EUR,
    ETSY_TRANSACTION_FEE_PCT,
    compute_sale_breakdown,
    country_info,
    normalize_country,
)
from models import CurrentUser, ProductTestLine, ProductTestRequest, ProductTestResult

router = APIRouter()
logger = logging.getLogger("etsydrop.product_test")

# Régions Etsy (destination_region) → pays couverts, pour retrouver le port
# d'un profil qui n'a pas de ligne par pays.
_EU = {"FR", "DE", "IT", "ES", "NL", "BE"}
_REGION_MEMBERS = {"eu": _EU, "european union": _EU, "europe": _EU | {"GB", "CH"}, "non_eu": {"GB", "CH", "US", "CA", "AU"}}


def _money(value) -> Optional[float]:
    if isinstance(value, dict) and "amount" in value:
        try:
            return float(value["amount"]) / float(value.get("divisor", 100) or 100)
        except (TypeError, ValueError):
            return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _status(margin_pct: Optional[float], target: float) -> str:
    if margin_pct is None:
        return "unknown"
    if margin_pct < target - 10:
        return "bad"
    if margin_pct < target:
        return "close"
    return "good"


async def _resolve_shipping(shop_id: int, access_token: str, profile_id, country: str) -> tuple:
    """
    Retourne (port_eur, days_min, days_max, source). source = 'etsy_profile'
    si trouvé dans le profil d'expédition Etsy, sinon 'default' (estimation).
    Le profil peut être en devise étrangère : converti via le taux du pays.
    """
    info = country_info(country)
    fallback = (info["shipping_default"], info["days"][0], info["days"][1], "default")
    if not profile_id:
        return fallback
    try:
        profile = await etsy_get(f"/shops/{shop_id}/shipping-profiles/{profile_id}", access_token=access_token)
    except Exception as exc:
        logger.warning("Profil d'expédition Etsy %s illisible : %s", profile_id, type(exc).__name__)
        return fallback

    destinations = profile.get("shipping_profile_destinations", []) if isinstance(profile, dict) else []
    best = None
    for dest in destinations:
        iso = normalize_country(dest.get("destination_country_iso"))
        region = str(dest.get("destination_region") or "").lower()
        if iso == country:
            best = dest
            break
        if not iso and country in _REGION_MEMBERS.get(region, set()):
            best = best or dest
        if not iso and region in ("", "none", "everywhere", "world") and best is None:
            best = dest  # "everywhere else"
    if not best:
        return fallback

    cost = _money(best.get("primary_cost"))
    if cost is None:
        return fallback
    currency = (best.get("primary_cost") or {}).get("currency_code") if isinstance(best.get("primary_cost"), dict) else None
    if currency and currency != "EUR":
        # Conversion approximative via le taux du pays de la devise.
        for c in ("US", "GB", "CA", "AU", "CH"):
            ci = country_info(c)
            if ci and ci["currency"] == currency and ci["rate"]:
                cost = cost / ci["rate"]
                break
    days_min = best.get("min_delivery_days") or profile.get("min_delivery_days") or info["days"][0]
    days_max = best.get("max_delivery_days") or profile.get("max_delivery_days") or info["days"][1]
    return round(cost, 2), int(days_min), int(days_max), "etsy_profile"


@router.post("/simulate", response_model=ProductTestResult)
async def simulate_purchase(payload: ProductTestRequest, user: CurrentUser = Depends(get_current_user)):
    country = normalize_country(payload.destination_country)
    info = country_info(country)
    if not info:
        raise HTTPException(status_code=422, detail=f"Pays non supporté : {payload.destination_country}")

    supabase = get_supabase()
    result = (
        supabase.table("listings")
        .select("id,etsy_listing_id,name,price_min,variants,cost_price,target_margin")
        .eq("id", payload.listing_id)
        .eq("user_id", user.id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise HTTPException(status_code=404, detail="Fiche introuvable.")
    listing = result.data
    warnings: List[str] = []

    # --- 1. Fiche Etsy en direct (prix courant, profil d'expédition) ---
    unit_price = float(listing.get("price_min") or 0)
    profile_id = None
    etsy_listing_id = listing.get("etsy_listing_id")
    access_token = None
    shop_id = None
    if etsy_listing_id:
        try:
            live = await etsy_get(f"/listings/{etsy_listing_id}")
            live_price = _money(live.get("price")) if isinstance(live, dict) else None
            if live_price:
                unit_price = live_price
            profile_id = live.get("shipping_profile_id") if isinstance(live, dict) else None
            if isinstance(live, dict) and live.get("state") and live["state"] != "active":
                warnings.append(f"La fiche n'est pas active sur Etsy (état : {live['state']}).")
        except Exception as exc:
            logger.warning("Lecture live de la fiche Etsy %s échouée : %s", etsy_listing_id, type(exc).__name__)
            warnings.append("Fiche Etsy injoignable en direct — prix issu de la dernière synchronisation.")
        try:
            access_token = await get_etsy_access_token(user.id)
            shop_id = get_etsy_shop_id(user.id)
        except HTTPException:
            warnings.append("Boutique Etsy non connectée — port estimé (pas le profil d'expédition réel).")
    else:
        warnings.append("Fiche non liée à Etsy : prix et port estimés depuis les données locales.")

    # --- Variante sélectionnée (prix propre à la combinaison) ---
    variant_label = None
    if payload.variant_selected:
        for v in listing.get("variants") or []:
            label = v.get("label") if isinstance(v, dict) else None
            if label and label.lower() == payload.variant_selected.lower():
                variant_label = label
                if v.get("price"):
                    unit_price = float(v["price"])
                if v.get("quantity") is not None and int(v["quantity"]) < payload.quantity:
                    warnings.append(f"Stock insuffisant pour cette variante ({v['quantity']} disponible(s)).")
                break
        if variant_label is None:
            warnings.append("Variante introuvable — prix de base utilisé.")

    if unit_price <= 0:
        raise HTTPException(status_code=422, detail="Prix de la fiche inconnu — resynchronise ton catalogue.")

    # --- 2. Port réel (profil d'expédition Etsy) ---
    if access_token and shop_id and profile_id:
        shipping, days_min, days_max, shipping_source = await _resolve_shipping(shop_id, access_token, profile_id, country)
    else:
        shipping, days_min, days_max, shipping_source = info["shipping_default"], info["days"][0], info["days"][1], "default"
    if shipping_source == "default":
        warnings.append("Port estimé (profil d'expédition Etsy non trouvé pour ce pays).")

    # --- 3-5. Calculs ---
    qty = payload.quantity
    cost_price = float(listing["cost_price"]) if listing.get("cost_price") is not None else None
    target = float(listing.get("target_margin") or 35.0)
    breakdown = compute_sale_breakdown(
        sale_price=unit_price * qty,
        shipping_charged=shipping,
        shipping_cost=shipping,
        cost_price=(cost_price * qty) if cost_price is not None else None,
        country_code=country,
    )

    lines: List[ProductTestLine] = [
        ProductTestLine(label=f"Prix produit × {qty}", amount_eur=round(unit_price * qty, 2), kind="buyer"),
        ProductTestLine(label="Frais de port (payés par l'acheteur)", amount_eur=round(shipping, 2), kind="buyer",
                        note="Profil d'expédition Etsy" if shipping_source == "etsy_profile" else "Estimation"),
        ProductTestLine(label="Commission Etsy 6,5 % (prix + port)", amount_eur=-breakdown["etsy_commission_eur"], kind="fee"),
        ProductTestLine(label="Frais de transaction fixes", amount_eur=-round(ETSY_FIXED_TRANSACTION_FEE_EUR, 2), kind="fee"),
        ProductTestLine(label="Frais de mise en vente (renouvellement)", amount_eur=-round(ETSY_LISTING_FEE_EUR, 2), kind="fee"),
    ]
    if breakdown["currency_fee_eur"]:
        lines.append(ProductTestLine(label=f"Frais de change Etsy {ETSY_CURRENCY_CONVERSION_PCT * 100:.1f} % ({info['currency']})", amount_eur=-breakdown["currency_fee_eur"], kind="fee"))
    if breakdown["vat_eur"]:
        lines.append(ProductTestLine(label=f"TVA à reverser ({breakdown['vat_label']})", amount_eur=-breakdown["vat_eur"], kind="fee"))
    elif info["vat_collected_by_etsy"]:
        lines.append(ProductTestLine(label=f"Taxe locale — {info['vat_label']}", amount_eur=0.0, kind="fee", note="Ajoutée au prix acheteur et reversée par Etsy, pas déduite de ton net"))
    lines.append(ProductTestLine(label="Port réellement payé (fournisseur / transporteur)", amount_eur=-round(shipping, 2), kind="cost"))
    if cost_price is not None:
        lines.append(ProductTestLine(label=f"Coût fournisseur × {qty}", amount_eur=-round(cost_price * qty, 2), kind="cost"))
    else:
        warnings.append("Coût fournisseur non renseigné dans la fiche — marge impossible à calculer.")
    lines.append(ProductTestLine(label="Net vendeur après frais Etsy", amount_eur=breakdown["seller_net_eur"], kind="net"))
    if breakdown["margin_eur"] is not None:
        lines.append(ProductTestLine(label="Marge finale", amount_eur=breakdown["margin_eur"], kind="net"))

    report = ProductTestResult(
        listing_id=payload.listing_id,
        etsy_listing_id=str(etsy_listing_id) if etsy_listing_id else None,
        destination_country=country,
        country_name=info["name"],
        flag=info["flag"],
        currency=info["currency"],
        variant_selected=variant_label,
        quantity=qty,
        unit_price_eur=round(unit_price, 2),
        shipping_eur=round(shipping, 2),
        shipping_source=shipping_source,
        delivery_days_min=days_min,
        delivery_days_max=days_max,
        buyer_total_eur=breakdown["buyer_total_eur"],
        buyer_total_local=breakdown["buyer_total_local"],
        seller_net_eur=breakdown["seller_net_eur"],
        cost_price_eur=round(cost_price * qty, 2) if cost_price is not None else None,
        margin_eur=breakdown["margin_eur"],
        margin_pct=breakdown["margin_pct"],
        status=_status(breakdown["margin_pct"], target),
        lines=lines,
        warnings=warnings,
    )

    # --- Historique (best-effort) ---
    try:
        saved = supabase.table("product_test_results").insert(
            {
                "user_id": user.id,
                "listing_id": payload.listing_id,
                "etsy_listing_id": int(etsy_listing_id) if etsy_listing_id and str(etsy_listing_id).isdigit() else None,
                "destination_country": country,
                "variant_selected": variant_label,
                "buyer_total": report.buyer_total_eur,
                "seller_net": report.seller_net_eur,
                "margin_eur": report.margin_eur,
                "margin_pct": report.margin_pct,
                "report": report.model_dump(mode="json", exclude={"id", "created_at"}),
            }
        ).execute()
        if saved.data:
            report.id = saved.data[0].get("id")
            report.created_at = saved.data[0].get("created_at")
    except Exception as exc:
        logger.warning("Historisation de la simulation échouée : %s", type(exc).__name__)

    return report


@router.get("/results/{listing_id}", response_model=List[ProductTestResult])
async def list_results(listing_id: str, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("product_test_results")
        .select("id,report,created_at")
        .eq("user_id", user.id)
        .eq("listing_id", listing_id)
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    )
    out: List[ProductTestResult] = []
    for row in result.data or []:
        report = row.get("report") or {}
        try:
            out.append(ProductTestResult(**{**report, "id": row["id"], "created_at": row["created_at"]}))
        except Exception:
            continue
    return out
