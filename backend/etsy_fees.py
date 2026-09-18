# =====================================================================
# === ETSY_FEES.PY — Constantes de frais Etsy + table pays partagées ===
# =====================================================================
#
# Source unique pour routers/pricing.py (analyse prix par pays) et
# routers/product_test.py (simulation d'achat) : les deux modules doivent
# donner exactement les mêmes chiffres pour une même fiche, sinon
# l'utilisateur voit deux marges différentes pour un même produit.
#
# Barème (voir CLAUDE.md > Taxes & Change et la spec Phase 2) :
#   - Commission Etsy        : 6,5 % du montant encaissé (prix + port)
#   - Frais de transaction   : 0,20 € fixes par vente
#   - Frais de mise en vente : 0,18 € (0,20 $) par vente / renouvellement
#   - Frais de change Etsy   : 1,8 % si la devise de l'acheteur n'est pas l'EUR
#   - TVA locale             : prise sur le prix TTC (prix HT = TTC / (1 + taux))
#
# Rappel : Etsy collecte lui-même la sales tax US, la GST AU et la TVA UK
# import (<135 £) en tant que "marketplace facilitator" — ces montants sont
# AJOUTÉS au prix côté acheteur, jamais déduits du net vendeur. C'est pour
# ça que le taux US est 0 ici et que UK/AU sont documentés comme collectés
# par Etsy (vat_collected_by_etsy) : la ligne TVA reste informative.

from typing import Dict, Optional

ETSY_TRANSACTION_FEE_PCT = 0.065
ETSY_FIXED_TRANSACTION_FEE_EUR = 0.20
ETSY_LISTING_FEE_EUR = 0.18
ETSY_CURRENCY_CONVERSION_PCT = 0.018

# Marge nette minimale pour considérer un pays "viable" (code couleur vert).
MIN_VIABLE_MARGIN_PCT = 25.0

# === TABLE PAYS ===
# shipping_default = coût d'expédition moyen depuis la France (€) utilisé
# quand aucun profil de livraison n'est fourni. Les taux de change sont
# indicatifs, mis à jour à la main (TODO : API de change).
COUNTRIES: Dict[str, dict] = {
    "FR": {"name": "France",        "flag": "🇫🇷", "currency": "EUR", "rate": 1.00,  "vat_rate": 0.20, "vat_label": "TVA 20 %",              "vat_collected_by_etsy": False, "shipping_default": 1.80, "days": (3, 6)},
    "DE": {"name": "Allemagne",     "flag": "🇩🇪", "currency": "EUR", "rate": 1.00,  "vat_rate": 0.19, "vat_label": "TVA 19 %",              "vat_collected_by_etsy": False, "shipping_default": 1.80, "days": (4, 7)},
    "IT": {"name": "Italie",        "flag": "🇮🇹", "currency": "EUR", "rate": 1.00,  "vat_rate": 0.22, "vat_label": "TVA 22 %",              "vat_collected_by_etsy": False, "shipping_default": 2.10, "days": (4, 8)},
    "ES": {"name": "Espagne",       "flag": "🇪🇸", "currency": "EUR", "rate": 1.00,  "vat_rate": 0.21, "vat_label": "TVA 21 %",              "vat_collected_by_etsy": False, "shipping_default": 2.10, "days": (4, 8)},
    "NL": {"name": "Pays-Bas",      "flag": "🇳🇱", "currency": "EUR", "rate": 1.00,  "vat_rate": 0.21, "vat_label": "TVA 21 %",              "vat_collected_by_etsy": False, "shipping_default": 1.90, "days": (3, 6)},
    "BE": {"name": "Belgique",      "flag": "🇧🇪", "currency": "EUR", "rate": 1.00,  "vat_rate": 0.21, "vat_label": "TVA 21 %",              "vat_collected_by_etsy": False, "shipping_default": 1.80, "days": (3, 5)},
    "GB": {"name": "Royaume-Uni",   "flag": "🇬🇧", "currency": "GBP", "rate": 0.86,  "vat_rate": 0.20, "vat_label": "TVA 20 % (collectée par Etsy <135 £)", "vat_collected_by_etsy": True, "shipping_default": 2.90, "days": (6, 10)},
    "US": {"name": "États-Unis",    "flag": "🇺🇸", "currency": "USD", "rate": 1.08,  "vat_rate": 0.00, "vat_label": "Sales tax collectée par Etsy", "vat_collected_by_etsy": True, "shipping_default": 3.40, "days": (8, 12)},
    "CA": {"name": "Canada",        "flag": "🇨🇦", "currency": "CAD", "rate": 1.47,  "vat_rate": 0.05, "vat_label": "TPS 5 % (+TVQ/HST selon province)", "vat_collected_by_etsy": True, "shipping_default": 3.90, "days": (10, 15)},
    "AU": {"name": "Australie",     "flag": "🇦🇺", "currency": "AUD", "rate": 1.65,  "vat_rate": 0.10, "vat_label": "GST 10 % (collectée par Etsy)", "vat_collected_by_etsy": True, "shipping_default": 4.60, "days": (12, 18)},
    "CH": {"name": "Suisse",        "flag": "🇨🇭", "currency": "CHF", "rate": 0.96,  "vat_rate": 0.081, "vat_label": "TVA 8,1 %",            "vat_collected_by_etsy": False, "shipping_default": 3.20, "days": (5, 9)},
}

