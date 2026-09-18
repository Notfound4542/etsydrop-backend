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
    etsy_shop_name: Optional[str] = None


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
    # Coût fournisseur saisi à la main (jamais fourni par Etsy) et marge
    # cible : alimentent le simulateur "Prix par pays" avec un coût réel au
    # lieu du placeholder — voir routers/listings.py > update_listing_pricing.
    cost_price: Optional[float] = Field(None, ge=0)
    target_margin: float = Field(35.0, ge=0, le=95)
    created_at: datetime


class ListingPricingUpdate(BaseModel):
    cost_price: Optional[float] = Field(None, ge=0, le=100000)
    target_margin: Optional[float] = Field(None, ge=0, le=95)


# === ORDERS (commandes / fulfillment) ===
class OrderItem(BaseModel):
    listing_id: str = Field("", max_length=40)
    title: str = Field("", max_length=140)
    quantity: int = Field(1, ge=0)
    price: float = Field(0, ge=0)
    variations: List[str] = Field(default_factory=list)


class Order(BaseModel):
    id: str
    etsy_order_id: str
    customer_name: str = Field(..., max_length=120)
    product_name: str = Field(..., max_length=140)
    supplier: SupplierName
    amount: float = Field(..., ge=0)
    status: OrderStatus
    tracking_number: Optional[str] = Field(None, max_length=64)
    total_price: Optional[float] = Field(None, ge=0)
    currency: str = Field("EUR", min_length=3, max_length=3)
    # Statut brut Etsy (Paid, Completed, Open…) — distinct de `status`, qui
    # est notre enum de fulfillment.
    etsy_status: Optional[str] = Field(None, max_length=40)
    buyer_email: Optional[str] = None
    items: List[OrderItem] = Field(default_factory=list)
    created_timestamp: Optional[int] = None
    synced_at: Optional[datetime] = None
    created_at: datetime


class SyncResult(BaseModel):
    synced: int = Field(..., ge=0)
    # None quand shop_id n'est pas résolu en base : la sync renvoie alors
    # synced=0 avec la raison dans `errors` au lieu d'un 400 opaque.
    shop_id: Optional[int] = None
    # Détail de la sync des fiches (voir etsy_client.py > sync_etsy_listings) —
    # absents pour la sync des commandes.
    received: Optional[int] = Field(None, ge=0)
    with_image: Optional[int] = Field(None, ge=0)
    with_variants: Optional[int] = Field(None, ge=0)
    # True si le token OAuth a pu être utilisé (variantes) ; False = import
    # public seul (clé d'app). Toujours renseigné pour la sync des fiches.
    oauth_used: Optional[bool] = None
    # Raisons lisibles de chaque compteur à 0 (jamais de corps Etsy brut).
    errors: List[str] = Field(default_factory=list)


# === DIAGNOSTIC CONNEXION ETSY (GET /api/auth/etsy/debug) ===
class EtsyDebugStatus(BaseModel):
    connected: bool
    shop_id: Optional[int] = None
    shop_name: Optional[str] = None
    has_access_token: bool = False
    has_refresh_token: bool = False
    # updated_at + expires_in ; None si aucun token.
    expires_at: Optional[datetime] = None
    token_valid: bool = False
    seconds_remaining: Optional[int] = None
    listings_in_db: int = Field(0, ge=0)
    orders_in_db: int = Field(0, ge=0)
    # Lecture publique (clé d'app seule) : nombre de fiches actives côté Etsy.
    public_api_ok: bool = False
    etsy_active_listings: Optional[int] = None
    # Sonde OAuth : GET authentifié (après refresh préventif si nécessaire).
    oauth_probe_ok: Optional[bool] = None
    problems: List[str] = Field(default_factory=list)


class OrderFulfillRequest(BaseModel):
    supplier: SupplierName
    tracking_number: Optional[str] = Field(None, max_length=64)


