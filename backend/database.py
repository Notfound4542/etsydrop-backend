# =====================================================================
# === DATABASE.PY — Client Supabase + vérification de session (Auth) ===
# =====================================================================
#
# Ce module est le point d'entrée unique vers Supabase :
# - `get_supabase()` retourne le client Postgres/Auth partagé par toute l'app.
# - `get_current_user()` est une dépendance FastAPI qui vérifie le JWT
#   émis par Supabase Auth via l'API officielle (supporte HS256 et ECC P-256).
#
# Toutes les requêtes passent par le query builder officiel du client
# Supabase (PostgREST) : `.select()`, `.eq()`, `.ilike()`, `.insert()`, ...
# Ce sont des requêtes paramétrées par construction — aucune chaîne SQL
# n'est jamais concaténée à la main dans ce projet.

import os
from functools import lru_cache

from dotenv import load_dotenv
from fastapi import Header, HTTPException
from supabase import Client, create_client

from models import CurrentUser

load_dotenv()

# === CONFIGURATION (jamais loggée, jamais renvoyée dans une réponse API) ===
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# === CLIENT SUPABASE (singleton) ===
@lru_cache
def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "SUPABASE_URL et SUPABASE_KEY doivent être définis (voir .env.example)."
        )
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# === DÉPENDANCE FASTAPI : UTILISATEUR COURANT ===
def get_current_user(authorization: str = Header(..., description="Bearer <supabase_access_token>")) -> CurrentUser:
    """
    Vérifie le token via l'API Supabase Auth — compatible avec HS256 (legacy)
    et ECC P-256 (nouveau format depuis la migration des clés JWT de Supabase).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token manquant ou mal formé.")

    token = authorization.removeprefix("Bearer ").strip()

    try:
        supabase = get_supabase()
        user_response = supabase.auth.get_user(token)
        if not user_response or not user_response.user:
            raise HTTPException(status_code=401, detail="Session invalide ou expirée.")
        user = user_response.user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Session invalide ou expirée.")

    return CurrentUser(
        id=user.id,
        email=user.email,
        etsy_shop_connected=False,
    )
