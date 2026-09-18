# =====================================================================
# === TEST_PHASE2.PY — Tests unitaires Phase 2 (sans réseau ni DB) ===
# =====================================================================
#
# Lancement : cd backend && python -m pytest test_phase2.py -q
#          ou : python test_phase2.py
#
# Couvre le moteur de frais partagé (etsy_fees.py), l'analyse SEO
# heuristique (routers/seo.py > _analyze / _heuristic_optimize) et la
# validation Pydantic des nouveaux endpoints (via TestClient, réponses 401
# attendues sans token — prouve que les routes existent et sont protégées).

import math
import sys

from etsy_fees import (
    ETSY_FIXED_TRANSACTION_FEE_EUR,
    ETSY_LISTING_FEE_EUR,
    compute_sale_breakdown,
    normalize_country,
    psychological_round,
    recommended_price,
)


# === ETSY_FEES ===
def test_breakdown_france_no_change_fee():
    b = compute_sale_breakdown(sale_price=24.99, shipping_charged=1.80, shipping_cost=1.80, cost_price=4.20, country_code="FR")
    assert b["currency_fee_eur"] == 0.0
    assert b["buyer_total_eur"] == 26.79
    # Commission 6,5 % sur prix + port
    assert b["etsy_commission_eur"] == round(26.79 * 0.065, 2)
    assert b["etsy_fixed_fees_eur"] == round(ETSY_FIXED_TRANSACTION_FEE_EUR + ETSY_LISTING_FEE_EUR, 2)
    # TVA FR 20 % sur le prix produit : 24.99 - 24.99/1.2
    assert b["vat_eur"] == round(24.99 - 24.99 / 1.2, 2)
    assert b["margin_eur"] is not None and b["margin_pct"] is not None
    expected_net = 26.79 - b["etsy_commission_eur"] - 0.38 - b["vat_eur"] - 1.80
    assert math.isclose(b["seller_net_eur"], expected_net, abs_tol=0.02)


def test_breakdown_us_has_change_fee_and_no_vat():
    b = compute_sale_breakdown(sale_price=30.0, shipping_charged=0.0, shipping_cost=3.40, cost_price=5.0, country_code="US")
    assert b["currency_fee_eur"] == round(30.0 * 0.018, 2)
    assert b["vat_eur"] == 0.0  # sales tax collectée par Etsy, pas déduite
    assert b["vat_collected_by_etsy"] is True
    assert b["currency"] == "USD"
    assert b["sale_price_local"] == round(30.0 * 1.08, 2)


def test_breakdown_without_cost_price_gives_none_margin():
    b = compute_sale_breakdown(sale_price=20.0, shipping_charged=2.0, shipping_cost=2.0, cost_price=None, country_code="DE")
    assert b["margin_eur"] is None and b["margin_pct"] is None
    assert b["seller_net_eur"] > 0


def test_vat_not_applicable_franchise():
    with_vat = compute_sale_breakdown(sale_price=20.0, shipping_charged=0, shipping_cost=0, cost_price=5, country_code="FR", vat_applicable=True)
    without = compute_sale_breakdown(sale_price=20.0, shipping_charged=0, shipping_cost=0, cost_price=5, country_code="FR", vat_applicable=False)
    assert without["vat_eur"] == 0.0
    assert without["seller_net_eur"] > with_vat["seller_net_eur"]


def test_recommended_price_holds_target_margin():
    """Au prix recommandé (non arrondi), la marge nette réelle == marge cible."""
    for code in ("FR", "US", "GB", "AU"):
        for free in (True, False):
            price = recommended_price(cost_price=4.20, target_margin_pct=35, country_code=code, shipping_cost=3.0, free_shipping=free)
            assert price is not None and price > 4.20
            b = compute_sale_breakdown(
                sale_price=price,
                shipping_charged=0.0 if free else 3.0,
                shipping_cost=3.0,
                cost_price=4.20,
                country_code=code,
            )
            assert abs(b["margin_pct"] - 35.0) < 0.6, (code, free, b["margin_pct"])


def test_recommended_price_free_shipping_is_higher():
    paid = recommended_price(cost_price=4.20, target_margin_pct=35, country_code="US", shipping_cost=3.4, free_shipping=False)
    free = recommended_price(cost_price=4.20, target_margin_pct=35, country_code="US", shipping_cost=3.4, free_shipping=True)
    assert free > paid


