# =====================================================================
# === MAIN.PY — Point d'entrée FastAPI de l'API EtsyDrop ===
# =====================================================================
#
# Lancement local :
#   uvicorn main:app --reload --port 8000
#
# Sécurité mise en place ici (voir CLAUDE.md > CYBERSÉCURITÉ) :
#   - CORS restreint à localhost:5500 (dev) + domaine GitHub Pages (prod)
#   - Headers de sécurité HTTP sur toutes les réponses
#   - Rate limiting global 60 req/min/IP via slowapi
#   - Gestionnaire d'erreurs global qui ne fuite jamais de détails internes

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from rate_limiter import limiter
from routers import (
    ads,
    analytics,
    auth,
    billing,
    generate,
    keywords,
    listings,
    orders,
    pricing,
    product_test,
    promotion,
    seo,
    shop_analyzer,
    sourcing,
    suppliers,
)

load_dotenv()

# === LOGGING ===
# Règle stricte : on ne logge jamais le contenu du .env, un token, ou un secret.
# Seuls la méthode, le chemin et le type d'erreur sont consignés.
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("etsydrop")

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# === RATE LIMITING (slowapi — 60 requêtes/minute par IP, sur toute l'API) ===
# Instance définie dans rate_limiter.py (partagée avec routers/billing.py,
# qui exempte le webhook Stripe via @limiter.exempt — voir plus bas).

# === APPLICATION ===
app = FastAPI(
        title="EtsyDrop API",
        description="Backend FastAPI pour EtsyDrop — sourcing, fulfillment, SEO et analytics pour vendeurs Etsy.",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# === CORS ===
# Le frontend est hébergé sur GitHub Pages. On lit l'origin depuis l'env var
# en nettoyant les guillemets éventuels, et on ajoute l'URL GitHub Pages
# comme fallback systématique pour éviter les problèmes de valeur manquante.
FRONTEND_ORIGIN_LOCAL = os.getenv("FRONTEND_ORIGIN_LOCAL", "http://localhost:5500").strip()
FRONTEND_ORIGIN_PROD = os.getenv("FRONTEND_ORIGIN_PROD", "https://notfound4542.github.io").strip().strip("\"'")

ALLOWED_ORIGINS = list({
        FRONTEND_ORIGIN_LOCAL,
        FRONTEND_ORIGIN_PROD,
        "https://notfound4542.github.io",
})

app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
)


# === HEADERS DE SÉCURITÉ HTTP ===
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


# === GESTIONNAIRE D'ERREURS GLOBAL ===
# Ne jamais renvoyer la stacktrace ou un message d'exception brut au client :
# cela pourrait exposer un chemin de fichier, une clé partielle ou un détail interne.
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
        # exc_info=True : la réponse au client reste générique (jamais de détail
        # interne exposé), mais la stack trace complète part dans les logs Railway
        # — sans ça, un type(exc).__name__ seul ne suffit pas à diagnostiquer quoi
        # que ce soit en production.
        logger.error(
                "Erreur non gérée sur %s %s : %s: %s",
                request.method, request.url.path, type(exc).__name__, exc,
                exc_info=True,
        )
        return JSONResponse(status_code=500, content={"detail": "Erreur interne du serveur."})


# === ROUTERS ===
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(listings.router, prefix="/api/listings", tags=["listings"])
app.include_router(orders.router, prefix="/api/orders", tags=["orders"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["analytics"])
app.include_router(sourcing.router, prefix="/api/sourcing", tags=["sourcing"])
app.include_router(keywords.router, prefix="/api/keywords", tags=["keywords"])
app.include_router(shop_analyzer.router, prefix="/api/shop", tags=["shop-analyzer"])
app.include_router(generate.router, prefix="/api/generate", tags=["generate"])
app.include_router(pricing.router, prefix="/api/pricing", tags=["pricing"])
app.include_router(promotion.router, prefix="/api/promotion", tags=["promotion"])
app.include_router(ads.router, prefix="/api/ads", tags=["ads"])
# === PHASE 2 — fournisseurs, SEO, test produit (pricing étendu ci-dessus) ===
app.include_router(suppliers.router, prefix="/api/suppliers", tags=["suppliers"])
app.include_router(seo.router, prefix="/api/seo", tags=["seo"])
app.include_router(product_test.router, prefix="/api/product-test", tags=["product-test"])
# Le webhook Stripe (/api/billing/webhook) est exempté du rate limiter global
# via @limiter.exempt dans routers/billing.py — Stripe retente agressivement
# et n'a pas à être throttled comme un client public.
app.include_router(billing.router, prefix="/api/billing", tags=["billing"])


# === HEALTH CHECK ===
@app.get("/api/health", tags=["health"])
async def health_check():
        return {"status": "ok", "service": "etsydrop-api", "environment": ENVIRONMENT}
    
