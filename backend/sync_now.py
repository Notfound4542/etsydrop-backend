#!/usr/bin/env python3
# =====================================================================
# === SYNC_NOW.PY — Rejoue la sync Etsy -> listings en local, pas à pas ===
# =====================================================================
#
# Fait exactement ce que fait etsy_callback juste après l'échange OAuth
# (voir routers/auth.py + etsy_client.py > sync_etsy_listings), mais :
#   - en local, avec les credentials de backend/.env
#   - sans importer aucun module du backend (pas de supabase-py, pas de
#     FastAPI) : uniquement httpx, en appels REST bruts vers Supabase et
#     Etsy — pour qu'aucune couche d'abstraction ne puisse masquer où ça
#     échoue réellement
#   - un upsert PAR FICHE (au lieu d'un upsert groupé) : chaque appel
#     affiche son propre statut HTTP et son propre corps de réponse, donc
#     une erreur sur une seule fiche n'est jamais noyée dans un message
#     d'erreur global portant sur tout le lot
#
# Usage :
#   python sync_now.py

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv absent : on utilise os.environ tel quel

import httpx

ETSY_API_KEY = os.getenv("ETSY_API_KEY", "").strip().strip("\"'")
ETSY_API_SECRET = os.getenv("ETSY_API_SECRET", "").strip().strip("\"'")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().strip("\"'").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip().strip("\"'")

missing = [
    name for name, val in [
        ("ETSY_API_KEY", ETSY_API_KEY),
        ("ETSY_API_SECRET", ETSY_API_SECRET),
        ("SUPABASE_URL", SUPABASE_URL),
        ("SUPABASE_KEY", SUPABASE_KEY),
    ]
    if not val
]
if missing:
    print("Variables manquantes dans cet environnement :", ", ".join(missing))
    print("Renseigne-les dans backend/.env pour un test local, ou exécute ce")
    print("script là où elles sont réellement définies, ex. sur Railway :")
    print("  railway run python sync_now.py")
    sys.exit(1)

SUPABASE_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
ETSY_HEADERS_BASE = {"x-api-key": f"{ETSY_API_KEY}:{ETSY_API_SECRET}"}


# === ÉTAPE 1 : LECTURE DE etsy_tokens (access_token + shop_id) ===
print("=== Étape 1/4 : lecture de etsy_tokens ===")
tokens_resp = httpx.get(
    f"{SUPABASE_URL}/rest/v1/etsy_tokens",
    headers=SUPABASE_HEADERS,
    params={"select": "user_id,access_token,shop_id,shop_name", "order": "updated_at.desc", "limit": 1},
    timeout=10,
)
print(f"Statut HTTP : {tokens_resp.status_code}")
if tokens_resp.status_code != 200:
    print("Corps de la réponse :", tokens_resp.text)
    sys.exit(1)

rows = tokens_resp.json()
if not rows or not rows[0].get("access_token"):
    print("Aucune ligne etsy_tokens avec un access_token trouvée.")
    sys.exit(1)

user_id = rows[0]["user_id"]
access_token = rows[0]["access_token"]
shop_id = rows[0].get("shop_id")
print(f"user_id  : {user_id}")
print(f"shop_id  : {shop_id}")
print(f"shop_name: {rows[0].get('shop_name')}")
print(f"access_token présent : {'oui' if access_token else 'non'}")
print()

if not shop_id:
    print("shop_id est NULL en base — impossible d'appeler /listings/active.")
    print("Reconnecte la boutique (ou corrige shop_id) avant de relancer ce script.")
    sys.exit(1)


# === ÉTAPE 2 : FICHES ACTIVES DEPUIS ETSY ===
print(f"=== Étape 2/4 : GET /shops/{shop_id}/listings/active ===")
etsy_headers = {**ETSY_HEADERS_BASE, "Authorization": f"Bearer {access_token}"}
listings_resp = httpx.get(
    f"https://openapi.etsy.com/v3/application/shops/{shop_id}/listings/active",
    headers=etsy_headers,
    params={"limit": 100},
    timeout=10,
)
print(f"Statut HTTP : {listings_resp.status_code}")
if listings_resp.status_code != 200:
    print("Corps de la réponse :", listings_resp.text)
    sys.exit(1)

etsy_listings = listings_resp.json().get("results", [])
print(f"Fiches reçues d'Etsy : {len(etsy_listings)}")
print()