# === ANALYTICS ===
class RevenuePoint(BaseModel):
    period: str = Field(..., max_length=20)
    revenue: float = Field(..., ge=0)
    # Peut être négatif sur un mois à 1 commande (frais fixes > marge).
    profit: float
    orders: int = Field(0, ge=0)


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
    visitors: Optional[int] = None
    shop_rating: Optional[float] = None
    net_profit: float
    orders_count: int = Field(..., ge=0)
    avg_order_value: float = Field(0, ge=0)
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
    period: str = Field("30d", max_length=10)
    revenue_gross: float = Field(..., ge=0)
    revenue_net: float
    orders_count: int = Field(..., ge=0)
    avg_order_value: float = Field(..., ge=0)
    top_products: List[RevenueTopProduct] = Field(default_factory=list)
    # Répartition par statut de fulfillment (enum OrderStatus -> nombre) —
    # alimente le donut "Statut des commandes" du dashboard.
    status_breakdown: Dict[str, int] = Field(default_factory=dict)
    history: List[RevenuePoint] = Field(default_factory=list)


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


# =====================================================================
# === PHASE 2 — FOURNISSEURS (routers/suppliers.py) ===
# =====================================================================
class SupplierPlatform(str, Enum):
    eprolo = "eprolo"
    aliexpress = "aliexpress"
    cj = "cj"
    autre = "autre"


class SupplierCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    platform: SupplierPlatform = SupplierPlatform.autre
    # Secrets : acceptés en entrée, stockés en DB, JAMAIS renvoyés (voir Supplier).
    api_key: Optional[str] = Field(None, max_length=512)
    api_secret: Optional[str] = Field(None, max_length=512)
    contact_email: Optional[str] = Field(None, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    notes: Optional[str] = Field(None, max_length=1000)


class SupplierUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=80)
    platform: Optional[SupplierPlatform] = None
    api_key: Optional[str] = Field(None, max_length=512)
    api_secret: Optional[str] = Field(None, max_length=512)
    contact_email: Optional[str] = Field(None, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    notes: Optional[str] = Field(None, max_length=1000)


class Supplier(BaseModel):
    id: str
    name: str
    platform: SupplierPlatform
    contact_email: Optional[str] = None
    notes: Optional[str] = None
    # Seul indicateur exposé sur les secrets — la valeur elle-même ne quitte
    # jamais le backend (CLAUDE.md > CYBERSÉCURITÉ).
    has_api_key: bool = False
    products_count: int = Field(0, ge=0)
    created_at: datetime


class SupplierDimensions(BaseModel):
    length: Optional[float] = Field(None, ge=0, le=1000)
    width: Optional[float] = Field(None, ge=0, le=1000)
    height: Optional[float] = Field(None, ge=0, le=1000)


class SupplierVariant(BaseModel):
    color: Optional[str] = Field(None, max_length=60)
    size: Optional[str] = Field(None, max_length=60)
    stock: Optional[int] = Field(None, ge=0)
    price: Optional[float] = Field(None, ge=0)


class SupplierProductCreate(BaseModel):
    supplier_product_id: Optional[str] = Field(None, max_length=120)
    name: str = Field(..., min_length=2, max_length=200)
    description: Optional[str] = Field(None, max_length=3000)
    base_price: float = Field(..., ge=0, le=100000)
    currency: str = Field("EUR", min_length=3, max_length=3)
    weight_grams: Optional[int] = Field(None, ge=0, le=100000)
    dimensions_cm: Optional[SupplierDimensions] = None
    variants: List[SupplierVariant] = Field(default_factory=list, max_length=200)
    images: List[str] = Field(default_factory=list, max_length=20)
    moq: int = Field(1, ge=1, le=100000)
    lead_time_days: Optional[int] = Field(None, ge=0, le=365)
    linked_etsy_listing_id: Optional[int] = Field(None, ge=1)


class SupplierProduct(SupplierProductCreate):
    id: str
    supplier_id: str
    # 'manual' | 'eprolo' | 'aliexpress' (voir migrations/2026-09-18_aliexpress.sql)
    source: Optional[str] = "manual"
    external_id: Optional[str] = None
    shipping_days_estimate: Optional[int] = Field(None, ge=0)
    rating: Optional[float] = Field(None, ge=0, le=5)
    orders_count: Optional[int] = Field(None, ge=0)
    created_at: datetime


class SupplierProductLink(BaseModel):
    # None = délier la fiche.
    linked_etsy_listing_id: Optional[int] = Field(None, ge=1)


class SupplierImportResult(BaseModel):
    supplier_id: str
    imported: int = Field(..., ge=0)
    # False quand la plateforme n'a pas (encore) d'API branchée : le message
    # explique quoi faire (saisie manuelle en attendant).
    api_available: bool
    message: str


class ConversationDirection(str, Enum):
    sent = "sent"
    received = "received"


class ConversationStatus(str, Enum):
    open = "open"
    answered = "answered"
    closed = "closed"


class SupplierConversationCreate(BaseModel):
    supplier_id: str = Field(..., min_length=36, max_length=36)
    supplier_product_id: Optional[str] = Field(None, min_length=36, max_length=36)
    subject: str = Field(..., min_length=2, max_length=140)
    message: str = Field(..., min_length=2, max_length=4000)
    direction: ConversationDirection = ConversationDirection.sent


class SupplierConversationUpdate(BaseModel):
    status: ConversationStatus


class SupplierConversation(BaseModel):
    id: str
    supplier_id: str
    supplier_product_id: Optional[str] = None
    subject: str
    message: str
    direction: ConversationDirection
    status: ConversationStatus
    email_sent: bool = False
    created_at: datetime


# =====================================================================
# === PHASE 2 — PRIX & LIVRAISON PAR PAYS (routers/pricing.py) ===
# =====================================================================
class ShippingCountry(BaseModel):
    code: str = Field(..., min_length=2, max_length=2)
    name: Optional[str] = Field(None, max_length=60)
    shipping_cost: float = Field(..., ge=0, le=1000)
    delivery_days_min: Optional[int] = Field(None, ge=0, le=120)
    delivery_days_max: Optional[int] = Field(None, ge=0, le=180)


class ShippingProfileCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    is_free_shipping: bool = False
    target_margin_pct: float = Field(35.0, ge=0, le=95)
    countries: List[ShippingCountry] = Field(..., min_length=1, max_length=40)


class ShippingProfile(ShippingProfileCreate):
    id: str
    created_at: datetime


class PricingAnalyzeRequest(BaseModel):
    # UUID interne de la fiche (listings.id) — optionnel pour une simulation
    # libre depuis la page Pricing sans fiche sélectionnée.
    listing_id: Optional[str] = Field(None, min_length=36, max_length=36)
    cost_price: float = Field(..., ge=0, le=100000)
    target_margin_pct: Optional[float] = Field(None, ge=0, le=95)
    shipping_profile_id: Optional[str] = Field(None, min_length=36, max_length=36)
    # Saisie manuelle par pays (prioritaire sur le profil si fournie).
    countries: Optional[List[ShippingCountry]] = Field(None, max_length=40)
    free_shipping: Optional[bool] = None
    # False pour un vendeur en franchise de TVA (auto-entrepreneur sous seuil).
    vat_applicable: bool = True
    # Prix actuellement affiché sur Etsy : si fourni, on renvoie aussi la
    # marge RÉELLE à ce prix (pas seulement au prix recommandé).
    current_price: Optional[float] = Field(None, ge=0, le=100000)


class PricingCountryAnalysis(BaseModel):
    country_code: str
    country_name: str
    flag: str
    currency: str
    recommended_price_eur: Optional[float] = None
    recommended_price_local: Optional[float] = None
    shipping_charged_eur: float = 0
    shipping_cost_eur: float = 0
    buyer_total_eur: float = 0
    etsy_fees_eur: float = 0
    currency_fee_eur: float = 0
    vat_rate_pct: float = 0
    vat_label: str = ""
    vat_collected_by_etsy: bool = False
    vat_eur: float = 0
    seller_net_eur: float = 0
    margin_eur: Optional[float] = None
    margin_pct: Optional[float] = None
    # Marge réelle au prix actuellement en ligne (si current_price fourni).
    current_margin_eur: Optional[float] = None
    current_margin_pct: Optional[float] = None
    delivery_days_min: Optional[int] = None
    delivery_days_max: Optional[int] = None
    # False = marge cible inatteignable pour ce pays.
    reachable: bool = True
    # 'good' (≥ cible) | 'close' (cible - 10 pts ≤ marge < cible) | 'bad' (< cible - 10 pts ou négatif)
    status: str = Field("good", pattern="^(good|close|bad)$")


class PricingWorstCase(BaseModel):
    country_code: str
    country_name: str
    flag: str
    margin_pct: Optional[float] = None
    reason: str


class PricingAnalyzeResponse(BaseModel):
    listing_id: Optional[str] = None
    cost_price: float
    target_margin_pct: float
    free_shipping: bool
    vat_applicable: bool
    results: List[PricingCountryAnalysis]
    worst_case: Optional[PricingWorstCase] = None
    best_case: Optional[PricingWorstCase] = None
    # Prix unique conseillé (Etsy n'a qu'un prix par fiche) : le plus élevé
    # des prix recommandés pour que la marge tienne dans TOUS les pays.
    suggested_single_price_eur: Optional[float] = None
    analysis_id: Optional[str] = None


class PricingApplyRequest(BaseModel):
    price_eur: float = Field(..., gt=0, le=100000)


class PricingApplyResponse(BaseModel):
    listing_id: str
    etsy_listing_id: str
    price_eur: float
    # 'listing' (fiche sans variantes → updateListing) ou 'inventory'
    # (variantes → updateListingInventory, toutes les offerings au même prix)
    method: str = Field(..., pattern="^(listing|inventory)$")
    offerings_updated: int = Field(0, ge=0)


# =====================================================================
# === PHASE 2 — SEO (routers/seo.py) ===
# =====================================================================
class SeoCheck(BaseModel):
    # 'ok' | 'warning' | 'missing'
    status: str = Field(..., pattern="^(ok|warning|missing)$")
    text: str = Field(..., max_length=200)


class SeoSection(BaseModel):
    score: int = Field(..., ge=0, le=100)
    checks: List[SeoCheck] = Field(default_factory=list)


class SeoKeyword(BaseModel):
    keyword: str = Field(..., max_length=80)
    count: int = Field(..., ge=0)
    in_title: bool = False
    in_tags: bool = False
    in_description: bool = False


class SeoAnalyzeResponse(BaseModel):
    listing_id: str
    etsy_listing_id: Optional[str] = None
    score: int = Field(..., ge=0, le=100)
    title: SeoSection
    tags: SeoSection
    description: SeoSection
    keywords: List[SeoKeyword] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    title_length: int = Field(0, ge=0)
    tags_count: int = Field(0, ge=0)
    description_length: int = Field(0, ge=0)


class SeoTrendingKeyword(BaseModel):
    keyword: str = Field(..., max_length=120)
    # 'taxonomy' (catégorie Etsy) | 'catalogue' (tag fréquent dans tes fiches)
    # | 'market' (tag fréquent chez les fiches actives Etsy pour cette recherche)
    source: str = Field(..., pattern="^(taxonomy|catalogue|market)$")
    count: int = Field(0, ge=0)


class SeoTrendingResponse(BaseModel):
    query: str
    keywords: List[SeoTrendingKeyword] = Field(default_factory=list)
    taxonomy_paths: List[str] = Field(default_factory=list)


class SeoOptimizeResponse(BaseModel):
    listing_id: str
    title: str = Field(..., max_length=140)
    description: str
    tags: List[str] = Field(..., min_length=1, max_length=13)
    # 'claude' (IA) | 'heuristic' (règles locales, sans clé Anthropic)
    engine: str = Field(..., pattern="^(claude|heuristic)$")
    notes: List[str] = Field(default_factory=list)


class SeoApplyRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=3, max_length=140)
    description: Optional[str] = Field(None, min_length=10, max_length=10000)
    tags: Optional[List[str]] = Field(None, min_length=1, max_length=13)


