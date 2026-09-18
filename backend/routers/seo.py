# =====================================================================
# === ROUTERS/SEO.PY — Analyse SEO, mots-clés tendance, optimisation ===
# =====================================================================
#
#  - GET  /analyze/{listing_id}   : scores titre / tags / description +
#                                   mots-clés détectés + suggestions
#  - GET  /keywords/trending      : mots-clés issus de la taxonomie Etsy
#                                   (seller-taxonomy/nodes), des tags du
#                                   catalogue de l'utilisateur et des fiches
#                                   actives Etsy pour la recherche
#  - POST /optimize/{listing_id}  : titre / tags / description optimisés
#                                   (Claude si ANTHROPIC_API_KEY, sinon
#                                   heuristique locale) — TOUJOURS en anglais
#                                   (CLAUDE.md > FICHES PRODUIT)
#  - POST /apply/{listing_id}     : pousse titre / tags / description sur Etsy
#                                   (updateListing = PATCH, scope listings_w)
#
# Rappel honnêteté des données : Etsy n'expose AUCUN volume de recherche à
# une appli tierce. "Trending" ici = catégories officielles + fréquence de
# tags observée, jamais un volume inventé.

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

from anthropic import AsyncAnthropic
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError

from database import get_current_user, get_supabase
from etsy_client import etsy_get, etsy_request, get_etsy_access_token, get_etsy_shop_id
from models import (
    CurrentUser,
    SeoAnalyzeResponse,
    SeoApplyRequest,
    SeoApplyResponse,
    SeoCheck,
    SeoKeyword,
    SeoOptimizeResponse,
    SeoSection,
    SeoTrendingKeyword,
    SeoTrendingResponse,
)

router = APIRouter()
logger = logging.getLogger("etsydrop.seo")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SEO_MODEL = "claude-opus-5"

ETSY_TITLE_MAX = 140
ETSY_TAGS_MAX = 13
ETSY_TAG_LEN_MAX = 20
DESCRIPTION_GOOD = 300
DESCRIPTION_MIN = 150
SEO_SNIPPET = 160

STOPWORDS = {
    # EN
    "the", "a", "an", "and", "or", "for", "with", "of", "to", "in", "on", "by", "at", "from", "your", "you",
    "this", "that", "is", "are", "it", "its", "our", "we", "her", "him", "his", "them", "as", "be", "will",
    "can", "any", "all", "new", "free", "one", "per", "set", "made", "just",
    # FR
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "ou", "pour", "avec", "en", "sur", "par",
    "au", "aux", "ce", "cette", "ces", "est", "sont", "vous", "votre", "vos", "nos", "notre",
}


# === TOKENISATION ===
def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-zà-ÿ0-9]+", (text or "").lower()) if len(t) > 2 and t not in STOPWORDS]


