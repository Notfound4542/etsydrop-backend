# =====================================================================
# === DATABASE.PY — Client Supabase + vérification de session (Auth) ===
# =====================================================================
#
# Ce module est le point d'entrée unique vers Supabase :
# - `get_supabase()` retourne le client Postgres/Auth partagé par toute l'app.
# - `get_current_user()` est une dépendance FastAPI qui vérifie le JWT
#   émis par Supabase Auth (aucune authentification maison ici).
#
# Toutes les requêtes passent par le query builder officiel du client
# Supabase (PostgREST) : `.select()`, `.eq()`, `.ilike()`, `.insert()`, ...
# Ce sont des requêtes paramétrées par construction — aucune chaîne SQL
# n'est jamais concaténée à la main dans ce projet.

import os
from functools import lru_cache

from dotenv import load_dotenv
from fastapi import Header, HTTPException
from jose import JWTError, jwt
from supabase import Client, create_client

from models import CurrentUser

load_dotenv()

# === CONFIGURATION (jamais logguée, jamais renvoyée dans une réponse API) ===
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")


# === CLIENT SUPABASE (singleton) ===
@lru_cache
def get_supabase() -> Client:
    """
    Retourne un client Supabase unique, réutilisé pour toute la durée de vie
    du process. Toutes les requêtes de l'API passent par ce client.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "SUPABASE_URL et SUPABASE_KEY doivent être définis (voir .env.example)."
        )
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# === VÉRIFICATION DU JWT SUPABASE ===
def _decode_supabase_jwt(token: str) -> dict:
    """
    Décode et vérifie la signature d'un JWT émis par Supabase Auth (HS256).
    Ne jamais logger le token ni le secret de signature.
    """
    if not SUPABASE_JWT_SECRET:
        raise RuntimeError("SUPABASE_JWT_SECRET doit être défini (voir .env.example).")
    try:
        return jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except JWTError:
        # On ne remonte jamais le détail de l'erreur JWT (pourrait aider un attaquant).
        raise ValueError("Token invalide ou expiré.")


# === DÉPENDANCE FASTAPI : UTILISATEUR COURANT ===
def get_current_user(authorization: str = Header(..., description="Bearer <supabase_access_token>")) -> CurrentUser:
    """
    Dépendance à injecter sur toutes les routes protégées.
    Le frontend envoie le token de session Supabase (obtenu via supabase-js
    côté client) dans le header `Authorization: Bearer <token>`.

    Aucune authentification maison : Supabase Auth gère entièrement
    l'inscription, la connexion et l'expiration des sessions.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token manquant ou mal formé.")

    token = authorization.removeprefix("Bearer ").strip()

    try:
        payload = _decode_supabase_jwt(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Session invalide ou expirée.")

    return CurrentUser(
        id=payload["sub"],
        email=payload.get("email"),
        etsy_shop_connected=False,  # enrichi si besoin par l'endpoint /api/auth/me
    )
