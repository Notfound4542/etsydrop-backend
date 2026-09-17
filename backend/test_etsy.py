#!/usr/bin/env python3
# =====================================================================
# === TEST_ETSY.PY — Vérif directe ETSY_API_KEY + ETSY_API_SECRET ===
# =====================================================================
#
# Appel HTTP brut vers l'API Etsy, en dehors du flow OAuth et de ce backend,
# pour isoler où une régression se situe : clés d'app, résolution shop_id,
# ou lecture des fiches avec le token OAuth du vendeur.
#
# Étape 1 — GET /shops?shop_name=... (query param, findShops) : confirme que
# ETSY_API_KEY/ETSY_API_SECRET fonctionnent ensemble et résout shop_id.
# GET /shops/{shop_id} attend un ID NUMÉRIQUE dans le path (Etsy renvoie 400
# "Expected int value for shop_id (got string)" si on y met un nom) — voir
# routers/auth.py > _resolve_shop_id, qui utilise exactement cet endpoint.
#
# Étape 2 — lit le access_token OAuth le plus récent depuis la table
# `etsy_tokens` en Supabase (REST direct, sans dépendre de tout le backend),
# puis appelle GET /shops/{shop_id}/listings/active avec CE token en plus de
# x-api-key — exactement l'appel fait par routers/auth.py >
# _sync_etsy_listings. Si l'étape 1 marche mais pas l'étape 2, le problème
# est le token stocké (expiré, scope manquant) plutôt que les clés d'app.
#
# Usage :
#   python test_etsy.py                     # shop_name par défaut
#   python test_etsy.py mon_autre_shop_name

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv absent : on utilise os.environ tel quel

import httpx

SHOP_NAME = sys.argv[1] if len(sys.argv) > 1 else "atelierlanterdesign"

ETSY_API_KEY = os.getenv("ETSY_API_KEY", "").strip().strip("\"'")
ETSY_API_SECRET = os.getenv("ETSY_API_SECRET", "").strip().strip("\"'")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().strip("\"'").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip().strip("\"'")

if not ETSY_API_KEY or not ETSY_API_SECRET:
    print("ETSY_API_KEY et/ou ETSY_API_SECRET absents de cet environnement :")
    print(f"  ETSY_API_KEY    : {'défini' if ETSY_API_KEY else 'MANQUANT'}")
    print(f"  ETSY_API_SECRET : {'défini' if ETSY_API_SECRET else 'MANQUANT'}")
    print()
    print("Renseigne-les dans backend/.env pour un test local, ou exécute ce")
    print("script là où ils sont réellement définis, ex. sur Railway :")
    print("  railway run python test_etsy.py")
    sys.exit(1)

api_key_header = f"{ETSY_API_KEY}:{ETSY_API_SECRET}"

# === ÉTAPE 1 : RÉSOLUTION shop_name -> shop_id ===
print("=== Étape 1/2 : GET /shops?shop_name=... (clés d'app) ===")
url = "https://openapi.etsy.com/v3/application/shops"
params = {"shop_name": SHOP_NAME}
headers = {"x-api-key": api_key_header}

print(f"GET {url}?shop_name={SHOP_NAME}")
# Jamais la vraie valeur du secret à l'écran, même dans un script de debug.
print(f"x-api-key: {ETSY_API_KEY[:4]}...:{'*' * len(ETSY_API_SECRET)}")
print()

response = httpx.get(url, headers=headers, params=params, timeout=10)

print(f"Statut HTTP : {response.status_code}")
print("Corps de la réponse :")
print(response.text)
print()

shop_id = None
if response.status_code == 200:
    payload = response.json()
    results = payload.get("results", [])
    if results:
        shop_id = results[0].get("shop_id")
        print(f"shop_id résolu : {shop_id}")
    else:
        print(f"Requête OK mais aucune boutique nommée '{SHOP_NAME}' trouvée —")
        print("vérifie l'orthographe exacte du nom de la boutique.")
else:
    print(f"Échec {response.status_code} — les clés, le format d'appel, ou le")
    print("nom de boutique posent encore problème. Voir le corps de la réponse")
    print("ci-dessus pour la raison exacte.")

print()

# === ÉTAPE 2 : LECTURE DES FICHES ACTIVES AVEC LE TOKEN OAUTH DU VENDEUR ===
print("=== Étape 2/2 : GET /shops/{shop_id}/listings/active (token vendeur) ===")

if not shop_id:
    print("Ignorée : shop_id non résolu à l'étape 1.")
    sys.exit(0)

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Ignorée : SUPABASE_URL et/ou SUPABASE_KEY absents de cet environnement :")
    print(f"  SUPABASE_URL : {'défini' if SUPABASE_URL else 'MANQUANT'}")
    print(f"  SUPABASE_KEY : {'défini' if SUPABASE_KEY else 'MANQUANT'}")
    print("(clé service_role — Project Settings > API — nécessaire pour lire")
    print("etsy_tokens en contournant la RLS, comme le fait le backend.)")
    sys.exit(0)

supabase_resp = httpx.get(
    f"{SUPABASE_URL}/rest/v1/etsy_tokens",
    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    params={"select": "access_token,shop_id", "order": "updated_at.desc", "limit": 1},
    timeout=10,
)

if supabase_resp.status_code != 200:
    print(f"Échec de lecture de etsy_tokens sur Supabase : {supabase_resp.status_code}")
    print(supabase_resp.text)
    sys.exit(1)

rows = supabase_resp.json()
if not rows or not rows[0].get("access_token"):
    print("Aucune ligne etsy_tokens avec un access_token trouvée — reconnecte la")
    print("boutique Etsy d'abord (le token n'est écrit qu'après le callback OAuth).")
    sys.exit(1)

access_token = rows[0]["access_token"]
db_shop_id = rows[0].get("shop_id")
print(f"access_token lu depuis Supabase (shop_id en DB : {db_shop_id}).")

listings_url = f"https://openapi.etsy.com/v3/application/shops/{shop_id}/listings/active"
listings_headers = {"x-api-key": api_key_header, "Authorization": f"Bearer {access_token}"}
listings_params = {"limit": 100}

print(f"GET {listings_url} (limit=100)")
listings_resp = httpx.get(listings_url, headers=listings_headers, params=listings_params, timeout=10)

print(f"Statut HTTP : {listings_resp.status_code}")

if listings_resp.status_code != 200:
    print("Corps de la réponse :")
    print(listings_resp.text)
    sys.exit(1)

listings_payload = listings_resp.json()
listings_results = listings_payload.get("results", [])
print(f"listing_active_count : {listings_payload.get('count', len(listings_results))} fiches actives")
for item in listings_results[:3]:
    print(f"  - {item.get('listing_id')} : {item.get('title')}")

if not listings_results:
    print("Aucune fiche renvoyée malgré un 200 — vérifie que la boutique a bien")
    print("des fiches actives sur Etsy.")