# Pays affichés par défaut (ordre d'affichage) quand aucun profil de livraison
# ne restreint la liste — alignés sur app().countries côté frontend.
DEFAULT_COUNTRY_CODES = ["FR", "US", "GB", "DE", "AU", "CA"]

# Alias fréquents (Etsy et le frontend historique utilisent "UK").
_ALIASES = {"UK": "GB"}


def normalize_country(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    code = code.strip().upper()
    return _ALIASES.get(code, code)


def country_info(code: str) -> Optional[dict]:
    return COUNTRIES.get(normalize_country(code) or "")


# === MOTEUR DE CALCUL ===
def compute_sale_breakdown(
    *,
    sale_price: float,
    shipping_charged: float,
    shipping_cost: float,
    cost_price: Optional[float],
    country_code: str,
    vat_applicable: bool = True,
) -> dict:
    """
    Décompose une vente à un prix donné. Toutes les valeurs en EUR.

    sale_price       : prix produit affiché à l'acheteur (TTC)
    shipping_charged : port facturé à l'acheteur (0 si livraison gratuite)
    shipping_cost    : port réellement payé au transporteur / fournisseur
    cost_price       : coût d'achat fournisseur (None si inconnu → marge None)
    vat_applicable   : False pour un vendeur en franchise de TVA (auto-
                       entrepreneur sous le seuil) : la TVA n'est pas déduite.

    La commission Etsy et les frais de change s'appliquent au montant TOTAL
    encaissé (prix + port) — Etsy facture bien sa commission sur le port.
    """
    info = country_info(country_code)
    if not info:
        raise ValueError(f"Pays non supporté : {country_code}")

    gross = sale_price + shipping_charged
    etsy_commission = gross * ETSY_TRANSACTION_FEE_PCT
    etsy_fixed = ETSY_FIXED_TRANSACTION_FEE_EUR + ETSY_LISTING_FEE_EUR
    is_foreign = info["currency"] != "EUR"
    currency_fee = gross * ETSY_CURRENCY_CONVERSION_PCT if is_foreign else 0.0

    # TVA "à ta charge" = celle que TU dois reverser (pas celle collectée par
    # Etsy en tant que marketplace). Sur le prix produit uniquement.
    vat_rate = info["vat_rate"] if (vat_applicable and not info["vat_collected_by_etsy"]) else 0.0
    vat_amount = sale_price - sale_price / (1 + vat_rate) if vat_rate else 0.0

    etsy_fees_total = etsy_commission + etsy_fixed
    seller_net = gross - etsy_fees_total - currency_fee - vat_amount - shipping_cost
    margin_eur = (seller_net - cost_price) if cost_price is not None else None
    margin_pct = (margin_eur / sale_price * 100) if (margin_eur is not None and sale_price > 0) else None

    return {
        "country_code": normalize_country(country_code),
        "country_name": info["name"],
        "flag": info["flag"],
        "currency": info["currency"],
        "sale_price_eur": round(sale_price, 2),
        "sale_price_local": round(sale_price * info["rate"], 2),
        "shipping_charged_eur": round(shipping_charged, 2),
        "shipping_cost_eur": round(shipping_cost, 2),
        "buyer_total_eur": round(gross, 2),
        "buyer_total_local": round(gross * info["rate"], 2),
        "etsy_commission_eur": round(etsy_commission, 2),
        "etsy_fixed_fees_eur": round(etsy_fixed, 2),
        "etsy_fees_eur": round(etsy_fees_total, 2),
        "currency_fee_eur": round(currency_fee, 2),
        "vat_rate_pct": round(info["vat_rate"] * 100, 1),
        "vat_label": info["vat_label"],
        "vat_collected_by_etsy": info["vat_collected_by_etsy"],
        "vat_eur": round(vat_amount, 2),
        "seller_net_eur": round(seller_net, 2),
        "cost_price_eur": round(cost_price, 2) if cost_price is not None else None,
        "margin_eur": round(margin_eur, 2) if margin_eur is not None else None,
        "margin_pct": round(margin_pct, 1) if margin_pct is not None else None,
        "delivery_days_min": info["days"][0],
        "delivery_days_max": info["days"][1],
    }


def recommended_price(
    *,
    cost_price: float,
    target_margin_pct: float,
    country_code: str,
    shipping_cost: float,
    free_shipping: bool,
    vat_applicable: bool = True,
) -> Optional[float]:
    """
    Prix de vente (TTC, EUR) tel que la marge nette RÉELLE après tous les
    frais = marge cible. La formule naïve cost/(1-marge) ignore la commission
    Etsy, le change et la TVA et donne une marge réelle bien inférieure à la
    cible ; on résout donc l'équation complète :

        net = P·(1 - com - change - tva/(1+tva)) - fixes - port_absorbé - coût
        net = m·P
        ⇒ P = (coût + fixes + port_absorbé) / (1 - m - com - change - tva/(1+tva))

    En livraison payante, le port est facturé à part (P + S côté acheteur) et
    la commission/change sur le port sont couvertes par... le port lui-même
    n'étant qu'un pass-through, le petit surcoût (6,5 % + 1,8 % de S) est
    réintégré dans P pour tenir la marge cible.

    Retourne None si la marge cible est inatteignable (dénominateur ≤ 0).
    """
    info = country_info(country_code)
    if not info:
        return None
    m = max(0.0, min(0.95, target_margin_pct / 100))
    is_foreign = info["currency"] != "EUR"
    change = ETSY_CURRENCY_CONVERSION_PCT if is_foreign else 0.0
    vat_rate = info["vat_rate"] if (vat_applicable and not info["vat_collected_by_etsy"]) else 0.0
    vat_share = vat_rate / (1 + vat_rate) if vat_rate else 0.0

    fixed = ETSY_FIXED_TRANSACTION_FEE_EUR + ETSY_LISTING_FEE_EUR
    if free_shipping:
        absorbed = shipping_cost
    else:
        # Port facturé = port payé ; il ne reste que les frais Etsy sur le port.
        absorbed = shipping_cost * (ETSY_TRANSACTION_FEE_PCT + change)

    denominator = 1 - m - ETSY_TRANSACTION_FEE_PCT - change - vat_share
    if denominator <= 0.02:
        return None
    return (cost_price + fixed + absorbed) / denominator


def psychological_round(price: float) -> float:
    """24,37 → 24,99 ; 24,995 → 25,99 (jamais en dessous du prix calculé)."""
    import math

    base = math.ceil(price)
    candidate = base - 0.01
    return round(candidate if candidate >= price else base + 0.99, 2)