class SeoApplyResponse(BaseModel):
    listing_id: str
    etsy_listing_id: str
    updated_fields: List[str]


# =====================================================================
# === PHASE 2 — TEST PRODUIT / SIMULATION D'ACHAT (routers/product_test.py) ===
# =====================================================================
class ProductTestRequest(BaseModel):
    listing_id: str = Field(..., min_length=36, max_length=36)
    destination_country: str = Field(..., min_length=2, max_length=2)
    # Label de variante (voir listings.variants[].label) — None = prix de base.
    variant_selected: Optional[str] = Field(None, max_length=160)
    quantity: int = Field(1, ge=1, le=100)


class ProductTestLine(BaseModel):
    label: str = Field(..., max_length=120)
    amount_eur: float
    # 'buyer' (ce que paie l'acheteur) | 'fee' (déduit) | 'cost' (coût) | 'net'
    kind: str = Field(..., pattern="^(buyer|fee|cost|net)$")
    note: Optional[str] = Field(None, max_length=200)


class ProductTestResult(BaseModel):
    id: Optional[str] = None
    listing_id: str
    etsy_listing_id: Optional[str] = None
    destination_country: str
    country_name: str
    flag: str
    currency: str
    variant_selected: Optional[str] = None
    quantity: int = 1
    unit_price_eur: float
    shipping_eur: float
    # 'etsy_profile' (profil d'expédition Etsy réel) | 'default' (estimation)
    shipping_source: str = Field(..., pattern="^(etsy_profile|default)$")
    delivery_days_min: Optional[int] = None
    delivery_days_max: Optional[int] = None
    buyer_total_eur: float
    buyer_total_local: float
    seller_net_eur: float
    cost_price_eur: Optional[float] = None
    margin_eur: Optional[float] = None
    margin_pct: Optional[float] = None
    # 'good' | 'close' | 'bad' | 'unknown' (pas de coût fournisseur)
    status: str = Field(..., pattern="^(good|close|bad|unknown)$")
    lines: List[ProductTestLine] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None


