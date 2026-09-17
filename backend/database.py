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

import base64
import json
import logging
import os
from functools import lru_cache

from dotenv import load_dotenv
from fastapi import Header, HTTPException
from supabase import Client, create_client

from models import CurrentUser

load_dotenv()
logger = logging.getLogger("etsydrop.database")

# === CONFIGURATION (jamais loggée, jamais renvoyée dans une réponse API) ===
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")


# === DÉTECTION DU TYPE DE CLÉ SUPABASE ===
# Le backend doit utiliser la clé service_role (contourne la Row Level
# Security) — jamais la clé anon/publishable, qui est censée être exposée
# côté client. Avec RLS activée (voir database_schema.sql) et la mauvaise
# clé, TOUS les inserts/updates échouent avec 42501 "new row violates row
# level security policy", sans que rien dans le code n'ait changé — un
# diagnostic qui coûte cher sans ce contrôle explicite au démarrage.
def _warn_if_not_service_role(key: str) -> None:
    if key.startswith("sb_secret_"):
        return  # nouveau format de clé Supabase, rôle service_role : OK
    if key.startswith("sb_publishable_"):
        logger.error(
            "SUPABASE_KEY ressemble à une clé PUBLISHABLE (%s…), pas à la clé "
            "service_role. Avec la Row Level Security activée, tous les "
            "inserts/updates du backend vont échouer (42501). Utilise la clé "
            "'service_role' de Project Settings > API sur Supabase.",
            key[:18],
        )
        return
    # Ancien format (JWT signé) : le payload contient un champ "role".
    try:
        parts = key.split(".")
        if len(parts) != 3:
            return
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        role = json.loads(base64.urlsafe_b64decode(padded)).get("role")
        if role and role != "service_role":
            logger.error(
                "SUPABASE_KEY a le rôle '%s', pas 'service_role'. Avec la Row "
                "Level Security activée, tous les inserts/updates du backend "
                "vont échouer (42501). Utilise la clé 'service_role' de "
                "Project Settings > API sur Supabase.",
                role,
            )
    except Exception:
        pass  # heuristique best-effort — ne doit jamais empêcher le démarrage


# === CLIENT SUPABASE (singleton) ===
@lru_cache
def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "SUPABASE_URL et SUPABASE_KEY doivent être définis (voir .env.example)."
        )
    _warn_if_not_service_role(SUPABASE_KEY)
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
