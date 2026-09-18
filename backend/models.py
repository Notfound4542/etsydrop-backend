# =====================================================================
# === MODELS.PY — Schémas Pydantic (validation de TOUS les endpoints) ===
# =====================================================================
#
# Chaque route de l'API valide ses entrées et sorties avec ces modèles.
# Aucune donnée brute (dict non validé) ne doit atteindre Supabase ni être
# renvoyée telle quelle au frontend.

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl


# === ENUMS PARTAGÉS ===
class StockStatus(str, Enum):
    available = "available"
    low_stock = "low_stock"
    out_of_stock = "out_of_stock"


class OrderStatus(str, Enum):
    pending_supplier = "pending_supplier"
    in_transit = "in_transit"
    delivered = "delivered"
    delayed = "delayed"
    return_requested = "return_requested"


class SupplierName(str, Enum):
    my_catalog = "my_catalog"
    eprolo = "eprolo"
    cj_dropshipping = "cj_dropshipping"
    printify = "printify"
    printful = "printful"
    zendrop = "zendrop"
    aliexpress = "aliexpress"


# === AUTH / UTILISATEUR (Supabase Auth) ===
class CurrentUser(BaseModel):
    id: str
    # Format déjà validé par Supabase Auth en amont — pas besoin d'EmailStr
    # ici (évite une dépendance supplémentaire à email-validator).
    email: Optional[str] = None
    etsy_shop_connected: bool = False


class EtsyOAuthLoginResponse(BaseModel):
    authorize_url: HttpUrl
    state: str = Field(..., min_length=10)


class EtsyOAuthCallback(BaseModel):
    code: str = Field(..., min_length=10, max_length=512)
    state: str = Field(..., min_length=10, max_length=128)


# === LISTINGS (fiches produit) ===
class ListingVariant(BaseModel):
    color: str = Field(..., max_length=60)
    size: str = Field(..., max_length=60)
    engraving: str = Field(..., max_length=60)
    supplier_price: float = Field(..., ge=0)


class ListingCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=140)
    description: str = Field(..., min_length=10, max_length=2000)
    tags: List[str] = Field(..., min_length=1, max_length=13)
    price_min: float = Field(..., ge=0)
    price_max: float = Field(..., ge=0)
    supplier: SupplierName
    variants: List[ListingVariant] = Field(default_factory=list)


class Listing(ListingCreate):
    id: str
    # Renseigné uniquement pour les fiches importées depuis la boutique Etsy
    # connectée (voir routers/auth.py > etsy_callback / etsy_client.py >
    # sync_etsy_listings) — jamais soumis par le client, donc absent de
    # ListingCreate.
    etsy_listing_id: Optional[str] = None
    # NULL pour une fiche créée à la main sans image.
    image_url: Optional[str] = None
    # Override du typage strict de ListingCreate.variants (List[ListingVariant],
    # qui impose color/size/engraving — pensé pour la saisie manuelle) : les
    # variantes réelles Etsy viennent de propriétés arbitraires (Color, Size,
    # Material, ou toute autre taxonomie propre à la boutique), pas d'un
    # schéma fixe. Voir etsy_client.py > fetch_listing_variants pour la forme
    # exacte écrite ici : [{label, price, quantity}, ...] où `label` est déjà
    # la combinaison de propriétés formatée (ex. "Color: Gold / Size: 40cm").
    variants: List[Dict] = Field(default_factory=list)
    stock_status: StockStatus
    margin_pct: float = Field(..., ge=0, le=100)
    created_at: datetime


# === ORDERS (commandes / fulfillment) ===
class Order(BaseModel):
    id: str
    etsy_order_id: str
    customer_name: str = Field(..., max_length=120)
    product_name: str = Field(..., max_length=140)
    supplier: SupplierName
    amount: float = Field(..., ge=0)
    status: OrderStatus
    tracking_number: Optional[str] = Field(None, max_length=64)
    created_at: datetime


class SyncResult(BaseModel):
    synced: int = Field(..., ge=0)
    shop_id: int


class OrderFulfillRequest(BaseModel):
    supplier: SupplierName
    tracking_number: Optional[str] = Field(None, max_length=64)


# === ANALYTICS ===
class RevenuePoint(BaseModel):
    period: str = Field(..., max_length=20)
    revenue: float = Field(..., ge=0)
    profit: float = Field(..., ge=0)