def test_recommended_price_unreachable_margin():
    assert recommended_price(cost_price=10, target_margin_pct=95, country_code="FR", shipping_cost=0, free_shipping=False) is None


def test_psychological_round():
    assert psychological_round(24.37) == 24.99
    assert psychological_round(24.995) == 25.99
    assert psychological_round(10.0) == 10.99 or psychological_round(10.0) == 9.99  # borne : ceil(10)=10 → 9.99 < 10 → 10.99
    assert psychological_round(24.37) >= 24.37


def test_normalize_country_aliases():
    assert normalize_country("uk") == "GB"
    assert normalize_country(" us ") == "US"
    assert normalize_country(None) is None


# === SEO HEURISTIQUE ===
def test_seo_analyze_scores_and_suggestions():
    from routers.seo import _analyze

    good = _analyze(
        "Personalized Gold Name Necklace - Custom Jewelry Gift for Her, Dainty Minimalist Birthday Gift",
        ["personalized gold", "name necklace", "custom jewelry", "gift for her", "dainty necklace", "minimalist gift",
         "birthday gift", "gold plated", "custom name", "jewelry gift", "mothers day gift", "bridesmaid gift", "initial necklace"],
        "Personalized gold name necklace, handcrafted to order.\n\nMaterials: 18K gold plated stainless steel, hypoallergenic.\n\n"
        "Chain length 40-50 cm adjustable. Ships from France in a gift box. Perfect birthday gift for her.",
    )
    assert good["score"] >= 75
    assert good["tags_count"] == 13
    assert good["title"].score >= 70

    bad = _analyze("necklace", ["necklace", "gold"], "Nice necklace.")
    assert bad["score"] < 40
    assert any("13 tags" in s or "tag" in s.lower() for s in bad["suggestions"])
    assert any("titre" in s.lower() for s in bad["suggestions"])
    assert bad["description"].score < 30


def test_seo_heuristic_optimize_respects_etsy_limits():
    from routers.seo import _heuristic_optimize

    out = _heuristic_optimize("necklace gold", ["necklace", "gold"], "Nice necklace.", ["gold name necklace", "custom jewelry gift"])
    assert len(out["title"]) <= 140
    assert 1 <= len(out["tags"]) <= 13
    assert all(len(t) <= 20 for t in out["tags"])
    assert len(out["description"]) >= 150
    assert out["engine"] == "heuristic"


# === ROUTES PROTÉGÉES (existent + exigent un token) ===
def test_new_routes_require_auth():
    from fastapi.testclient import TestClient

    import main

    client = TestClient(main.app)
    for method, path, body in [
        ("get", "/api/suppliers/", None),
        ("post", "/api/suppliers/", {"name": "Eprolo", "platform": "eprolo"}),
        ("get", "/api/suppliers/conversations/", None),
        ("get", "/api/pricing/profiles/", None),
        ("post", "/api/pricing/analyze", {"cost_price": 4.2}),
        ("get", "/api/seo/analyze/00000000-0000-0000-0000-000000000000", None),
        ("get", "/api/seo/keywords/trending?q=necklace", None),
        ("post", "/api/product-test/simulate", {"listing_id": "00000000-0000-0000-0000-000000000000", "destination_country": "US"}),
    ]:
        r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        # 401 (token manquant) ou 422 (header Authorization requis par FastAPI) — jamais 404/500.
        assert r.status_code in (401, 422), (method, path, r.status_code, r.text[:200])


def test_pydantic_rejects_bad_supplier_payload():
    from pydantic import ValidationError

    from models import SupplierCreate, SupplierProductCreate

    try:
        SupplierCreate(name="x", platform="eprolo")
        assert False, "name trop court accepté"
    except ValidationError:
        pass
    try:
        SupplierCreate(name="Eprolo", contact_email="pas-un-email")
        assert False, "email invalide accepté"
    except ValidationError:
        pass
    try:
        SupplierProductCreate(name="Produit", base_price=-1)
        assert False, "prix négatif accepté"
    except ValidationError:
        pass
    # Le modèle de réponse Supplier n'a pas de champ api_key : impossible de le fuiter.
    from models import Supplier

    assert "api_key" not in Supplier.model_fields and "api_secret" not in Supplier.model_fields


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK    {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
