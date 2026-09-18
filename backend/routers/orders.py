# =====================================================================
# === ROUTERS/ORDERS.PY — Commandes & fulfillment ===
# =====================================================================
#
# Lecture des commandes synchronisées depuis Etsy et transmission au
# fournisseur choisi. L'appel réel aux APIs fournisseurs (Eprolo, CJ...)
# sera branché ici lors de l'étape "Fulfillment auto" du plan.

from typing import List

from fastapi import APIRouter, Depends, HTTPException

from database import get_current_user, get_supabase
from etsy_client import get_etsy_access_token, get_etsy_shop_id, sync_etsy_orders
from models import CurrentUser, Order, OrderFulfillRequest, SyncResult

router = APIRouter()


# === SYNCHRONISATION MANUELLE DEPUIS ETSY ===
@router.post("/sync", response_model=SyncResult)
async def sync_orders(user: CurrentUser = Depends(get_current_user)):
    """Redéclenche l'import des commandes payées depuis la boutique Etsy connectée."""
    access_token = await get_etsy_access_token(user.id)
    shop_id = get_etsy_shop_id(user.id)
    synced = await sync_etsy_orders(user.id, access_token, shop_id)
    return {"synced": synced, "shop_id": shop_id}


# === LISTE DES COMMANDES ===
@router.get("/", response_model=List[Order])
async def list_orders(user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("orders")
        .select("*")
        .eq("user_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


# === DÉTAIL D'UNE COMMANDE ===
@router.get("/{order_id}", response_model=Order)
async def get_order(order_id: str, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("orders")
        .select("*")
        .eq("id", order_id)
        .eq("user_id", user.id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Commande introuvable.")
    return result.data


# === TRANSMISSION AU FOURNISSEUR ===
@router.post("/{order_id}/fulfill", response_model=Order)
async def fulfill_order(
    order_id: str,
    payload: OrderFulfillRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """Marque la commande comme transmise au fournisseur choisi."""
    supabase = get_supabase()
    result = (
        supabase.table("orders")
        .update(
            {
                "status": "in_transit",
                "supplier": payload.supplier.value,
                "tracking_number": payload.tracking_number,
            }
        )
        .eq("id", order_id)
        .eq("user_id", user.id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Commande introuvable.")
    return result.data[0]