class AnalyticsSummary(BaseModel):
    revenue_total: float = Field(..., ge=0)
    # "Marge nette" ici = revenu moins frais Etsy uniquement (pas de coût
    # fournisseur réel pour les fiches importées depuis Etsy, qui n'ont pas
    # de prix d'achat connu) — voir routers/analytics.py > get_analytics_summary.
    net_margin_pct: float = Field(..., ge=0, le=100)
    # Etsy Open API v3 n'expose ni le trafic ni les conversions par fiche à
    # une appli tierce (ce n'est disponible que dans Shop Manager, côté
    # vendeur, hors API publique) — toujours None ici, jamais une valeur
    # inventée. Le frontend doit afficher "Non disponible via API Etsy".
    conversion_rate_pct: Optional[float] = Field(None, ge=0, le=100)
    net_profit: float = Field(..., ge=0)
    orders_count: int = Field(..., ge=0)
    top_product_title: Optional[str] = None
    history: List[RevenuePoint] = Field(default_factory=list)


# === SOURCING (comparateur multi-fournisseurs) ===
class SourcingComparisonItem(BaseModel):
    supplier: SupplierName
    price: Optional[float] = Field(None, ge=0)
    delay_days_min: Optional[int] = Field(None, ge=0)
    delay_days_max: Optional[int] = Field(None, ge=0)
    available: bool = True


class SourcingCompareResult(BaseModel):
    product_name: str = Field(..., max_length=140)
    sources: List[SourcingComparisonItem]
    best_price: Optional[SourcingComparisonItem] = None
    recommended_sell_price: Optional[float] = Field(None, ge=0)


# === KEYWORDS (SEO / mots-clés) ===
class KeywordMetric(BaseModel):
    keyword: str = Field(..., min_length=2, max_length=80)
    volume_monthly: int = Field(..., ge=0)
    total_sales: int = Field(..., ge=0)
    competition: str = Field(..., max_length=20)
    trend_pct: float
    score: int = Field(..., ge=0, le=100)


class KeywordVolumeEstimate(BaseModel):
    """Proxy de volume basé sur la fréquence des tags Etsy pour un mot-clé donné."""

    keyword: str = Field(..., max_length=80)
    etsy_count: int = Field(..., ge=0)
    competition: str = Field(..., pattern="^(low|medium|high)$")


# === SOURCING MULTI-PLATEFORME (recherche live fournisseurs) ===
class SourcingSearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=140)
    country_code: str = Field(..., min_length=2, max_length=2)


class SourcingSupplierResult(BaseModel):
    source: str = Field(..., pattern="^(eprolo|cj|aliexpress)$")
    product_id: str = Field(..., max_length=80)
    title: str = Field(..., max_length=200)
    cost_price: float = Field(..., ge=0)
    shipping_cost: float = Field(..., ge=0)
    shipping_days: int = Field(..., ge=0)
    supplier_rating: float = Field(..., ge=0, le=5)
    stock: int = Field(..., ge=0)
    url: str = Field(..., max_length=500)


class SourcingSearchResponse(BaseModel):
    suppliers: List[SourcingSupplierResult]
    best_pick: int = Field(..., description="Index dans `suppliers`, -1 si aucun résultat")


# === ANALYTICS — REVENUS ===
class RevenueTopProduct(BaseModel):
    listing_id: str
    title: str = Field(..., max_length=200)
    revenue: float = Field(..., ge=0)
    units: int = Field(..., ge=0)


class RevenueAnalytics(BaseModel):
    revenue_gross: float = Field(..., ge=0)
    revenue_net: float
    orders_count: int = Field(..., ge=0)
    avg_order_value: float = Field(..., ge=0)
    top_products: List[RevenueTopProduct] = Field(default_factory=list)


# === ANALYTICS — MARGE PAR PAYS ===
class MarginCountryResult(BaseModel):
    country_code: str = Field(..., min_length=2, max_length=2)
    sale_price: float = Field(..., ge=0)
    etsy_fees: float = Field(..., ge=0)
    net_margin_eur: float
    margin_pct: float


class MarginAnalyticsResponse(BaseModel):
    listing_id: str
    supplier_cost: float = Field(..., ge=0)
    shipping_cost: float = Field(..., ge=0)
    results: List[MarginCountryResult]


# === SHOP ANALYZER (analyse concurrents) ===
class ShopAnalyzeRequest(BaseModel):
    shop_name: str = Field(..., min_length=2, max_length=80)


class ShopListingEstimate(BaseModel):
    listing_id: str
    title: str = Field(..., max_length=200)
    price: float = Field(..., ge=0)
    estimated_sales: int = Field(..., ge=0)
    num_favorers: int = Field(..., ge=0)
    tags: List[str] = Field(default_factory=list)
    url: str = Field(..., max_length=500)


class ShopAnalyzeResponse(BaseModel):
    shop_name: str
    listings: List[ShopListingEstimate]


# === GÉNÉRATION IA — MOCKUP PRODUIT (Stability AI) ===
class GenerateImageRequest(BaseModel):
    product_title: str = Field(..., min_length=3, max_length=140)
    product_category: str = Field(..., min_length=2, max_length=60)
    style_hints: Optional[List[str]] = None


