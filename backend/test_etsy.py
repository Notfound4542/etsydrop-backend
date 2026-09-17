#!/usr/bin/env python3
# =====================================================================
# === TEST_ETSY.PY — Vérif directe ETSY_API_KEY + ETSY_API_SECRET ===
# =====================================================================
#
# Appel HTTP brut vers l'API publique Etsy (GET /shops?shop_name=...) avec
# x-api-key: {ETSY_API_KEY}:{ETSY_API_SECRET} — ne passe PAS par le flow
# OAuth, ni par ce backend : ça isole deux questions à la fois, "ces deux
# clés fonctionnent-elles ensemble ?" et "shop_name se résout-il en shop_id ?"
#
# GET /shops/{shop_id} attend un ID NUMÉRIQUE dans le path (Etsy renvoie 400
# "Expected int value for shop_id (got string)" si on y met un nom) — la
# recherche par nom passe par GET /shops?shop_name=... (query param), qui
# renvoie {count, results: [...]}. Voir routers/auth.py > _resolve_shop_id,
# qui utilise exactement le même endpoint.
#
# 200 + shop_id résolu -> les clés et l'endpoint sont bons, le problème est
#                          ailleurs (Railway pas encore à jour, DB, sync...).
# 403/400/autre         -> les clés ou l'appel posent encore problème ; le
#                          corps de la réponse Etsy explique généralement
#                          pourquoi.
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

if not ETSY_API_KEY or not ETSY_API_SECRET:
    print("ETSY_API_KEY et/ou ETSY_API_SECRET absents de cet environnement :")
    print(f"  ETSY_API_KEY    : {'défini' if ETSY_API_KEY else 'MANQUANT'}")
    print(f"  ETSY_API_SECRET : {'défini' if ETSY_API_SECRET else 'MANQUANT'}")
    print()
    print("Renseigne-les dans backend/.env pour un test local, ou exécute ce")
    print("script là où ils sont réellement définis, ex. sur Railway :")
    print("  railway run python test_etsy.py")
    sys.exit(1)

url = "https://openapi.etsy.com/v3/application/shops"
params = {"shop_name": SHOP_NAME}
headers = {"x-api-key": f"{ETSY_API_KEY}:{ETSY_API_SECRET}"}

print(f"GET {url}?shop_name={SHOP_NAME}")
# Jamais la vraie valeur du secret à l'écran, même dans un script de debug.
print(f"x-api-key: {ETSY_API_KEY[:4]}...:{'*' * len(ETSY_API_SECRET)}")
print()

response = httpx.get(url, headers=headers, params=params, timeout=10)

print(f"Statut HTTP : {response.status_code}")
print("Corps de la réponse :")
print(response.text)
print()

if response.status_code == 200:
    payload = response.json()
    results = payload.get("results", [])
    if results:
        shop_id = results[0].get("shop_id")
        print(f"shop_id résolu : {shop_id}")
        print("Les clés et l'endpoint fonctionnent. Si le dashboard reste vide")
        print("malgré ça, le problème est ailleurs (Railway pas encore à jour,")
        print("DB, sync...).")
    else:
        print(f"Requête OK mais aucune boutique nommée '{SHOP_NAME}' trouvée —")
        print("vérifie l'orthographe exacte du nom de la boutique.")
else:
    print(f"Échec {response.status_code} — les clés, le format d'appel, ou le")
    print("nom de boutique posent encore problème. Voir le corps de la réponse")
    print("ci-dessus pour la raison exacte.")
