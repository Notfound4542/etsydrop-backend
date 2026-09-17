# =====================================================================
# === ROUTERS/GENERATE.PY — Génération IA (mockups + fiches produit) ===
# =====================================================================
#
# Deux générateurs :
#  - POST /image   : mockup produit via Stability AI (SDXL text-to-image)
#  - POST /listing : fiche Etsy optimisée SEO via Claude, toujours en
#    anglais (voir CLAUDE.md > FICHES PRODUIT — RÈGLES INTERNATIONALES)
#
# Le prompt image interne n'est jamais renvoyé au client — c'est un actif
# IP du produit (voir GenerateImageResponse.prompt_used, toujours None).
# Chaque appel logge son coût dans Supabase "ai_costs" (jamais le contenu
# généré ni le prompt, uniquement le coût).

import json
import logging
import os

import httpx
from anthropic import AsyncAnthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from database import get_current_user, get_supabase
from models import (
    CurrentUser,
    GeneratedImage,
    GenerateImageRequest,
    GenerateImageResponse,
    GenerateListingRequest,
    GenerateListingResponse,
)

router = APIRouter()
logger = logging.getLogger("etsydrop.generate")

STABILITY_API_KEY = os.getenv("STABILITY_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

STABILITY_URL = "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image"
IMAGE_COST_EUR = 0.04
LISTING_COST_EUR = 0.001
LISTING_MODEL = "claude-3-haiku-20240307"

NEGATIVE_PROMPT = "text, watermark, logo, person, face, CGI, dark background, cluttered"


def _log_ai_cost(user_id: str, cost_type: str, cost_eur: float) -> None:
    """Enregistre uniquement le coût — jamais le prompt ni le contenu généré."""
    supabase = get_supabase()
    try:
        supabase.table("ai_costs").insert(
            {"user_id": user_id, "type": cost_type, "cost_eur": cost_eur}
        ).execute()
    except Exception as exc:
        logger.warning("Échec de l'enregistrement du coût IA (type=%s) : %s", cost_type, type(exc).__name__)


# === GÉNÉRATION D'IMAGE PRODUIT (Stability AI) ===
@router.post("/image", response_model=GenerateImageResponse)
async def generate_image(payload: GenerateImageRequest, user: CurrentUser = Depends(get_current_user)):
    if not STABILITY_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Stability AI manquante côté serveur.")

    style_suffix = f", {', '.join(payload.style_hints)}" if payload.style_hints else ""
    prompt = (
        f"Cozy lifestyle product photography of {payload.product_title}, {payload.product_category} style, "
        f"warm natural lighting, wooden surface, linen texture, soft bokeh background, "
        f"Etsy handmade aesthetic, pastel tones, 4K, editorial product shot{style_suffix}"
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                STABILITY_URL,
                headers={
                    "Authorization": f"Bearer {STABILITY_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "text_prompts": [
                        {"text": prompt, "weight": 1},
                        {"text": NEGATIVE_PROMPT, "weight": -1},
                    ],
                    "cfg_scale": 7,
                    "height": 1024,
                    "width": 1024,
                    "samples": 4,
                    "steps": 30,
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("Stability AI indisponible : %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Échec de la génération d'image.")

    if response.status_code != 200:
        logger.warning("Stability AI a répondu %s", response.status_code)
        raise HTTPException(status_code=502, detail="Échec de la génération d'image.")

    artifacts = response.json().get("artifacts", [])
    images = [
        GeneratedImage(index=i, base64=artifact["base64"], seed=artifact.get("seed", 0))
        for i, artifact in enumerate(artifacts)
    ]

    _log_ai_cost(user.id, "image", IMAGE_COST_EUR)

    # prompt_used=None : jamais exposé au client, même en cas de succès.
    return GenerateImageResponse(images=images, cost_eur=IMAGE_COST_EUR, prompt_used=None)


# === GÉNÉRATION DE FICHE ETSY OPTIMISÉE SEO (Claude) ===
@router.post("/listing", response_model=GenerateListingResponse)
async def generate_listing(payload: GenerateListingRequest, user: CurrentUser = Depends(get_current_user)):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Anthropic manquante côté serveur.")

    system = (
        "You are an expert Etsy SEO copywriter specializing in the US/UK/AU/CA market. "
        "Always write in English. Optimize for Etsy search algorithm and buyer psychology. "
        "Output must be valid JSON only, no explanation."
    )
    user_prompt = f"""Generate an optimized Etsy listing for this product:
Product: {payload.product_title}
Category: {payload.category}
Market: {payload.target_market}
Price: {payload.price_eur}€
Key features: {', '.join(payload.key_features)}

Return JSON:
{{
  "title": "string under 140 chars, starts with main keyword, includes buyer intent",
  "description": "string, 150-200 words, benefits-first, 3 paragraphs, ends with care instructions",
  "tags": ["exactly 13 tags", "each under 20 chars", "mix broad and long-tail", "buyer intent focused"],
  "lqs_score": integer 0-100,
  "lqs_details": {{
    "title_score": integer,
    "description_score": integer,
    "tags_score": integer,
    "improvements": ["max 3 short suggestions"]
  }}
}}"""

    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    try:
        message = await client.messages.create(
            model=LISTING_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:
        logger.warning("Anthropic API indisponible : %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Échec de la génération de fiche.")

    raw_text = "".join(block.text for block in message.content if block.type == "text")
    try:
        listing_data = json.loads(raw_text)
        listing = GenerateListingResponse(**listing_data)
    except (json.JSONDecodeError, TypeError, ValidationError):
        logger.warning("Réponse Claude invalide pour la génération de fiche.")
        raise HTTPException(status_code=502, detail="Réponse IA invalide.")

    _log_ai_cost(user.id, "listing", LISTING_COST_EUR)

    return listing