class GeneratedImage(BaseModel):
    index: int = Field(..., ge=0)
    base64: str
    seed: int


class GenerateImageResponse(BaseModel):
    images: List[GeneratedImage]
    cost_eur: float = Field(..., ge=0)
    # Toujours None : le prompt interne n'est jamais exposé (actif IP, voir routers/generate.py).
    prompt_used: Optional[str] = None


# === GÉNÉRATION IA — FICHE ETSY OPTIMISÉE SEO (Claude) ===
class GenerateListingRequest(BaseModel):
    product_title: str = Field(..., min_length=3, max_length=140)
    category: str = Field(..., min_length=2, max_length=60)
    target_market: str = Field(..., pattern="^(US|UK|AU|CA)$")
    price_eur: float = Field(..., ge=0)
    key_features: List[str] = Field(..., min_length=1, max_length=10)


class ListingQualityDetails(BaseModel):
    title_score: int = Field(..., ge=0, le=100)
    description_score: int = Field(..., ge=0, le=100)
    tags_score: int = Field(..., ge=0, le=100)
    improvements: List[str] = Field(default_factory=list, max_length=3)


class GenerateListingResponse(BaseModel):
    title: str = Field(..., max_length=140)
    description: str
    tags: List[str] = Field(..., min_length=13, max_length=13)
    lqs_score: int = Field(..., ge=0, le=100)
    lqs_details: ListingQualityDetails


# === PRICING INTERNATIONAL ===
class PricingCalculateRequest(BaseModel):
    supplier_cost_eur: float = Field(..., ge=0)
    shipping_cost_eur: float = Field(..., ge=0)
    desired_margin_pct: float = Field(..., ge=0, le=100)
    countries: List[str] = Field(..., min_length=1)


class PricingCountryResult(BaseModel):
    country: str = Field(..., min_length=2, max_length=2)
    sale_price_eur: float = Field(..., ge=0)
    sale_price_local: float = Field(..., ge=0)
    currency: str = Field(..., min_length=3, max_length=3)
    net_margin_eur: float
    net_margin_pct: float
    etsy_fees_eur: float = Field(..., ge=0)
    viable: bool
    customs_note: Optional[str] = None


class PricingSummary(BaseModel):
    best_country: Optional[str] = None
    best_margin_pct: Optional[float] = None
    avg_sale_price_eur: Optional[float] = None


class PricingCalculateResponse(BaseModel):
    results: List[PricingCountryResult]
    recommended_countries: List[str]
    summary: PricingSummary


# === PROMOTION — PUBLICATION PINTEREST ===
class PinterestPublishRequest(BaseModel):
    listing_id: str = Field(..., min_length=1, max_length=40)
    board_id: str = Field(..., min_length=1, max_length=60)
    image_base64: str
    title: str = Field(..., min_length=1, max_length=140)
    description: str = Field(..., max_length=150)
    link: HttpUrl
    tags: List[str] = Field(..., min_length=1, max_length=13)


class PinterestPublishResponse(BaseModel):
    pin_id: str
    pin_url: str
    status: str = Field(..., pattern="^(published|failed)$")


# === ADS — SYNCHRONISATION & DASHBOARD ===
class AdsSyncRequest(BaseModel):
    platform: str = Field(..., pattern="^(etsy|meta)$")
    date_from: str = Field(..., min_length=8, max_length=10, description="Format YYYY-MM-DD")
    date_to: str = Field(..., min_length=8, max_length=10, description="Format YYYY-MM-DD")


class AdsSyncResponse(BaseModel):
    platform: str
    date_from: str
    date_to: str
    spend_eur: float = Field(..., ge=0)
    # Champs Meta uniquement :
    impressions: Optional[int] = None
    clicks: Optional[int] = None
    roas: Optional[float] = None
    # Champ Etsy uniquement :
    spend_by_listing: Optional[Dict[str, float]] = None


class AdsDashboardRow(BaseModel):
    platform: str
    total_spend_eur: float = Field(..., ge=0)
    total_revenue_eur: float = Field(..., ge=0)
    avg_roas: float = Field(..., ge=0)
    total_clicks: int = Field(..., ge=0)


class AdsDashboardResponse(BaseModel):
    rows: List[AdsDashboardRow]


# === BILLING — ABONNEMENT STRIPE ===
class BillingCheckoutRequest(BaseModel):
    plan: str = Field(..., pattern="^pro$")


class BillingCheckoutResponse(BaseModel):
    checkout_url: str


class BillingStatusResponse(BaseModel):
    plan: str = Field(..., pattern="^(free|pro)$")
    plan_started_at: Optional[datetime] = None
