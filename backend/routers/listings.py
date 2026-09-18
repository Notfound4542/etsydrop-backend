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
from models import CurrentUser, Listing, ListingCreate, ListingPricingUpdate, SyncResult

router = APIRouter()


# === SYNCHRONISATION MANUELLE DEPUIS ETSY ===
@router.post("/sync", response_model=SyncResult)
async def sync_listings(user: CurrentUser = Depends(get_current_user)):
    """
    Redéclenche l'import des fiches actives (titre, tags, image principale,
    variantes/inventaire) depuis la boutique Etsy connectée, sans repasser
    par tout le flow OAuth. Le token est rafraîchi automatiquement s'il a
    expiré (voir etsy_client.py > get_etsy_access_token).
    """
    access_token = await get_etsy_access_token(user.id)
    shop_id = get_etsy_shop_id(user.id)
    result = await sync_etsy_listings(user.id, access_token, shop_id)
    return {**result, "shop_id": shop_id}


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


# === COÛT FOURNISSEUR & MARGE CIBLE (saisie manuelle) ===
@router.patch("/{listing_id}/pricing", response_model=Listing)
async def update_listing_pricing(
    listing_id: str,
    payload: ListingPricingUpdate,
    user: CurrentUser = Depends(get_current_user),
):
    """
    Etsy ne connaît pas le coût d'achat d'une fiche : il est saisi ici par
    l'utilisateur et sert au simulateur "Prix par pays" (frontend) à la
    place du placeholder. Une resync Etsy ne l'écrase jamais (voir
    etsy_client.py > sync_etsy_listings).
    """
    record = payload.model_dump(exclude_unset=True)
    if not record:
        raise HTTPException(status_code=422, detail="Aucun champ à mettre à jour.")
    supabase = get_supabase()
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