# =====================================================================
# === SOURCING ALIEXPRESS (routers/aliexpress.py) ===
# =====================================================================
class AliexpressProduct(BaseModel):
    product_id: str = Field(..., max_length=40)
    title: str = Field(..., max_length=300)
    price_usd: Optional[float] = Field(None, ge=0)
    original_price_usd: Optional[float] = Field(None, ge=0)
    image_url: Optional[str] = Field(None, max_length=600)
    url: str = Field(..., max_length=600)
    # Texte tel que fourni par AliExpress ("Free shipping", "Livraison: 2,10 €")
    shipping_to_fr: Optional[str] = Field(None, max_length=120)
    rating: Optional[float] = Field(None, ge=0, le=5)
    orders_count: Optional[int] = Field(None, ge=0)
    store_name: Optional[str] = Field(None, max_length=160)


class AliexpressSearchResponse(BaseModel):
    query: str
    page: int = Field(1, ge=1)
    limit: int = Field(20, ge=1, le=50)
    total: Optional[int] = Field(None, ge=0)
    # 'affiliate_api' (clés ALIEXPRESS_APP_*) | 'scrape' (repli pages publiques)
    source: str = Field(..., pattern="^(affiliate_api|scrape)$")
    cached: bool = False
    results: List[AliexpressProduct] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class AliexpressVariant(BaseModel):
    sku_id: Optional[str] = Field(None, max_length=60)
    label: str = Field(..., max_length=200)
    price_usd: Optional[float] = Field(None, ge=0)
    stock: Optional[int] = Field(None, ge=0)


class AliexpressQuantityPrice(BaseModel):
    min_quantity: int = Field(..., ge=1)
    price_usd: float = Field(..., ge=0)


class AliexpressShippingOption(BaseModel):
    country_code: str = Field(..., min_length=2, max_length=2)
    method: Optional[str] = Field(None, max_length=120)
    cost_usd: Optional[float] = Field(None, ge=0)
    delivery_days_min: Optional[int] = Field(None, ge=0)
    delivery_days_max: Optional[int] = Field(None, ge=0)


class AliexpressProductDetail(AliexpressProduct):
    description: Optional[str] = Field(None, max_length=3000)
    images: List[str] = Field(default_factory=list, max_length=20)
    variants: List[AliexpressVariant] = Field(default_factory=list)
    quantity_prices: List[AliexpressQuantityPrice] = Field(default_factory=list)
    shipping: List[AliexpressShippingOption] = Field(default_factory=list)
    source: str = Field(..., pattern="^(affiliate_api|scrape)$")
    warnings: List[str] = Field(default_factory=list)


class AliexpressImportRequest(BaseModel):
    product_id: str = Field(..., min_length=3, max_length=40, pattern=r"^[0-9]+$")
    # UUID interne (listings.id) — optionnel : lie le produit à une fiche Etsy.
    listing_id: Optional[str] = Field(None, min_length=36, max_length=36)


class AliexpressImportResponse(BaseModel):
    supplier_id: str
    supplier_product_id: str
    product_id: str
    linked_etsy_listing_id: Optional[int] = None
    # True si le produit existait déjà (mise à jour au lieu d'un doublon).
    updated: bool = False
    product: SupplierProduct
