# =====================================================================
# === ROUTERS/LISTINGS.PY — Fiches produit (catalogue) ===
# =====================================================================
#
# CRUD des fiches produit de l'utilisateur connecté. Toutes les requêtes
# sont scoping sur `user_id` (jamais d'accès aux fiches d'un autre compte)
# et passent par le query builder Supabase (paramétré, pas de SQL brut).

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_current_user, get_supabase
from etsy_client import get_etsy_access_token, get_etsy_shop_id, sync_etsy_listings
from models import CurrentUser, Listing, ListingCreate, SyncResult

router = APIRouter()


# === SYNCHRONISATION MANUELLE DEPUIS ETSY ===
@router.post("/sync", response_model=SyncResult)
async def sync_listings(user: CurrentUser = Depends(get_current_user)):
    """
    Redéclenche l'import des fiches actives depuis la boutique Etsy connectée,
    sans repasser par tout le flow OAuth. Utile quand shop_id ou le token ont
    été corrigés directement en base (ex. correctif SQL) : etsy_callback est
    le SEUL autre endroit qui appelle sync_etsy_listings, donc un tel
    correctif ne déclenche jamais la sync tout seul.
    """
    access_token = get_etsy_access_token(user.id)
    shop_id = get_etsy_shop_id(user.id)
    synced = await sync_etsy_listings(user.id, access_token, shop_id)
    return {"synced": synced, "shop_id": shop_id}


# === LISTE DES FICHES ===
@router.get("/", response_model=List[Listing])
async def list_listings(
    stock_status: Optional[str] = Query(None, description="available | low_stock | out_of_stock"),
    user: CurrentUser = Depends(get_current_user),
):
    supabase = get_supabase()
    query = supabase.table("listings").select("*").eq("user_id", user.id)
    if stock_status:
        query = query.eq("stock_status", stock_status)
    result = query.execute()
    return result.data


# === DÉTAIL D'UNE FICHE ===
@router.get("/{listing_id}", response_model=Listing)
async def get_listing(listing_id: str, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("listings")
        .select("*")
        .eq("id", listing_id)
        .eq("user_id", user.id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Fiche introuvable.")
    return result.data


# === CRÉATION D'UNE FICHE ===
@router.post("/", response_model=Listing, status_code=201)
async def create_listing(payload: ListingCreate, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    record = payload.model_dump(mode="json")
    record["user_id"] = user.id
    result = supabase.table("listings").insert(record).execute()
    return result.data[0]


# === MISE À JOUR D'UNE FICHE ===
@router.put("/{listing_id}", response_model=Listing)
async def update_listing(listing_id: str, payload: ListingCreate, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    record = payload.model_dump(mode="json")
    result = (
        supabase.table("listings")
        .update(record)
        .eq("id", listing_id)
        .eq("user_id", user.id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Fiche introuvable.")
    return result.data[0]


# === SUPPRESSION D'UNE FICHE ===
@router.delete("/{listing_id}", status_code=204)
async def delete_listing(listing_id: str, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    supabase.table("listings").delete().eq("id", listing_id).eq("user_id", user.id).execute()
    return None
