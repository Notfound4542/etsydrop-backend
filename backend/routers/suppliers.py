# =====================================================================
# === ROUTERS/SUPPLIERS.PY — Fournisseurs (Eprolo + générique) ===
# =====================================================================
#
# Gestion des fournisseurs de l'utilisateur, de leur catalogue produits
# (saisie manuelle ou import API), du lien produit fournisseur ↔ fiche Etsy
# et des conversations (questions / devis).
#
# Sécurité (CLAUDE.md > CYBERSÉCURITÉ) :
#   - api_key / api_secret sont écrits en DB et JAMAIS renvoyés : toutes les
#     lectures passent par _SUPPLIER_PUBLIC_COLUMNS (les colonnes secrètes ne
#     sont même pas sélectionnées) et le modèle Supplier n'expose que
#     has_api_key.
#   - Tout est scopé sur user_id (jamais d'accès aux données d'un autre compte).
#   - Query builder PostgREST uniquement — requêtes paramétrées par construction.

import asyncio
import logging
import os
import smtplib
from email.message import EmailMessage
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_current_user, get_supabase
from models import (
    CurrentUser,
    Supplier,
    SupplierConversation,
    SupplierConversationCreate,
    SupplierConversationUpdate,
    SupplierCreate,
    SupplierImportResult,
    SupplierProduct,
    SupplierProductCreate,
    SupplierProductLink,
    SupplierUpdate,
)

router = APIRouter()
logger = logging.getLogger("etsydrop.suppliers")

# Colonnes renvoyées au client — api_key / api_secret volontairement absentes.
_SUPPLIER_PUBLIC_COLUMNS = "id,name,platform,contact_email,notes,created_at"

# === SMTP (optionnel) — envoi réel des questions au fournisseur ===
# Sans configuration, le message est simplement stocké (email_sent=false).
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip() or SMTP_USER

# === EPROLO (import catalogue) ===
# Eprolo fournit son API au cas par cas : l'URL de base est configurable
# pour ne pas avoir à redéployer quand ils communiquent l'endpoint définitif.
EPROLO_API_BASE = (os.getenv("EPROLO_API_BASE", "").strip() or "https://open.eprolo.com/api").rstrip("/")


