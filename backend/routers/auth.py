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
from etsy_client import etsy_get, fetch_public, get_etsy_access_token, sync_etsy_listings, sync_etsy_orders
from models import CurrentUser, EtsyDebugStatus, EtsyOAuthCallback, EtsyOAuthLoginResponse

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
    GET /shops/{shop_id} attend un ID NUMÉRIQUE dans le path — Etsy renvoie
    400 "Expected int value for shop_id (got string)" si on y met un nom
    (confirmé en prod via test_etsy.py). La recherche par nom se fait via
    GET /shops?shop_name=... (findShops, query param), qui renvoie
    {count, results: [...]} — même mécanisme public (x-api-key seulement,
    pas soumis au tier OAuth) que routers/shop_analyzer.py. Retourne None
    sans lever si la boutique n'est pas trouvée : la connexion ne doit pas
    échouer pour ça, seule la sync des fiches/du CA sera indisponible.
    """
    try:
        payload = await etsy_get("/shops", params={"shop_name": shop_name})
        results = payload.get("results", []) if isinstance(payload, dict) else []
        resolved = results[0].get("shop_id") if results else None
        if resolved:
            logger.info("Shop résolu : shop_name=%s -> shop_id=%s", shop_name, resolved)
        else:
            logger.warning("Aucune boutique Etsy trouvée pour shop_name=%s.", shop_name)
        return resolved
    except Exception as exc:
        logger.warning("Résolution shop_id échouée pour shop_name=%s : %s: %s", shop_name, type(exc).__name__, exc)
        return None


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

    logger.info("etsy_callback appelé pour user_id=%s (shop_name=%r).", entry["user_id"], entry.get("shop_name"))

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
        # Sans ce fallback, une reconnexion où la résolution échoue (rate
        # limit Etsy transitoire, panne momentanée, etc.) écraserait un
        # shop_id déjà résolu (par une connexion précédente, ou par un
        # correctif SQL manuel) avec NULL via l'upsert ci-dessous — la
        # boutique redeviendrait "non résolue" sans qu'aucune erreur ne le
        # signale, et toute sync ultérieure échouerait pour une raison
        # totalement différente de celle qui a déclenché l'échec initial.
        try:
            existing = (
                get_supabase()
                .table("etsy_tokens")
                .select("shop_id")
                .eq("user_id", entry["user_id"])
                .maybe_single()
                .execute()
            )
            shop_id = existing.data.get("shop_id") if existing.data else None
        except Exception:
            shop_id = None
        logger.warning(
            "shop_id non résolu pour user_id=%s (shop_name=%r) — conservation de la valeur déjà en base (%s).",
            entry["user_id"], shop_name, shop_id,
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

    logger.info("Token Etsy enregistré pour user_id=%s (shop_id=%s).", entry["user_id"], shop_id)

    synced = await sync_etsy_listings(entry["user_id"], tokens["access_token"], shop_id)
    synced_orders = await sync_etsy_orders(entry["user_id"], tokens["access_token"], shop_id)
    logger.info(
        "Connexion Etsy réussie pour user_id=%s : %d fiches importées (%d avec image, %d avec variantes), %d commandes importées.",
        entry["user_id"], synced["synced"], synced["with_image"], synced["with_variants"], synced_orders,
    )

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
    result = supabase.table("etsy_tokens").select("user_id,shop_name").eq("user_id", user.id).execute()
    user.etsy_shop_connected = bool(result.data)
    user.etsy_shop_name = result.data[0].get("shop_name") if result.data else None
    return user


# === DIAGNOSTIC DE LA CONNEXION ETSY ===
@router.get("/etsy/debug", response_model=EtsyDebugStatus)
async def etsy_debug(
    probe_oauth: bool = Query(True, description="Sonde un GET authentifié (déclenche le refresh préventif si le token expire)"),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Explique POURQUOI une sync peut renvoyer 0 : shop_id présent ? token en
    base ? encore valide (updated_at + expires_in) ? API publique OK ? OAuth
    OK ? Combien de fiches/commandes en DB ? Aucun secret n'est renvoyé (les
    tokens ne quittent jamais le backend — seuls des booléens sortent).
    """
    from datetime import datetime, timedelta, timezone

    supabase = get_supabase()
    row = (
        supabase.table("etsy_tokens")
        .select("shop_id,shop_name,access_token,refresh_token,expires_in,updated_at")
        .eq("user_id", user.id)
        .maybe_single()
        .execute()
    )
    data = row.data if (row and row.data) else None
    status = EtsyDebugStatus(connected=bool(data))
    if not data:
        status.problems.append("Aucune ligne etsy_tokens pour ce compte — boutique jamais connectée (ou déconnectée).")
    else:
        status.shop_id = data.get("shop_id")
        status.shop_name = data.get("shop_name")
        status.has_access_token = bool(data.get("access_token"))
        status.has_refresh_token = bool(data.get("refresh_token"))
        if not status.shop_id:
            status.problems.append("shop_id NULL — reconnecte la boutique en indiquant son nom exact.")
        if not status.has_access_token:
            status.problems.append("access_token NULL en base.")
        if not status.has_refresh_token:
            status.problems.append("refresh_token NULL — le token ne pourra pas être rafraîchi après 1 h.")
        try:
            updated_at = datetime.fromisoformat(str(data.get("updated_at")).replace("Z", "+00:00"))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            expires_at = updated_at + timedelta(seconds=int(data.get("expires_in") or 3600))
            status.expires_at = expires_at
            remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())
            status.seconds_remaining = remaining
            status.token_valid = remaining > 0 and status.has_access_token
            if remaining <= 0:
                status.problems.append("access_token expiré (durée de vie Etsy : 1 h) — un refresh automatique est tenté à chaque appel authentifié.")
        except (TypeError, ValueError):
            status.problems.append("updated_at illisible — impossible de dater le token.")

    # Compteurs DB (scopés user)
    for table, attr in (("listings", "listings_in_db"), ("orders", "orders_in_db")):
        try:
            res = supabase.table(table).select("id", count="exact").eq("user_id", user.id).execute()
            setattr(status, attr, int(res.count or 0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Comptage %s échoué pour user_id=%s : %s", table, user.id, exc)

    # API publique (clé d'app seule) — indépendante du token OAuth
    if status.shop_id:
        try:
            payload = await fetch_public(f"/shops/{status.shop_id}/listings/active", params={"limit": 1})
            status.public_api_ok = True
            status.etsy_active_listings = int(payload.get("count") or 0) if isinstance(payload, dict) else None
            if status.etsy_active_listings == 0:
                status.problems.append("Etsy ne renvoie aucune fiche ACTIVE pour ce shop_id (brouillons/inactives ?).")
        except HTTPException as exc:
            status.problems.append(f"API publique Etsy en échec ({exc.detail}) — clés ETSY_API_KEY/SECRET ou shop_id invalides.")

    # Sonde OAuth (refresh préventif inclus) — un GET léger authentifié
    if probe_oauth and data and status.has_access_token:
        try:
            token = await get_etsy_access_token(user.id)
            await etsy_get(f"/shops/{status.shop_id}/listings/active" if status.shop_id else "/openapi-ping", access_token=token, params={"limit": 1})
            status.oauth_probe_ok = True
            status.token_valid = True
        except HTTPException as exc:
            status.oauth_probe_ok = False
            status.problems.append(f"Sonde OAuth en échec : {exc.detail}")

    return status
