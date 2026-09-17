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
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from database import get_current_user, get_supabase
from etsy_client import etsy_get
from models import CurrentUser, EtsyOAuthCallback, EtsyOAuthLoginResponse

router = APIRouter()
logger = logging.getLogger("etsydrop.auth")

# === CONFIGURATION (jamais loggée) ===
# .strip().strip("\"'") : Railway a déjà causé un bug identique sur les
# variables CORS (guillemets collés en copiant-collant la valeur dans le
# dashboard — voir le commit "Update CORS settings for GitHub Pages" sur
# main.py). Même traitement défensif ici pour ne pas répéter l'incident.
def _clean_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip("\"'")


ETSY_API_KEY = _clean_env("ETSY_API_KEY") or None
ETSY_API_SECRET = _clean_env("ETSY_API_SECRET") or None
ETSY_REDIRECT_URI = _clean_env("ETSY_REDIRECT_URI", "http://localhost:8000/api/auth/etsy/callback")
# URL du frontend vers laquelle renvoyer le navigateur une fois l'échange terminé
# (même variable que celle utilisée pour les redirections Stripe — routers/billing.py).
FRONTEND_URL = _clean_env("FRONTEND_URL", "http://localhost:5500")

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
async def etsy_login(
    shop_name: str = Query(..., min_length=1, max_length=80, description="Nom exact de la boutique Etsy à connecter"),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Génère l'URL d'autorisation Etsy (avec PKCE) vers laquelle le frontend
    doit rediriger l'utilisateur déjà connecté à EtsyDrop (via Supabase Auth).

    shop_name est demandé ici, avant même de partir sur Etsy : GET
    /users/{user_id}/shops (qui aurait permis de le déduire automatiquement
    après coup) renvoie 403 au tier Etsy actuel de cette app. Le nom saisi
    permet de résoudre shop_id après coup via l'endpoint public
    /shops/{shop_name} — voir etsy_callback.
    """
    if not ETSY_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Etsy manquante côté serveur.")

    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    _pkce_store[state] = {"code_verifier": code_verifier, "user_id": user.id, "shop_name": shop_name}

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


# === RÉSOLUTION DU SHOP_ID (endpoint public, pas soumis au tier OAuth) ===
async def _resolve_shop_id(shop_name: str) -> Optional[int]:
    """
    GET /shops/{shop_name} est un endpoint public (x-api-key seulement,
    même mécanisme que routers/shop_analyzer.py) — contrairement à
    GET /users/{user_id}/shops, il n'est pas bloqué par le tier Etsy actuel
    de cette app (confirmé 403 en prod). Retourne None sans lever si la
    boutique n'est pas trouvée : la connexion ne doit pas échouer pour ça,
    seule la sync des fiches/du CA sera indisponible.
    """
    try:
        shop = await etsy_get(f"/shops/{shop_name}")
        resolved = shop.get("shop_id") if isinstance(shop, dict) else None
        if resolved:
            logger.info("Shop résolu : shop_name=%s -> shop_id=%s", shop_name, resolved)
        return resolved
    except Exception as exc:
        logger.warning("Résolution shop_id échouée pour shop_name=%s : %s: %s", shop_name, type(exc).__name__, exc)
        return None


# === IMPORT INITIAL DES FICHES DE LA BOUTIQUE (déclenché juste après connexion) ===
async def _sync_etsy_listings(user_id: str, access_token: str, shop_id: Optional[int]) -> int:
    """
    Importe les fiches actives de la boutique Etsy connectée dans la table
    `listings`, pour que le Catalogue affiche autre chose qu'une table vide
    juste après le callback OAuth. Upsert sur (user_id, etsy_listing_id) —
    voir l'index unique dans database_schema.sql — donc une resynchronisation
    met à jour les fiches déjà importées au lieu de les dupliquer.

    Ne doit JAMAIS faire échouer le callback OAuth : le token doit être
    sauvegardé et l'utilisateur redirigé même si cet import échoue (shop_id
    non résolu, boutique vide, rate limit Etsy, etc.) — toute erreur est
    donc loguée et avalée.
    """
    if not shop_id:
        logger.warning("Sync listings Etsy ignorée pour user_id=%s : shop_id non résolu.", user_id)
        return 0

    try:
        payload = await etsy_get(
            f"/shops/{shop_id}/listings/active", access_token=access_token, params={"limit": 100}
        )
        etsy_listings = payload.get("results", []) if isinstance(payload, dict) else []
    except Exception as exc:
        logger.warning("Sync listings Etsy échouée pour user_id=%s : %s: %s", user_id, type(exc).__name__, exc)
        return 0

    # Les fiches importées doivent rester compatibles avec le modèle Listing
    # (voir models.py) — sans ça, la lecture ultérieure via GET /api/listings/
    # plante en ResponseValidationError (500 générique) au lieu de renvoyer
    # les données. D'où les tailles/valeurs par défaut ci-dessous.
    rows = []
    for item in etsy_listings:
        try:
            listing_id = str(item.get("listing_id") or "")
            if not listing_id:
                continue

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

            rows.append(
                {
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
            )
        except (TypeError, ValueError):
            continue

    if not rows:
        return 0

    try:
        get_supabase().table("listings").upsert(rows, on_conflict="user_id,etsy_listing_id").execute()
    except Exception as exc:
        logger.warning("Écriture des listings Etsy échouée pour user_id=%s : %s: %s", user_id, type(exc).__name__, exc)
        return 0

    return len(rows)


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
    shop_name = entry.get("shop_name", "")
    shop_id = await _resolve_shop_id(shop_name) if shop_name else None
    if not shop_id:
        logger.warning(
            "shop_id non résolu pour user_id=%s (shop_name=%r) — le token est quand même sauvegardé.",
            entry["user_id"], shop_name,
        )

    # Jamais laissé remonter tel quel : une exception ici (ex. colonnes
    # shop_id/shop_name pas encore migrées en base — voir database_schema.sql)
    # ferait planter tout le callback en 500 brut, juste après que
    # l'utilisateur ait validé le consentement côté Etsy, sans aucune
    # redirection — la pire UX possible à ce stade précis du flow.
    try:
        get_supabase().table("etsy_tokens").upsert(
            {
                "user_id": entry["user_id"],
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "expires_in": tokens.get("expires_in"),
                "shop_id": shop_id,
                "shop_name": shop_name,
            }
        ).execute()
    except Exception as exc:
        logger.error(
            "Échec de l'enregistrement du token Etsy pour user_id=%s : %s: %s",
            entry["user_id"], type(exc).__name__, exc,
            exc_info=True,
        )
        return RedirectResponse(f"{FRONTEND_URL}/?etsy_error=token_save_failed")

    synced = await _sync_etsy_listings(entry["user_id"], tokens["access_token"], shop_id)
    logger.info("Connexion Etsy réussie pour user_id=%s : %d fiches importées.", entry["user_id"], synced)

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