def _fetch_listing(user_id: str, listing_id: str) -> dict:
    supabase = get_supabase()
    result = (
        supabase.table("listings")
        .select("id,etsy_listing_id,name,description,tags,price_min")
        .eq("id", listing_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise HTTPException(status_code=404, detail="Fiche introuvable.")
    return result.data


# === ANALYSE HEURISTIQUE ===
def _analyze(title: str, tags: List[str], description: str) -> dict:
    title = (title or "").strip()
    description = (description or "").strip()
    tags = [t.strip() for t in (tags or []) if t and t.strip()]
    title_tokens = _tokens(title)
    tag_tokens = [tok for t in tags for tok in _tokens(t)]
    desc_tokens = _tokens(description)
    suggestions: List[str] = []

    # --- Mots-clés (fréquence titre + tags, pondérée) ---
    counter: Counter = Counter()
    for tok in title_tokens:
        counter[tok] += 2
    for tok in tag_tokens:
        counter[tok] += 1
    title_set, tag_set, desc_set = set(title_tokens), set(tag_tokens), set(desc_tokens)
    keywords = [
        SeoKeyword(keyword=k, count=c, in_title=k in title_set, in_tags=k in tag_set, in_description=k in desc_set)
        for k, c in counter.most_common(15)
    ]
    top_words = [k.keyword for k in keywords[:5]]

    # --- TITRE ---
    title_checks: List[SeoCheck] = []
    title_score = 0
    length = len(title)
    if 70 <= length <= ETSY_TITLE_MAX:
        title_score += 35
        title_checks.append(SeoCheck(status="ok", text=f"Longueur {length}/140 caractères — bien exploitée"))
    elif 40 <= length < 70:
        title_score += 20
        title_checks.append(SeoCheck(status="warning", text=f"Titre court ({length}/140) — ajoute des mots-clés secondaires"))
        suggestions.append("Allonge le titre à 70–140 caractères avec 2–3 expressions longue traîne.")
    elif length > ETSY_TITLE_MAX:
        title_checks.append(SeoCheck(status="missing", text=f"Titre trop long ({length}) — Etsy coupe à 140"))
        suggestions.append("Raccourcis le titre sous 140 caractères.")
    else:
        title_checks.append(SeoCheck(status="missing", text=f"Titre très court ({length}/140)"))
        suggestions.append("Rédige un titre de 70–140 caractères commençant par ton mot-clé principal.")

    first_words = set(title_tokens[:4])
    top_tag_words = set(tok for t in tags[:5] for tok in _tokens(t))
    if title_tokens and (first_words & top_tag_words):
        title_score += 30
        title_checks.append(SeoCheck(status="ok", text="Mot-clé principal en début de titre"))
    elif title_tokens:
        title_score += 10
        title_checks.append(SeoCheck(status="warning", text="Le début du titre ne reprend pas tes tags principaux"))
        if top_words:
            suggestions.append(f"Place « {top_words[0]} » dans les 4 premiers mots du titre.")
    else:
        title_checks.append(SeoCheck(status="missing", text="Aucun mot-clé détecté dans le titre"))

    repeats = [w for w, c in Counter(title_tokens).items() if c > 2]
    if not repeats:
        title_score += 20
        title_checks.append(SeoCheck(status="ok", text="Pas de répétition abusive"))
    else:
        title_checks.append(SeoCheck(status="warning", text=f"Mot répété plus de 2 fois : {', '.join(repeats[:3])}"))
        suggestions.append(f"Évite de répéter « {repeats[0]} » plus de deux fois dans le titre.")

    if title and title.upper() == title and length > 10:
        title_checks.append(SeoCheck(status="warning", text="Titre entièrement en majuscules (pénalisé par Etsy)"))
        suggestions.append("Écris le titre en minuscules/majuscules normales.")
    else:
        title_score += 15

    # --- TAGS ---
    tag_checks: List[SeoCheck] = []
    tag_score = 0
    n = len(tags)
    if n >= ETSY_TAGS_MAX:
        tag_score += 35
        tag_checks.append(SeoCheck(status="ok", text="13/13 tags utilisés"))
    elif n >= 9:
        tag_score += 20
        tag_checks.append(SeoCheck(status="warning", text=f"{n}/13 tags — {ETSY_TAGS_MAX - n} emplacement(s) perdu(s)"))
        suggestions.append(f"Ajoute {ETSY_TAGS_MAX - n} tag(s) : chaque tag est une porte d'entrée dans la recherche Etsy.")
    else:
        tag_checks.append(SeoCheck(status="missing", text=f"Seulement {n}/13 tags"))
        suggestions.append("Utilise les 13 tags disponibles (expressions de 2–3 mots).")

    long_tail = [t for t in tags if len(t.split()) >= 2]
    if tags and len(long_tail) >= max(1, n // 2):
        tag_score += 25
        tag_checks.append(SeoCheck(status="ok", text=f"{len(long_tail)} tags longue traîne (2+ mots)"))
    elif tags:
        tag_score += 10
        tag_checks.append(SeoCheck(status="warning", text=f"Seulement {len(long_tail)} tags multi-mots — trop de tags courts"))
        suggestions.append("Remplace les tags d'un seul mot par des expressions de 2–3 mots (ex. « gold name necklace »).")

    too_long = [t for t in tags if len(t) > ETSY_TAG_LEN_MAX]
    if too_long:
        tag_checks.append(SeoCheck(status="missing", text=f"Tag > 20 caractères (refusé par Etsy) : {too_long[0]}"))
        suggestions.append(f"Raccourcis le tag « {too_long[0]} » (max 20 caractères).")
    else:
        tag_score += 10

    dupes = [t for t, c in Counter(t.lower() for t in tags).items() if c > 1]
    if dupes:
        tag_checks.append(SeoCheck(status="warning", text=f"Tag en double : {dupes[0]}"))
        suggestions.append(f"Supprime le doublon « {dupes[0]} » et libère un emplacement.")
    else:
        tag_score += 10

    overlap = [t for t in tags if set(_tokens(t)) & title_set]
    if tags and len(overlap) >= max(1, n // 3):
        tag_score += 20
        tag_checks.append(SeoCheck(status="ok", text=f"{len(overlap)} tags cohérents avec le titre"))
    elif tags:
        tag_score += 5
        tag_checks.append(SeoCheck(status="warning", text="Peu de tags repris dans le titre (cohérence titre/tags faible)"))
        suggestions.append("Reprends tes 3–4 tags les plus importants mot pour mot dans le titre.")

    # --- DESCRIPTION ---
    desc_checks: List[SeoCheck] = []
    desc_score = 0
    dlen = len(description)
    if dlen >= DESCRIPTION_GOOD:
        desc_score += 35
        desc_checks.append(SeoCheck(status="ok", text=f"{dlen} caractères — description détaillée"))
    elif dlen >= DESCRIPTION_MIN:
        desc_score += 18
        desc_checks.append(SeoCheck(status="warning", text=f"{dlen} caractères — un peu courte"))
        suggestions.append("Développe la description (matériaux, dimensions, personnalisation, délais, entretien).")
    else:
        desc_checks.append(SeoCheck(status="missing", text=f"Description trop courte ({dlen} caractères)"))
        suggestions.append("Rédige au moins 300 caractères : Google et Etsy indexent les premières phrases.")

    snippet_tokens = set(_tokens(description[:SEO_SNIPPET]))
    if top_words and snippet_tokens & set(top_words):
        desc_score += 30
        desc_checks.append(SeoCheck(status="ok", text="Mot-clé principal dans les 160 premiers caractères"))
    elif description:
        desc_score += 8
        desc_checks.append(SeoCheck(status="warning", text="Les 160 premiers caractères ne contiennent pas ton mot-clé principal"))
        if top_words:
            suggestions.append(f"Commence la description par une phrase contenant « {top_words[0]} » (extrait affiché dans Google).")
    else:
        desc_checks.append(SeoCheck(status="missing", text="Aucun extrait SEO"))

    present = [w for w in top_words if w in desc_set]
    if top_words and len(present) >= min(3, len(top_words)):
        desc_score += 25
        desc_checks.append(SeoCheck(status="ok", text=f"{len(present)}/{len(top_words)} mots-clés principaux présents"))
    elif top_words:
        desc_score += 8
        missing = [w for w in top_words if w not in desc_set]
        desc_checks.append(SeoCheck(status="warning", text=f"Mots-clés absents de la description : {', '.join(missing[:3])}"))
        suggestions.append(f"Ajoute « {missing[0]} » dans la description." if missing else "Ajoute tes mots-clés principaux dans la description.")

    paragraphs = [p for p in re.split(r"\n\s*\n|\n", description) if p.strip()]
    if len(paragraphs) >= 3:
        desc_score += 10
        desc_checks.append(SeoCheck(status="ok", text="Structurée en plusieurs paragraphes"))
    elif description:
        desc_checks.append(SeoCheck(status="warning", text="Un seul bloc de texte — aère en 3+ paragraphes"))

    # Plafonds : les points "pas de défaut" (pas de doublon, pas de majuscules…)
    # ne doivent pas faire monter un contenu quasi vide au-dessus de la moyenne.
    if length < 40:
        title_score = min(title_score, 30)
    if n < 9:
        tag_score = min(tag_score, 40)
    if dlen < DESCRIPTION_MIN:
        desc_score = min(desc_score, 25)
    title_score = max(0, min(100, title_score))
    tag_score = max(0, min(100, tag_score))
    desc_score = max(0, min(100, desc_score))
    total = round(title_score * 0.4 + tag_score * 0.35 + desc_score * 0.25)

    return {
        "score": total,
        "title": SeoSection(score=title_score, checks=title_checks),
        "tags": SeoSection(score=tag_score, checks=tag_checks),
        "description": SeoSection(score=desc_score, checks=desc_checks),
        "keywords": keywords,
        "suggestions": suggestions[:8],
        "title_length": length,
        "tags_count": n,
        "description_length": dlen,
    }


@router.get("/analyze/{listing_id}", response_model=SeoAnalyzeResponse)
async def analyze_listing_seo(listing_id: str, user: CurrentUser = Depends(get_current_user)):
    listing = _fetch_listing(user.id, listing_id)
    analysis = _analyze(listing.get("name") or "", listing.get("tags") or [], listing.get("description") or "")
    return SeoAnalyzeResponse(listing_id=listing_id, etsy_listing_id=listing.get("etsy_listing_id"), **analysis)


# === TAXONOMIE ETSY (cache mémoire 24 h) ===
_taxonomy_cache: Dict[str, object] = {"at": 0.0, "nodes": []}
_TAXONOMY_TTL = 24 * 3600


def _flatten_taxonomy(nodes: list, path: Tuple[str, ...] = ()) -> List[dict]:
    out = []
    for node in nodes or []:
        name = str(node.get("name") or "").strip()
        if not name:
            continue
        current = path + (name,)
        out.append({"name": name, "path": current, "id": node.get("id")})
        out.extend(_flatten_taxonomy(node.get("children") or [], current))
    return out


async def _get_taxonomy() -> List[dict]:
    now = time.time()
    if _taxonomy_cache["nodes"] and now - float(_taxonomy_cache["at"]) < _TAXONOMY_TTL:
        return _taxonomy_cache["nodes"]  # type: ignore[return-value]
    try:
        payload = await etsy_get("/seller-taxonomy/nodes")
        nodes = _flatten_taxonomy(payload.get("results", []) if isinstance(payload, dict) else [])
    except Exception as exc:
        logger.warning("Taxonomie Etsy indisponible : %s", type(exc).__name__)
        return _taxonomy_cache["nodes"] or []  # type: ignore[return-value]
    _taxonomy_cache["nodes"] = nodes
    _taxonomy_cache["at"] = now
    return nodes


@router.get("/keywords/trending", response_model=SeoTrendingResponse)
async def trending_keywords(
    listing_id: Optional[str] = Query(None, min_length=36, max_length=36),
    q: Optional[str] = Query(None, min_length=2, max_length=80),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Trois sources combinées (chacune ignorée si indisponible, jamais bloquante) :
      1. taxonomy  — catégories Etsy dont le nom contient un mot de la fiche
      2. catalogue — tags les plus fréquents dans TES fiches
      3. market    — tags les plus fréquents parmi les fiches actives Etsy
                     pour la recherche (proxy de popularité, pas un volume)
    """
    query_terms: List[str] = []
    if listing_id:
        listing = _fetch_listing(user.id, listing_id)
        query_terms = _tokens(listing.get("name") or "")[:6]
        if not q:
            q = " ".join(query_terms[:3])
    if q:
        query_terms = list(dict.fromkeys(query_terms + _tokens(q)))
    if not query_terms:
        raise HTTPException(status_code=422, detail="Indique une fiche (listing_id) ou une recherche (q).")

    keywords: List[SeoTrendingKeyword] = []
    seen = set()

    # 1. Taxonomie
    taxonomy_paths: List[str] = []
    nodes = await _get_taxonomy()
    term_set = set(query_terms)
    matched = [n for n in nodes if set(_tokens(n["name"])) & term_set]
    matched.sort(key=lambda n: (-len(set(_tokens(n["name"])) & term_set), len(n["path"])))
    for n in matched[:12]:
        kw = n["name"].lower()
        if kw not in seen:
            seen.add(kw)
            keywords.append(SeoTrendingKeyword(keyword=kw[:120], source="taxonomy", count=len(set(_tokens(n["name"])) & term_set)))
        taxonomy_paths.append(" > ".join(n["path"]))
    taxonomy_paths = list(dict.fromkeys(taxonomy_paths))[:8]

    # 2. Catalogue de l'utilisateur
    try:
        rows = get_supabase().table("listings").select("tags").eq("user_id", user.id).execute().data or []
        cat_counter: Counter = Counter()
        for row in rows:
            for t in row.get("tags") or []:
                if set(_tokens(t)) & term_set:
                    cat_counter[t.lower()] += 1
        for kw, c in cat_counter.most_common(10):
            if kw not in seen:
                seen.add(kw)
                keywords.append(SeoTrendingKeyword(keyword=kw[:120], source="catalogue", count=c))
    except Exception as exc:
        logger.warning("Lecture des tags du catalogue échouée : %s", type(exc).__name__)

    # 3. Marché Etsy (fiches actives pour la recherche — endpoint public)
    if q:
        try:
            payload = await etsy_get("/listings/active", params={"keywords": q, "limit": 100})
            market_counter: Counter = Counter()
            for item in payload.get("results", []) if isinstance(payload, dict) else []:
                for t in item.get("tags") or []:
                    market_counter[str(t).lower()] += 1
            for kw, c in market_counter.most_common(20):
                if kw not in seen:
                    seen.add(kw)
                    keywords.append(SeoTrendingKeyword(keyword=kw[:120], source="market", count=c))
        except Exception as exc:
            logger.warning("Recherche marché Etsy échouée : %s", type(exc).__name__)

    return SeoTrendingResponse(query=q or " ".join(query_terms), keywords=keywords[:40], taxonomy_paths=taxonomy_paths)


# === OPTIMISATION (Claude → fallback heuristique) ===
def _heuristic_optimize(title: str, tags: List[str], description: str, extra_keywords: List[str]) -> dict:
    """Optimisation locale, sans IA : réordonne et complète à partir des mots-clés existants."""
    analysis = _analyze(title, tags, description)
    top = [k.keyword for k in analysis["keywords"][:6]]
    notes = ["Optimisation heuristique (aucune clé Anthropic configurée ou IA indisponible)."]

    # Tags : existants (dédoublonnés, ≤ 20 car.) + candidats marché + bigrammes du titre.
    clean_tags: List[str] = []
    for t in tags + extra_keywords:
        t = t.strip().lower()[:ETSY_TAG_LEN_MAX].strip()
        if t and t not in clean_tags and len(t) >= 3:
            clean_tags.append(t)
    words = _tokens(title)
    for i in range(len(words) - 1):
        bigram = f"{words[i]} {words[i + 1]}"
        if len(bigram) <= ETSY_TAG_LEN_MAX and bigram not in clean_tags:
            clean_tags.append(bigram)
    new_tags = clean_tags[:ETSY_TAGS_MAX]
    if len(new_tags) < len(tags):
        new_tags = tags[:ETSY_TAGS_MAX]

    # Titre : mot-clé principal devant si absent des 4 premiers mots.
    new_title = title.strip()
    if top and top[0] not in set(words[:4]):
        lead = next((t for t in new_tags if top[0] in t), top[0]).title()
        new_title = f"{lead} - {new_title}"
        notes.append(f"Mot-clé principal « {top[0]} » déplacé en tête de titre.")
    if len(new_title) < 70 and new_tags:
        for t in new_tags:
            candidate = f"{new_title} | {t.title()}"
            if len(candidate) > ETSY_TITLE_MAX:
                break
            if t.lower() in new_title.lower():
                continue
            new_title = candidate
            if len(new_title) >= 90:
                break
        notes.append("Titre complété avec des tags longue traîne.")
    new_title = new_title[:ETSY_TITLE_MAX].rstrip(" -|")

    # Description : extrait SEO en tête s'il manque le mot-clé principal.
    new_desc = description.strip()
    if top and not (set(_tokens(new_desc[:SEO_SNIPPET])) & set(top[:2])):
        opener = f"{new_title.split('|')[0].strip()} — {', '.join(top[:3])}. "
        new_desc = opener + new_desc
        notes.append("Phrase d'accroche avec mots-clés ajoutée en tête de description.")
    if len(new_desc) < DESCRIPTION_GOOD:
        new_desc += ("\n\n" if new_desc else "") + (
            "Handmade with care and shipped from France. Each order is checked before dispatch. "
            "Perfect as a personalized gift for birthdays, Mother's Day, Christmas or any special occasion."
        )
        notes.append("Paragraphe générique ajouté pour atteindre 300 caractères — à personnaliser.")

    return {"title": new_title, "description": new_desc, "tags": new_tags or ["handmade gift"], "engine": "heuristic", "notes": notes}


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no json")
    return json.loads(text[start : end + 1])


async def _claude_optimize(title: str, tags: List[str], description: str, market_keywords: List[str]) -> Optional[dict]:
    if not ANTHROPIC_API_KEY:
        return None
    system = (
        "You are an Etsy SEO specialist for the US/UK/AU/CA markets. Rewrite listings in ENGLISH ONLY "
        "(never French). Rules: title ≤ 140 characters, main keyword in the first 4 words, no ALL CAPS, "
        "no word repeated more than twice; exactly 13 tags, each ≤ 20 characters, lowercase, mostly 2-3 word "
        "phrases, no duplicates, the 4 most important tags reused verbatim in the title; description 3+ short "
        "paragraphs, first 160 characters contain the main keyword, benefits first, then materials/sizing/"
        "personalization, then shipping & care. Keep every factual detail from the original; never invent "
        "materials, sizes or claims. Reply with a single JSON object and nothing else."
    )
    user_prompt = (
        f"Current title: {title}\n"
        f"Current tags: {', '.join(tags)}\n"
        f"Popular tags in this niche on Etsy: {', '.join(market_keywords[:20]) or 'n/a'}\n"
        f"Current description:\n{description[:3000]}\n\n"
        'Return JSON: {"title": "...", "tags": ["13 tags"], "description": "...", "notes": ["max 3 short notes in French explaining the changes"]}'
    )
    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    try:
        message = await client.messages.create(
            model=SEO_MODEL,
            max_tokens=4000,
            output_config={"effort": "low"},
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:
        logger.warning("Anthropic API indisponible pour l'optimisation SEO : %s", type(exc).__name__)
        return None
    raw = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
    try:
        data = _extract_json(raw)
        new_tags = [str(t).strip().lower()[:ETSY_TAG_LEN_MAX].strip() for t in data.get("tags") or []]
        new_tags = list(dict.fromkeys(t for t in new_tags if t))[:ETSY_TAGS_MAX]
        if not new_tags:
            raise ValueError("no tags")
        return {
            "title": str(data.get("title") or title)[:ETSY_TITLE_MAX],
            "description": str(data.get("description") or description),
            "tags": new_tags,
            "engine": "claude",
            "notes": [str(n)[:200] for n in (data.get("notes") or [])][:3],
        }
    except (ValueError, TypeError, json.JSONDecodeError):
        logger.warning("Réponse Claude invalide pour l'optimisation SEO.")
        return None


@router.post("/optimize/{listing_id}", response_model=SeoOptimizeResponse)
async def optimize_listing_seo(listing_id: str, user: CurrentUser = Depends(get_current_user)):
    listing = _fetch_listing(user.id, listing_id)
    title, tags, description = listing.get("name") or "", listing.get("tags") or [], listing.get("description") or ""

    # Tags populaires de la niche (best-effort) pour nourrir l'optimisation.
    market_keywords: List[str] = []
    try:
        trending = await trending_keywords(listing_id=listing_id, q=None, user=user)
        market_keywords = [k.keyword for k in trending.keywords if k.source == "market"][:20]
    except Exception:
        market_keywords = []

    result = await _claude_optimize(title, tags, description, market_keywords)
    if result is None:
        result = _heuristic_optimize(title, tags, description, market_keywords)

    try:
        return SeoOptimizeResponse(listing_id=listing_id, **result)
    except ValidationError:
        logger.warning("Résultat d'optimisation invalide — repli heuristique.")
        return SeoOptimizeResponse(listing_id=listing_id, **_heuristic_optimize(title, tags, description, market_keywords))


# === APPLIQUER SUR ETSY ===
@router.post("/apply/{listing_id}", response_model=SeoApplyResponse)
async def apply_listing_seo(listing_id: str, payload: SeoApplyRequest, user: CurrentUser = Depends(get_current_user)):
    """
    updateListing Etsy v3 = PATCH /shops/{shop_id}/listings/{listing_id} en
    x-www-form-urlencoded (tags séparés par des virgules). La spec Phase 2
    mentionne PUT ; Etsy renvoie 405 sur PUT pour cette route — voir
    etsy_client.py > etsy_request.
    """
    record = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not record:
        raise HTTPException(status_code=422, detail="Aucun champ à appliquer.")
    listing = _fetch_listing(user.id, listing_id)
    etsy_listing_id = listing.get("etsy_listing_id")
    if not etsy_listing_id:
        raise HTTPException(status_code=400, detail="Cette fiche n'est pas liée à Etsy (créée à la main).")

    if "tags" in record:
        tags = [t.strip()[:ETSY_TAG_LEN_MAX].strip() for t in record["tags"] if t and t.strip()]
        tags = list(dict.fromkeys(tags))[:ETSY_TAGS_MAX]
        if not tags:
            raise HTTPException(status_code=422, detail="Liste de tags vide.")
        record["tags"] = tags

    access_token = await get_etsy_access_token(user.id)
    shop_id = get_etsy_shop_id(user.id)
    form = {}
    if "title" in record:
        form["title"] = record["title"]
    if "description" in record:
        form["description"] = record["description"]
    if "tags" in record:
        form["tags"] = ",".join(record["tags"])
    await etsy_request("PATCH", f"/shops/{shop_id}/listings/{etsy_listing_id}", access_token=access_token, data=form)

    # Alignement local (name = titre côté DB).
    local = {}
    if "title" in record:
        local["name"] = record["title"]
    if "description" in record:
        local["description"] = record["description"][:2000]
    if "tags" in record:
        local["tags"] = record["tags"]
    try:
        get_supabase().table("listings").update(local).eq("id", listing_id).eq("user_id", user.id).execute()
    except Exception as exc:
        logger.warning("Alignement local de la fiche échoué : %s", type(exc).__name__)

    return SeoApplyResponse(listing_id=listing_id, etsy_listing_id=str(etsy_listing_id), updated_fields=sorted(record.keys()))
