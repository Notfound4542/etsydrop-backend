#!/usr/bin/env python3
# =====================================================================
# === TEST_LISTINGS_API.PY — Vérif directe GET /api/listings/ ===
# =====================================================================
#
# ⚠️ Correction d'une prémisse : le token à utiliser ici N'EST PAS lisible
# depuis la table `etsy_tokens`. Cette table stocke le token OAuth ETSY du
# vendeur (format "{etsy_user_id}.{secret}", voir etsy_client.py), qui n'a
# rien à voir avec le token de SESSION SUPABASE attendu par
# Authorization: Bearer sur ce backend (voir database.py > get_current_user,
# qui vérifie ce token via GET {SUPABASE_URL}/auth/v1/user). Supabase
# n'expose nulle part une table interrogeable contenant les JWT de session
# émis — ils n'existent que côté client, le temps de la session.
#
# Ce script demande donc directement le JWT de session déjà émis pour le
# compte utilisé dans le navigateur : ouvre l'app EtsyDrop connectée, puis
# dans la console devtools :
#   JSON.parse(sessionStorage.getItem('ed_session')).access_token
# et colle la valeur ici (argument ou variable d'env SUPABASE_USER_JWT).
# C'est un jeton de session déjà émis et à courte durée de vie (pas un mot
# de passe) : le script ne le stocke nulle part, il l'utilise seulement le
# temps de l'appel.
#
# Ce que le script vérifie concrètement :
#   1. Le `sub` (= user_id) encodé dans le JWT, décodé LOCALEMENT sans appel
#      réseau — pour savoir immédiatement si le compte connecté dans le
#      navigateur est bien celui qui a les fiches en DB.
#   2. La réponse réelle de GET /api/listings/ sur le backend Railway avec
#      ce token — statut HTTP, corps complet, nombre de fiches renvoyées.
#
# Usage :
#   python test_listings_api.py <jwt>
#   ou : SUPABASE_USER_JWT=<jwt> python test_listings_api.py

import base64
import json
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import httpx

BACKEND_URL = os.getenv("BACKEND_URL", "https://etsydrop-backend-production.up.railway.app").rstrip("/")
EXPECTED_USER_ID = "68b8e866-e866-49cd-9d26-f718e433403d"

jwt = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("SUPABASE_USER_JWT", "")).strip()

if not jwt:
    print("Aucun JWT fourni.")
    print()
    print("Ce script a besoin du token de SESSION SUPABASE du compte utilisé")
    print("dans le navigateur (PAS le token Etsy stocké dans etsy_tokens — voir")
    print("le commentaire en tête de ce fichier). Pour le récupérer :")
    print("  1. Ouvre https://notfound4542.github.io/etsydrop-backend/ connecté")
    print("  2. Ouvre la console devtools (F12)")
    print("  3. Exécute : JSON.parse(sessionStorage.getItem('ed_session')).access_token")
    print("  4. Colle le résultat :")
    print("       python test_listings_api.py <jwt>")
    sys.exit(1)


# === DÉCODAGE LOCAL DU JWT (payload seulement — pas de vérification de
# signature, juste pour lire le `sub` avant même d'appeler le backend) ===
def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


print("=== Étape 1/2 : décodage local du JWT (sans appel réseau) ===")
payload = _decode_jwt_payload(jwt)
token_user_id = payload.get("sub")
token_email = payload.get("email")
print(f"sub (user_id)  : {token_user_id}")
print(f"email          : {token_email}")
print(f"exp            : {payload.get('exp')}")
print()

if token_user_id and token_user_id != EXPECTED_USER_ID:
    print(f"[ATTENTION] Ce token appartient à user_id={token_user_id!r}, PAS à")
    print(f"  {EXPECTED_USER_ID!r} (celui qui a les 11 fiches en DB, confirmé")
    print("  par sync_now.py). Si c'est le cas, /api/listings/ renverra []")
    print("  correctement (RLS/scoping par user_id fonctionne comme prévu) —")
    print("  le vrai problème est que le navigateur est connecté avec le")
    print("  mauvais compte EtsyDrop, pas un bug du backend.")
elif token_user_id == EXPECTED_USER_ID:
    print(f"[OK] Ce token correspond bien à user_id={EXPECTED_USER_ID!r}.")
print()

# === APPEL RÉEL AU BACKEND ===
print("=== Étape 2/2 : GET /api/listings/ sur le backend Railway ===")
print(f"GET {BACKEND_URL}/api/listings/")
resp = httpx.get(
    f"{BACKEND_URL}/api/listings/",
    headers={"Authorization": f"Bearer {jwt}"},
    timeout=15,
)
print(f"Statut HTTP : {resp.status_code}")
print("Corps de la réponse :")
print(resp.text)
print()

if resp.status_code == 200:
    data = resp.json()
    count = len(data) if isinstance(data, list) else "n/a (pas une liste)"
    print(f"Nombre de fiches retournées : {count}")
    if isinstance(data, list) and not data and token_user_id == EXPECTED_USER_ID:
        print()
        print("200 + [] alors que le JWT correspond au bon user_id et que")
        print("sync_now.py a confirmé des lignes en DB pour cet utilisateur :")
        print("vérifie que les lignes de `listings` ont bien user_id EXACTEMENT")
        print(f"'{EXPECTED_USER_ID}' (pas de casse différente, pas d'espace).")
else:
    print("Échec — voir le corps de la réponse ci-dessus. Un 401 confirme un")
    print("problème de token (expiré, mal copié) plutôt qu'un problème de")
    print("scoping des données.")
