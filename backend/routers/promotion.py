# =====================================================================
# === ROUTERS/PROMOTION.PY — Publication automatique Pinterest ===
# =====================================================================

import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException

from database import get_current_user, get_supabase
from models import CurrentUser, PinterestPublishRequest, PinterestPublishResponse

router = APIRouter()
logger = logging.getLogger("etsydrop.promotion")

PINTEREST_ACCESS_TOKEN = os.getenv("PINTEREST_ACCESS_TOKEN")
PINTEREST_PINS_URL = "https://api.pinterest.com/v5/pins"


# === PUBLICATION D'UN PIN ===
@router.post("/pinterest/publish", response_model=PinterestPublishResponse)
async def publish_to_pinterest(
    payload: PinterestPublishRequest,
    user: CurrentUser = Depends(get_current_user),
):
    if not PINTEREST_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Configuration Pinterest manquante côté serveur.")

    hashtags = " ".join(f"#{tag.replace(' ', '')}" for tag in payload.tags[:5])
    description = f"{payload.description} {hashtags}".strip()

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                PINTEREST_PINS_URL,
                headers={"Authorization": f"Bearer {PINTEREST_ACCESS_TOKEN}"},
                json={
                    "board_id": payload.board_id,
                    "title": payload.title[:100],
                    "description": description,
                    "link": str(payload.link),
                    "media_source": {
                        "source_type": "image_base64",
                        "content_type": "image/jpeg",
                        "data": payload.image_base64,
                    },
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("Pinterest indisponible : %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Échec de la publication Pinterest.")

    if response.status_code not in (200, 201):
        logger.warning("Pinterest a répondu %s", response.status_code)
        raise HTTPException(status_code=502, detail="Échec de la publication Pinterest.")

    pin_id = str(response.json().get("id") or "")
    if not pin_id:
        logger.warning("Réponse Pinterest sans id de pin.")
        raise HTTPException(status_code=502, detail="Réponse Pinterest invalide.")

    pin_url = f"https://www.pinterest.com/pin/{pin_id}/"

    # Colonne `external_id` (voir database_schema.sql > table promotions) :
    # le champ générique qui stocke pin_id / ad_id selon la plateforme.
    supabase = get_supabase()
    try:
        supabase.table("promotions").insert(
            {
                "user_id": user.id,
                "listing_id": payload.listing_id,
                "platform": "pinterest",
                "external_id": pin_id,
                "status": "published",
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()
    except Exception as exc:
        logger.warning("Échec de l'enregistrement de la promotion Pinterest : %s", type(exc).__name__)

    return PinterestPublishResponse(pin_id=pin_id, pin_url=pin_url, status="published")