if not etsy_listings:
    print("Etsy n'a renvoyé aucune fiche active — rien à synchroniser.")
    sys.exit(0)


# === ÉTAPE 3 : UPSERT DE CHAQUE FICHE, UNE PAR UNE, VIA L'API REST SUPABASE ===
# Même correspondance de champs que etsy_client.py > sync_etsy_listings,
# dupliquée ici volontairement (script indépendant du code du backend).
def _map_row(item: dict) -> dict | None:
    listing_id = str(item.get("listing_id") or "")
    if not listing_id:
        return None

    title = (item.get("title") or "Fiche Etsy sans titre").strip()[:140] or "Fiche Etsy"
    if len(title) < 3:
        title = title.ljust(3, ".")

    description = (item.get("description") or "").strip()[:2000]
    if len(description) < 10:
        description = f"{title} — fiche importée depuis Etsy."

    price_data = item.get("price")
    if isinstance(price_data, dict) and "amount" in price_data:
        price = float(price_data["amount"]) / float(price_data.get("divisor", 100) or 100)
    else:
        price = float(item.get("price") or 0)

    tags = [t for t in (item.get("tags") or []) if t][:13] or ["etsy import"]

    quantity = int(item.get("quantity") or 0)
    if quantity <= 0:
        stock_status = "out_of_stock"
    elif quantity < 5:
        stock_status = "low_stock"
    else:
        stock_status = "available"

    return {
        "user_id": user_id,
        "etsy_listing_id": listing_id,
        "name": title,
        "description": description,
        "tags": tags,
        "price_min": round(price, 2),
        "price_max": round(price, 2),
        "supplier": "my_catalog",
        "variants": [],
        "stock_status": stock_status,
        "margin_pct": 0,
    }


print("=== Étape 3/4 : upsert dans `listings` (un appel par fiche) ===")
upsert_headers = {
    **SUPABASE_HEADERS,
    "Content-Type": "application/json",
    # merge-duplicates : upsert via ON CONFLICT sur le on_conflict fourni en
    # query string plutôt qu'un INSERT qui échouerait sur les fiches déjà
    # importées lors d'une resynchronisation.
    "Prefer": "resolution=merge-duplicates,return=representation",
}

success_count = 0
for item in etsy_listings:
    row = _map_row(item)
    if row is None:
        print(f"  - (ignorée, pas de listing_id exploitable) : {item.get('title')!r}")
        continue

    resp = httpx.post(
        f"{SUPABASE_URL}/rest/v1/listings",
        headers=upsert_headers,
        params={"on_conflict": "user_id,etsy_listing_id"},
        json=row,
        timeout=10,
    )
    if resp.status_code in (200, 201):
        success_count += 1
        print(f"  - etsy_listing_id={row['etsy_listing_id']} ({row['name'][:40]!r}) -> {resp.status_code} OK")
    else:
        print(f"  - etsy_listing_id={row['etsy_listing_id']} ({row['name'][:40]!r}) -> {resp.status_code} ÉCHEC")
        print(f"    Corps de la réponse : {resp.text}")

print()
print(f"Upserts réussis : {success_count}/{len(etsy_listings)}")
print()


# === ÉTAPE 4 : COUNT(*) FROM listings APRÈS SYNC ===
print("=== Étape 4/4 : COUNT(*) FROM listings ===")
count_resp = httpx.get(
    f"{SUPABASE_URL}/rest/v1/listings",
    headers={**SUPABASE_HEADERS, "Prefer": "count=exact"},
    params={"select": "id", "limit": 1},
    timeout=10,
)
content_range = count_resp.headers.get("content-range", "")
total = content_range.split("/")[-1] if "/" in content_range else "?"
print(f"Statut HTTP : {count_resp.status_code}")
print(f"COUNT(*) FROM listings (toutes lignes, tous utilisateurs) : {total}")

user_count_resp = httpx.get(
    f"{SUPABASE_URL}/rest/v1/listings",
    headers={**SUPABASE_HEADERS, "Prefer": "count=exact"},
    params={"select": "id", "user_id": f"eq.{user_id}", "limit": 1},
    timeout=10,
)
user_content_range = user_count_resp.headers.get("content-range", "")
user_total = user_content_range.split("/")[-1] if "/" in user_content_range else "?"
print(f"COUNT(*) FROM listings WHERE user_id='{user_id}' : {user_total}")
