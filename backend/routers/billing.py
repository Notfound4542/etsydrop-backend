# =====================================================================
# === ROUTERS/BILLING.PY — Abonnement Stripe (Free / Pro) ===
# =====================================================================
#
# Règles strictes (voir CLAUDE.md > CYBERSÉCURITÉ) :
#  - Le webhook vérifie TOUJOURS la signature Stripe avant de faire quoi
#    que ce soit avec le body — jamais de confiance aveugle.
#  - stripe.api_key est posé une seule fois au chargement du module.
#  - Aucun montant en dur : le prix vient de STRIPE_PRICE_ID (Dashboard).
#  - metadata.user_id (posé à la création du Checkout Session, propagé à
#    l'abonnement via subscription_data.metadata) est la seule source de
#    vérité pour identifier l'utilisateur dans le webhook.

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from database import get_current_user, get_supabase
from models import BillingCheckoutRequest, BillingCheckoutResponse, BillingStatusResponse, CurrentUser
from rate_limiter import limiter

router = APIRouter()
logger = logging.getLogger("etsydrop.billing")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5500")

# Instancié une seule fois au démarrage (jamais recréé dans une fonction).
# Le pattern global `stripe.api_key = ...` est déprécié par le SDK récent —
# voir stripe:stripe-best-practices > API keys.
stripe_client = stripe.StripeClient(api_key=STRIPE_SECRET_KEY) if STRIPE_SECRET_KEY else None


# === CRÉATION DE LA SESSION DE PAIEMENT ===
@router.post("/create-checkout", response_model=BillingCheckoutResponse)
async def create_checkout(payload: BillingCheckoutRequest, user: CurrentUser = Depends(get_current_user)):
    if not stripe_client or not STRIPE_PRICE_ID:
        raise HTTPException(status_code=500, detail="Configuration Stripe manquante côté serveur.")

    try:
        session = stripe_client.v1.checkout.sessions.create(
            {
                # payment_method_types volontairement omis : Stripe détermine
                # dynamiquement les moyens de paiement éligibles depuis le
                # Dashboard. Le coder en dur ("card") écarte les autres
                # moyens de paiement et dégrade la conversion.
                "line_items": [{"price": STRIPE_PRICE_ID, "quantity": 1}],
                "mode": "subscription",
                "success_url": f"{FRONTEND_URL}/dashboard?upgraded=true",
                "cancel_url": f"{FRONTEND_URL}/dashboard?cancelled=true",
                "customer_email": user.email,
                "metadata": {"user_id": str(user.id)},
                # Propage user_id sur l'objet Subscription lui-même : sans ça,
                # les events customer.subscription.* n'ont pas ces metadata
                # (Stripe ne les copie pas automatiquement depuis la Session).
                "subscription_data": {"metadata": {"user_id": str(user.id)}},
            }
        )
    except stripe.StripeError as exc:
        logger.warning("Stripe checkout indisponible : %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Échec de la création de la session de paiement.")

    return BillingCheckoutResponse(checkout_url=session.url)


# === WEBHOOK STRIPE (public — pas de get_current_user, exempté du rate limit) ===
@router.post("/webhook")
@limiter.exempt
async def stripe_webhook(request: Request, stripe_signature: Optional[str] = Header(None, alias="Stripe-Signature")):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Configuration webhook Stripe manquante côté serveur.")
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Signature Stripe manquante.")

    raw_body = await request.body()

    try:
        # Vérification statique (signature HMAC), indépendante de la clé API
        # secrète — fonctionne même sans stripe_client.
        event = stripe.Webhook.construct_event(raw_body, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        logger.warning("Signature Stripe invalide sur le webhook.")
        raise HTTPException(status_code=400, detail="Signature Stripe invalide.")

    event_type = event["type"]
    # StripeObject n'est plus un dict natif dans le SDK récent — .to_dict()
    # donne une structure sûre à parcourir avec .get().
    data_object = event["data"]["object"].to_dict()
    user_id = (data_object.get("metadata") or {}).get("user_id")

    supabase = get_supabase()

    if event_type in ("customer.subscription.created", "checkout.session.completed"):
        if user_id:
            try:
                supabase.table("profiles").update(
                    {"plan": "pro", "plan_started_at": datetime.now(timezone.utc).isoformat()}
                ).eq("id", user_id).execute()
            except Exception as exc:
                logger.warning("Échec de la mise à jour du plan (upgrade) : %s", type(exc).__name__)
        else:
            logger.warning("Event %s sans metadata.user_id — impossible d'identifier l'utilisateur.", event_type)

    elif event_type == "customer.subscription.deleted":
        if user_id:
            try:
                supabase.table("profiles").update({"plan": "free"}).eq("id", user_id).execute()
            except Exception as exc:
                logger.warning("Échec de la mise à jour du plan (downgrade) : %s", type(exc).__name__)
        else:
            logger.warning("Event %s sans metadata.user_id — impossible d'identifier l'utilisateur.", event_type)

    # Stripe exige un 200 immédiat, quel que soit le traitement interne.
    return {"received": True}


# === STATUT D'ABONNEMENT ===
@router.get("/status", response_model=BillingStatusResponse)
async def billing_status(user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("profiles")
        .select("plan, plan_started_at")
        .eq("id", user.id)
        .maybe_single()
        .execute()
    )
    data = result.data or {}
    return BillingStatusResponse(
        plan=data.get("plan", "free"),
        plan_started_at=data.get("plan_started_at"),
    )
