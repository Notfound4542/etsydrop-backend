# =====================================================================
# === ROUTERS/AUTH.PY — Connexion boutique Etsy (OAuth 2.0 + PKCE) ===
# =====================================================================
#
# Important : ceci gère UNIQUEMENT la connexion de la boutique Etsy du
# vendeur (pour lire ses commandes/fiches). L'authentification des
# utilisateurs d'EtsyDrop (inscription/connexion) est gérée entièrement
# par Supabase Auth côté frontend — voir database.get_current_user().
#
# Flow PKCE (RFC 7636), requis par l'API Etsy v3 :
#   1. GET /api/auth/etsy/login     -> génère code_verifier/code_challenge,
#                                       renvoie l'URL d'autorisation Etsy.
#   2. L'utilisateur autorise sur etsy.com, Etsy redirige le NAVIGATEUR (GET,
#      pas de header Authorization possible) vers ETSY_REDIRECT_URI?code&state.
#   3. GET /api/auth/etsy/callback  -> échange le code contre les tokens,
#      stockés en base, puis redirige le navigateur vers FRONTEND_URL.
#      L'utilisateur est identifié via le state (posé à l'étape 1), pas via
#      get_current_user : une redirection navigateur ne porte aucun header.

import base64
import hashlib
import logging
import os
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from database import get_current_user, get_supabase
from models import CurrentUser, EtsyOAuthCallback, EtsyOAuthLoginResponse

router = APIRouter()
logger = logging.getLogger("etsydrop.auth")

# === CONFIGURATION (jamais loggée) ===
ETSY_API_KEY = os.getenv("ETSY_API_KEY")
ETSY_API_SECRET = os.getenv("ETSY_API_SECRET")
ETSY_REDIRECT_URI = os.getenv("ETSY_REDIRECT_URI", "http://localhost:8000/api/auth/etsy/callback")
# URL du frontend vers laquelle renvoyer le navigateur une fois l'échange terminé
# (même variable que celle utilisée pour les redirections Stripe — routers/billing.py).
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5500")

ETSY_AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
ETSY_SCOPES = "listings_r listings_w shops_r transactions_r"

# Stockage temporaire state -> {code_verifier, user_id} le temps du flow OAuth
# (quelques minutes). Le user_id est capturé ici, à l'étape où l'on a encore le
# header Authorization du frontend — la redirection Etsy qui suit n'en aura plus.
# ⚠️ En production : remplacer ce dict en mémoire par une table Supabase
# (colonnes state, code_verifier, user_id, expires_at) — un dict en mémoire ne
# survit pas à un redémarrage et n'est pas partagé entre plusieurs workers/instances
# Railway.
_pkce_store: dict[str, dict] = {}


# === PKCE — GÉNÉRATION DU COUPLE VERIFIER / CHALLENGE (S256) ===
def _generate_pkce_pair() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# === DÉMARRAGE DU FLOW OAUTH ETSY ===
@router.get("/etsy/login", response_model=EtsyOAuthLoginResponse)
async def etsy_login(user: CurrentUser = Depends(get_current_user)):
    """
    Génère l'URL d'autorisation Etsy (avec PKCE) vers laquelle le frontend
    doit rediriger l'utilisateur déjà connecté à EtsyDrop (via Supabase Auth).
    """
    if not ETSY_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Etsy manquante côté serveur.")

    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    _pkce_store[state] = {"code_verifier": code_verifier, "user_id": user.id}

    params = {
        "response_type": "code",
        "client_id": ETSY_API_KEY,
        "redirect_uri": ETSY_REDIRECT_URI,
        "scope": ETSY_SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = str(httpx.URL(ETSY_AUTHORIZE_URL, params=params))
    return EtsyOAuthLoginResponse(authorize_url=authorize_url, state=state)


# === ÉCHANGE DU CODE CONTRE LES TOKENS ETSY ===
@router.get("/etsy/callback")
async def etsy_callback(
    code: str = Query(..., min_length=10, max_length=512),
    state: str = Query(..., min_length=10, max_length=128),
):
    """
    Cible de la redirection Etsy (GET, code+state en query string — jamais de
    JSON body ni de header Authorization sur une navigation navigateur).
    Échange le code contre un access/refresh token, les stocke en base
    Supabase (jamais renvoyés au frontend ni posés en localStorage), puis
    renvoie le navigateur vers FRONTEND_URL.
    """
    validated = EtsyOAuthCallback(code=code, state=state)
    entry = _pkce_store.pop(validated.state, None)
    if not entry:
        logger.warning("Callback Etsy avec un state invalide ou expiré.")
        return RedirectResponse(f"{FRONTEND_URL}/?etsy_error=invalid_state")

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            ETSY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": ETSY_API_KEY,
                "redirect_uri": ETSY_REDIRECT_URI,
                "code": validated.code,
                "code_verifier": entry["code_verifier"],
            },
        )

    if response.status_code != 200:
        # On ne renvoie jamais le corps de la réponse Etsy telle quelle au client.
        logger.warning("Échange du token Etsy échoué : %s", response.status_code)
        return RedirectResponse(f"{FRONTEND_URL}/?etsy_error=token_exchange_failed")

    tokens = response.json()

    supabase = get_supabase()
    supabase.table("etsy_tokens").upsert(
        {
            "user_id": entry["user_id"],
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_in": tokens.get("expires_in"),
        }
    ).execute()

    return RedirectResponse(f"{FRONTEND_URL}/?etsy_connected=true")


# === DÉCONNEXION DE LA BOUTIQUE ETSY ===
@router.delete("/etsy/disconnect")
async def etsy_disconnect(user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    supabase.table("etsy_tokens").delete().eq("user_id", user.id).execute()
    return {"connected": False}


# === UTILISATEUR COURANT (Supabase Auth) ===
@router.get("/me", response_model=CurrentUser)
async def get_me(user: CurrentUser = Depends(get_current_user)):
    """Retourne l'utilisateur courant, enrichi du statut de connexion Etsy."""
    supabase = get_supabase()
    result = supabase.table("etsy_tokens").select("user_id").eq("user_id", user.id).execute()
    user.etsy_shop_connected = bool(result.data)
    return user