# === HELPERS ===
def _get_supplier_row(user_id: str, supplier_id: str, columns: str = _SUPPLIER_PUBLIC_COLUMNS) -> dict:
    supabase = get_supabase()
    result = (
        supabase.table("suppliers")
        .select(columns)
        .eq("id", supplier_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise HTTPException(status_code=404, detail="Fournisseur introuvable.")
    return result.data


def _to_public_supplier(row: dict, has_api_key: bool, products_count: int = 0) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "platform": row.get("platform") or "autre",
        "contact_email": row.get("contact_email"),
        "notes": row.get("notes"),
        "has_api_key": has_api_key,
        "products_count": products_count,
        "created_at": row["created_at"],
    }


def _products_count_by_supplier(user_id: str) -> dict:
    supabase = get_supabase()
    result = supabase.table("supplier_products").select("supplier_id").eq("user_id", user_id).execute()
    counts: dict = {}
    for row in result.data or []:
        counts[row["supplier_id"]] = counts.get(row["supplier_id"], 0) + 1
    return counts


# === LISTE DES FOURNISSEURS ===
@router.get("/", response_model=List[Supplier])
async def list_suppliers(user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    # On sélectionne api_key uniquement pour dériver has_api_key : la valeur
    # n'est jamais copiée dans la réponse (voir _to_public_supplier).
    result = (
        supabase.table("suppliers")
        .select(_SUPPLIER_PUBLIC_COLUMNS + ",api_key")
        .eq("user_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )
    counts = _products_count_by_supplier(user.id)
    return [
        _to_public_supplier(row, bool(row.get("api_key")), counts.get(row["id"], 0))
        for row in (result.data or [])
    ]


# === AJOUT D'UN FOURNISSEUR ===
@router.post("/", response_model=Supplier, status_code=201)
async def create_supplier(payload: SupplierCreate, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    record = payload.model_dump(mode="json")
    record["user_id"] = user.id
    result = supabase.table("suppliers").insert(record).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Création du fournisseur impossible.")
    row = result.data[0]
    return _to_public_supplier(row, bool(payload.api_key), 0)


# === MISE À JOUR D'UN FOURNISSEUR ===
@router.patch("/{supplier_id}", response_model=Supplier)
async def update_supplier(supplier_id: str, payload: SupplierUpdate, user: CurrentUser = Depends(get_current_user)):
    record = payload.model_dump(mode="json", exclude_unset=True)
    if not record:
        raise HTTPException(status_code=422, detail="Aucun champ à mettre à jour.")
    supabase = get_supabase()
    result = (
        supabase.table("suppliers")
        .update(record)
        .eq("id", supplier_id)
        .eq("user_id", user.id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Fournisseur introuvable.")
    row = result.data[0]
    counts = _products_count_by_supplier(user.id)
    return _to_public_supplier(row, bool(row.get("api_key")), counts.get(supplier_id, 0))


# === SUPPRESSION D'UN FOURNISSEUR (cascade produits + conversations) ===
@router.delete("/{supplier_id}", status_code=204)
async def delete_supplier(supplier_id: str, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    supabase.table("suppliers").delete().eq("id", supplier_id).eq("user_id", user.id).execute()
    return None


# === CONVERSATIONS (déclarées AVANT /{supplier_id}/products pour ne pas être
#     masquées par le paramètre de chemin générique) ===
@router.get("/conversations/", response_model=List[SupplierConversation])
async def list_conversations(
    supplier_id: Optional[str] = Query(None, min_length=36, max_length=36),
    user: CurrentUser = Depends(get_current_user),
):
    supabase = get_supabase()
    query = supabase.table("supplier_conversations").select("*").eq("user_id", user.id)
    if supplier_id:
        query = query.eq("supplier_id", supplier_id)
    result = query.order("created_at", desc=True).limit(200).execute()
    return result.data or []


def _send_email_sync(to_email: str, subject: str, body: str, reply_to: Optional[str]) -> bool:
    """Envoi SMTP bloquant — appelé via asyncio.to_thread. Jamais de secret loggé."""
    if not SMTP_HOST or not SMTP_FROM:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.starttls()
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("Envoi SMTP au fournisseur échoué : %s", type(exc).__name__)
        return False


@router.post("/conversations/", response_model=SupplierConversation, status_code=201)
async def create_conversation(payload: SupplierConversationCreate, user: CurrentUser = Depends(get_current_user)):
    """
    Stocke le message en DB. Si le fournisseur a un contact_email ET que le
    SMTP est configuré (SMTP_HOST/SMTP_FROM), l'email part réellement ;
    sinon le message reste tracé (email_sent=false) — à copier/coller.
    """
    supplier = _get_supplier_row(user.id, payload.supplier_id)

    if payload.supplier_product_id:
        supabase = get_supabase()
        product = (
            supabase.table("supplier_products")
            .select("id")
            .eq("id", payload.supplier_product_id)
            .eq("user_id", user.id)
            .maybe_single()
            .execute()
        )
        if not product or not product.data:
            raise HTTPException(status_code=404, detail="Produit fournisseur introuvable.")

    email_sent = False
    if payload.direction == "sent" and supplier.get("contact_email"):
        email_sent = await asyncio.to_thread(
            _send_email_sync,
            supplier["contact_email"],
            f"[EtsyDrop] {payload.subject}",
            payload.message,
            user.email,
        )

    record = payload.model_dump(mode="json")
    record["user_id"] = user.id
    record["email_sent"] = email_sent
    # Un message reçu du fournisseur = la question est répondue.
    record["status"] = "answered" if payload.direction == "received" else "open"
    supabase = get_supabase()
    result = supabase.table("supplier_conversations").insert(record).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Enregistrement du message impossible.")
    return result.data[0]


@router.patch("/conversations/{conversation_id}", response_model=SupplierConversation)
async def update_conversation(
    conversation_id: str,
    payload: SupplierConversationUpdate,
    user: CurrentUser = Depends(get_current_user),
):
    supabase = get_supabase()
    result = (
        supabase.table("supplier_conversations")
        .update({"status": payload.status.value})
        .eq("id", conversation_id)
        .eq("user_id", user.id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Conversation introuvable.")
    return result.data[0]


# === LIER UN PRODUIT FOURNISSEUR À UNE FICHE ETSY ===
# Déclaré avant /{supplier_id}/products pour la même raison de routage.
@router.patch("/products/{product_id}/link", response_model=SupplierProduct)
async def link_product(product_id: str, payload: SupplierProductLink, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    if payload.linked_etsy_listing_id is not None:
        # La fiche doit exister dans le catalogue de l'utilisateur.
        listing = (
            supabase.table("listings")
            .select("id,cost_price")
            .eq("user_id", user.id)
            .eq("etsy_listing_id", str(payload.linked_etsy_listing_id))
            .maybe_single()
            .execute()
        )
        if not listing or not listing.data:
            raise HTTPException(status_code=404, detail="Fiche Etsy introuvable dans ton catalogue.")

    result = (
        supabase.table("supplier_products")
        .update({"linked_etsy_listing_id": payload.linked_etsy_listing_id})
        .eq("id", product_id)
        .eq("user_id", user.id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Produit fournisseur introuvable.")
    product = result.data[0]

    # Bonus utile : si la fiche n'a pas encore de coût fournisseur, on le
    # pré-remplit avec le prix de base du produit lié (l'utilisateur peut
    # toujours l'ajuster dans la fiche). Jamais d'écrasement d'une valeur saisie.
    if payload.linked_etsy_listing_id is not None and listing.data.get("cost_price") is None:
        try:
            supabase.table("listings").update({"cost_price": product.get("base_price")}).eq("id", listing.data["id"]).execute()
        except Exception as exc:
            logger.warning("Pré-remplissage cost_price échoué : %s", type(exc).__name__)
    return product


# === PRODUITS D'UN FOURNISSEUR ===
@router.get("/{supplier_id}/products", response_model=List[SupplierProduct])
async def list_supplier_products(supplier_id: str, user: CurrentUser = Depends(get_current_user)):
    _get_supplier_row(user.id, supplier_id)
    supabase = get_supabase()
    result = (
        supabase.table("supplier_products")
        .select("*")
        .eq("user_id", user.id)
        .eq("supplier_id", supplier_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


@router.post("/{supplier_id}/products", response_model=SupplierProduct, status_code=201)
async def create_supplier_product(
    supplier_id: str,
    payload: SupplierProductCreate,
    user: CurrentUser = Depends(get_current_user),
):
    _get_supplier_row(user.id, supplier_id)
    record = payload.model_dump(mode="json")
    record["user_id"] = user.id
    record["supplier_id"] = supplier_id
    supabase = get_supabase()
    result = supabase.table("supplier_products").insert(record).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Création du produit impossible.")
    return result.data[0]


@router.delete("/{supplier_id}/products/{product_id}", status_code=204)
async def delete_supplier_product(supplier_id: str, product_id: str, user: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    (
        supabase.table("supplier_products")
        .delete()
        .eq("id", product_id)
        .eq("supplier_id", supplier_id)
        .eq("user_id", user.id)
        .execute()
    )
    return None


# === IMPORT AUTOMATIQUE DU CATALOGUE (API fournisseur) ===
async def _import_eprolo(api_key: str, api_secret: Optional[str]) -> List[dict]:
    """
    Appelle l'API Eprolo avec la clé DU FOURNISSEUR (stockée en DB, jamais
    celle du .env global). Le schéma exact de leur réponse n'est pas encore
    figé (API fournie au cas par cas) : le mapping est défensif, chaque champ
    est optionnel et un item illisible est ignoré sans bloquer l'import.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    if api_secret:
        headers["X-Api-Secret"] = api_secret
    products: List[dict] = []
    async with httpx.AsyncClient(timeout=20) as client:
        page = 1
        while page <= 10:  # garde-fou : 10 pages max par import
            response = await client.get(
                f"{EPROLO_API_BASE}/product/list",
                headers=headers,
                params={"page": page, "pageSize": 100},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            items = data.get("list") or data.get("products") or (payload if isinstance(payload, list) else [])
            if not items:
                break
            for item in items:
                try:
                    ext_id = str(item.get("productId") or item.get("id") or item.get("sku") or "")
                    if not ext_id:
                        continue
                    images = item.get("images") or item.get("imageList") or []
                    if isinstance(images, str):
                        images = [images]
                    variants = []
                    for v in item.get("variants") or item.get("skuList") or []:
                        variants.append(
                            {
                                "color": (v.get("color") or v.get("attr1") or None),
                                "size": (v.get("size") or v.get("attr2") or None),
                                "stock": int(v.get("stock") or v.get("inventory") or 0),
                                "price": float(v.get("price") or v.get("sellPrice") or 0),
                            }
                        )
                    products.append(
                        {
                            "supplier_product_id": ext_id[:120],
                            "name": str(item.get("productName") or item.get("title") or item.get("name") or "Produit")[:200],
                            "description": str(item.get("description") or "")[:3000] or None,
                            "base_price": float(item.get("price") or item.get("sellPrice") or 0),
                            "currency": str(item.get("currency") or "USD")[:3].upper(),
                            "weight_grams": int(item.get("weight") or 0) or None,
                            "dimensions_cm": {
                                "length": item.get("length"),
                                "width": item.get("width"),
                                "height": item.get("height"),
                            },
                            "variants": variants[:200],
                            "images": [str(u) for u in images[:20] if u],
                            "moq": int(item.get("moq") or 1) or 1,
                            "lead_time_days": int(item.get("shippingDays") or item.get("leadTime") or 0) or None,
                        }
                    )
                except (TypeError, ValueError):
                    continue
            if len(items) < 100:
                break
            page += 1
    return products


@router.post("/{supplier_id}/products/import", response_model=SupplierImportResult)
async def import_supplier_products(supplier_id: str, user: CurrentUser = Depends(get_current_user)):
    supplier = _get_supplier_row(user.id, supplier_id, _SUPPLIER_PUBLIC_COLUMNS + ",api_key,api_secret")
    platform = supplier.get("platform") or "autre"

    if not supplier.get("api_key"):
        return SupplierImportResult(
            supplier_id=supplier_id,
            imported=0,
            api_available=False,
            message="Aucune clé API renseignée pour ce fournisseur — ajoute-la (bouton Modifier) ou saisis les produits à la main.",
        )

    if platform != "eprolo":
        return SupplierImportResult(
            supplier_id=supplier_id,
            imported=0,
            api_available=False,
            message=f"Import automatique non disponible pour la plateforme « {platform} » — bientôt. Saisis les produits à la main en attendant.",
        )

    try:
        products = await _import_eprolo(supplier["api_key"], supplier.get("api_secret"))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Import Eprolo échoué pour supplier_id=%s : %s", supplier_id, type(exc).__name__)
        return SupplierImportResult(
            supplier_id=supplier_id,
            imported=0,
            api_available=False,
            message="L'API Eprolo n'a pas répondu (clé invalide ou endpoint pas encore ouvert par Eprolo). Saisie manuelle possible en attendant.",
        )

    if not products:
        return SupplierImportResult(
            supplier_id=supplier_id, imported=0, api_available=True, message="Catalogue Eprolo vide pour cette clé."
        )

    rows = []
    for p in products:
        rows.append({**p, "user_id": user.id, "supplier_id": supplier_id})
    supabase = get_supabase()
    try:
        supabase.table("supplier_products").upsert(rows, on_conflict="supplier_id,supplier_product_id").execute()
    except Exception as exc:
        logger.error("Écriture des produits Eprolo échouée : %s: %s", type(exc).__name__, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Enregistrement des produits importés impossible.")

    return SupplierImportResult(
        supplier_id=supplier_id,
        imported=len(rows),
        api_available=True,
        message=f"{len(rows)} produit(s) importé(s) depuis Eprolo.",
    )
