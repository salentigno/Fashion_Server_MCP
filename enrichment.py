"""
Enriquecimiento de resultados de tendencias.
--------------------------------------------
Procesa los datos crudos de Google/Wikipedia/RSS y añade:

NIVEL 1 — Metadatos por trend individual:
  - detected_brand: marca identificada en la query
  - brand_tier: 'luxury' | 'premium' | 'mass_market' | 'fast_fashion' | 'unknown'
  - product_type: 'bolso' | 'zapato' | 'joyeria' | 'ropa' | etc.
  - color: si se detecta un color en la query
  - momentum: 'breakout' | 'high' | 'moderate' | 'low' según % crecimiento

NIVEL 2 — Análisis agregado:
  - brand_frequency: qué marcas aparecen más veces en los trends
  - category_breakdown: desglose por tipo de producto
  - top_brands_cross_source: marcas presentes en ≥2 fuentes (Google + Wiki + RSS)
  - momentum_tiers: agrupación de trends por nivel de crecimiento

Todo el procesamiento es local — sin API calls, sin latencia externa.
"""

import re
from collections import Counter
from typing import Any


# ===========================================================================
# Catálogo de marcas con tier (lujo / premium / mass market / fast fashion)
# ===========================================================================

BRAND_CATALOG: dict[str, str] = {
    # Lujo (luxury)
    "louis vuitton": "luxury", "dior": "luxury", "chanel": "luxury",
    "hermes": "luxury", "hermès": "luxury", "gucci": "luxury", "prada": "luxury",
    "miu miu": "luxury", "bottega veneta": "luxury", "saint laurent": "luxury",
    "ysl": "luxury", "cartier": "luxury", "tiffany": "luxury", "bulgari": "luxury",
    "bvlgari": "luxury", "rolex": "luxury", "balenciaga": "luxury",
    "valentino": "luxury", "givenchy": "luxury", "celine": "luxury",
    "fendi": "luxury", "loewe": "luxury", "alexander mcqueen": "luxury",
    "mcqueen": "luxury", "burberry": "luxury", "versace": "luxury",

    # Premium / diseñador contemporáneo
    "michael kors": "premium", "karl lagerfeld": "premium",
    "philipp plein": "premium", "dsquared": "premium", "pinko": "premium",
    "vivienne westwood": "premium", "off-white": "premium",
    "chiara ferragni": "premium", "the attico": "premium",
    "pierre cardin": "premium", "tous": "premium", "pandora": "premium",
    "swarovski": "premium", "pierre hardy": "premium",
    "jimmy choo": "premium", "christian louboutin": "premium",
    "coach": "premium", "kate spade": "premium",

    # Mass market mid-range
    "sandro": "mass_market", "maje": "mass_market", "cos": "mass_market",
    "arket": "mass_market", "massimo dutti": "mass_market",
    "bimba y lola": "mass_market", "uterque": "mass_market",
    "parfois": "mass_market", "desigual": "mass_market",
    "geox": "mass_market", "unisa": "mass_market",
    "pedro miralles": "mass_market",

    # Fast fashion
    "zara": "fast_fashion", "mango": "fast_fashion",
    "stradivarius": "fast_fashion", "bershka": "fast_fashion",
    "pull&bear": "fast_fashion", "pull and bear": "fast_fashion",
    "h&m": "fast_fashion", "primark": "fast_fashion", "shein": "fast_fashion",
    "oysho": "fast_fashion", "lefties": "fast_fashion",

    # Sport / streetwear
    "nike": "premium", "adidas": "premium", "puma": "mass_market",
    "new balance": "premium", "converse": "mass_market",
    "vans": "mass_market", "reebok": "mass_market",
    "under armour": "mass_market",
}


# ===========================================================================
# Palabras clave para detectar tipo de producto
# ===========================================================================

PRODUCT_TYPE_KEYWORDS: dict[str, list[str]] = {
    "bolso": [
        "bolso", "bolsos", "borsa", "borse", "bag", "handbag", "shopper",
        "tote", "pochette", "clutch", "mochila", "backpack", "zaino",
        "cartera", "wallet",
    ],
    "zapato": [
        "zapato", "zapatos", "scarpa", "scarpe", "shoe", "shoes",
        "zapatilla", "zapatillas", "sneaker", "sneakers",
        "bota", "botas", "boot", "stivale", "stivali",
        "sandalia", "sandal", "sandalo",
        "tacon", "tacco", "heel", "mocasin", "loafer",
    ],
    "joyeria": [
        "collar", "collana", "necklace", "anillo", "anello", "ring",
        "pulsera", "bracciale", "bracelet", "pendiente", "orecchin",
        "earring", "reloj", "watch", "orologio", "joya", "gioiell", "jewelry",
    ],
    "ropa": [
        "vestido", "vestito", "dress", "camisa", "shirt", "camicia",
        "falda", "skirt", "gonna", "pantalon", "pants", "pantalone",
        "abrigo", "coat", "cappotto", "jersey", "sweater", "maglione",
        "jeans", "chaqueta", "jacket", "giacca", "blusa", "blouse",
    ],
    "accesorio": [
        "cinturon", "cintura", "belt", "bufanda", "scarf", "sciarpa",
        "sombrero", "hat", "cappello", "gafa", "sunglass", "occhial",
    ],
    "belleza": [
        "perfume", "profumo", "fragrance", "maquillaje", "trucco", "makeup",
        "crema", "labial", "lipstick", "cosmetic",
    ],
}


# ===========================================================================
# Colores detectables en queries (multiidioma)
# ===========================================================================

COLOR_KEYWORDS: dict[str, list[str]] = {
    "blanco": ["blanco", "bianco", "white"],
    "negro": ["negro", "nero", "black"],
    "rojo": ["rojo", "rosso", "red"],
    "azul": ["azul", "blu", "blue"],
    "verde": ["verde", "green"],
    "rosa": ["rosa", "pink"],
    "marron": ["marron", "marrone", "brown", "maron"],
    "beige": ["beige", "nude", "camel"],
    "dorado": ["dorado", "oro", "gold", "dorato"],
    "plateado": ["plateado", "argento", "silver"],
    "gris": ["gris", "grigio", "gray", "grey"],
    "amarillo": ["amarillo", "giallo", "yellow"],
}


# ===========================================================================
# NIVEL 1 — Metadatos por trend
# ===========================================================================


def _to_float_growth(raw: str) -> float:
    """Convierte '+350%', 'Breakout', '40' → número float para ordenar."""
    if raw is None:
        return 0.0
    s = str(raw).lower().strip()
    if "breakout" in s:
        return 99999.0  # los "Breakout" son los más fuertes
    # Quitar símbolos +, % y separadores
    s = s.replace("+", "").replace("%", "").replace(",", "").replace(".", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def detect_brand(query: str) -> str | None:
    """Busca una marca conocida en la query. Devuelve el nombre "canónico"."""
    q = query.lower()
    # Ordenar por longitud descendente para que "alexander mcqueen" gane a "mcqueen"
    for brand in sorted(BRAND_CATALOG.keys(), key=len, reverse=True):
        if brand in q:
            return " ".join(w.capitalize() for w in brand.split())
    return None


def detect_product_type(query: str) -> str | None:
    """Identifica el tipo de producto a partir de palabras clave."""
    q = query.lower()
    for ptype, keywords in PRODUCT_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in q:
                return ptype
    return None


def detect_color(query: str) -> str | None:
    """Detecta un color en la query si existe."""
    q = query.lower()
    for color, variants in COLOR_KEYWORDS.items():
        for v in variants:
            # Matching por palabra completa para evitar falsos positivos tipo "gold" en "goldberg"
            if re.search(rf"\b{re.escape(v)}\b", q):
                return color
    return None


def classify_momentum(growth_raw: str) -> str:
    """Clasifica el momentum según % de crecimiento."""
    n = _to_float_growth(growth_raw)
    if n >= 10000:       # Breakout marcado
        return "breakout"
    if n >= 500:
        return "very_high"
    if n >= 100:
        return "high"
    if n >= 50:
        return "moderate"
    return "low"


def enrich_trend(trend: dict) -> dict:
    """Añade metadatos a un trend individual (Nivel 1)."""
    query = trend.get("query", "")
    growth = trend.get("growth", "")

    brand = detect_brand(query)
    product_type = detect_product_type(query)
    color = detect_color(query)
    momentum = classify_momentum(growth)

    enriched = dict(trend)
    if brand:
        enriched["detected_brand"] = brand
        enriched["brand_tier"] = BRAND_CATALOG.get(brand.lower(), "unknown")
    if product_type:
        enriched["product_type"] = product_type
    if color:
        enriched["color"] = color
    enriched["momentum"] = momentum
    return enriched


# ===========================================================================
# NIVEL 2 — Análisis agregado
# ===========================================================================


def brand_frequency(enriched_trends: list[dict]) -> list[dict]:
    """Lista de marcas que aparecen más veces en los trends, con tier."""
    counter: Counter[str] = Counter()
    for t in enriched_trends:
        b = t.get("detected_brand")
        if b:
            counter[b] += 1

    return [
        {
            "brand": brand,
            "mentions": count,
            "tier": BRAND_CATALOG.get(brand.lower(), "unknown"),
        }
        for brand, count in counter.most_common(10)
    ]


def category_breakdown(enriched_trends: list[dict]) -> dict[str, int]:
    """Cuenta trends por tipo de producto."""
    counter: Counter[str] = Counter()
    for t in enriched_trends:
        ptype = t.get("product_type", "unclassified")
        counter[ptype] += 1
    return dict(counter.most_common())


def momentum_tiers(enriched_trends: list[dict]) -> dict[str, list[str]]:
    """Agrupa las queries por nivel de momentum."""
    tiers: dict[str, list[str]] = {
        "breakout": [], "very_high": [], "high": [], "moderate": [], "low": []
    }
    for t in enriched_trends:
        m = t.get("momentum", "low")
        tiers[m].append(t.get("query", ""))
    return {k: v for k, v in tiers.items() if v}  # solo tiers con contenido


def cross_source_brands(
    enriched_trends: list[dict],
    wikipedia_signals: dict | None,
    editorial_signals: dict | None,
) -> list[dict]:
    """
    Detecta marcas que aparecen en MÚLTIPLES fuentes simultáneamente.
    Una marca presente en Google + Wikipedia + RSS es una señal muy fuerte.
    """
    # Marcas de Google
    google_brands = {
        t["detected_brand"].lower()
        for t in enriched_trends
        if t.get("detected_brand")
    }

    # Marcas de Wikipedia
    wiki_brands = set()
    if wikipedia_signals and wikipedia_signals.get("ranking"):
        for item in wikipedia_signals["ranking"]:
            wiki_brands.add(item.get("brand", "").lower())

    # Marcas de RSS (buscando nombres en los títulos)
    rss_brands = set()
    if editorial_signals and editorial_signals.get("items_by_source"):
        for items in editorial_signals["items_by_source"].values():
            for it in items:
                title_low = (it.get("title") or "").lower()
                for brand in BRAND_CATALOG:
                    if brand in title_low:
                        rss_brands.add(brand)

    # Consolidar: brand → lista de fuentes donde aparece
    all_brands = google_brands | wiki_brands | rss_brands
    cross: list[dict] = []
    for brand in all_brands:
        sources = []
        if brand in google_brands:
            sources.append("google")
        if brand in wiki_brands:
            sources.append("wikipedia")
        if brand in rss_brands:
            sources.append("editorial")
        if len(sources) >= 2:  # solo las que aparecen en ≥2 fuentes
            cross.append({
                "brand": " ".join(w.capitalize() for w in brand.split()),
                "sources": sources,
                "source_count": len(sources),
                "tier": BRAND_CATALOG.get(brand, "unknown"),
            })

    cross.sort(key=lambda x: (-x["source_count"], x["brand"]))
    return cross


# ===========================================================================
# Entry point
# ===========================================================================


def enrich_results(results: dict) -> dict:
    """
    Aplica Nivel 1 (metadatos por trend) y Nivel 2 (análisis agregado)
    a un diccionario de resultados ya formado.

    Modifica `results` in-place añadiendo:
      - metadatos a cada trend existente
      - clave "enrichment" con análisis agregado
      - clave "suggested_angle" con un ángulo rotativo para evitar repetición
    """
    import random as _rnd

    trends = results.get("trends", [])
    if not trends and not results.get("wikipedia_signals") and not results.get("editorial_signals"):
        return results  # nada que enriquecer

    # NIVEL 1: metadatos por trend
    enriched_trends = [enrich_trend(t) for t in trends]
    results["trends"] = enriched_trends

    # Ordenar trends por momentum (breakouts arriba)
    momentum_order = {
        "breakout": 0, "very_high": 1, "high": 2, "moderate": 3, "low": 4
    }
    results["trends"].sort(
        key=lambda t: (momentum_order.get(t.get("momentum", "low"), 5),
                       -_to_float_growth(t.get("growth", "0")))
    )

    # NIVEL 2: análisis agregado
    enrichment = {
        "brand_frequency": brand_frequency(enriched_trends),
        "category_breakdown": category_breakdown(enriched_trends),
        "momentum_tiers": momentum_tiers(enriched_trends),
    }

    # Cross-source: solo si hay datos de ≥2 fuentes
    wiki_sig = results.get("wikipedia_signals")
    edit_sig = results.get("editorial_signals")
    if wiki_sig or edit_sig:
        cross = cross_source_brands(enriched_trends, wiki_sig, edit_sig)
        if cross:
            enrichment["cross_source_brands"] = cross

    # Highlight: top-3 trends por momentum (resumen rápido para el LLM)
    top3 = enriched_trends[:3] if enriched_trends else []
    if top3:
        enrichment["top3_highlights"] = [
            {
                "query": t["query"],
                "growth": t.get("growth"),
                "momentum": t.get("momentum"),
                "brand": t.get("detected_brand"),
                "tier": t.get("brand_tier"),
                "product_type": t.get("product_type"),
            }
            for t in top3
        ]

    # ROTACIÓN DE ÁNGULO — sugerimos al LLM un foco distinto cada vez
    # para evitar que repita el mismo análisis en cada turno.
    ANGLES = [
        "price_tier_distribution",       # enfoca en tiers de marca (lujo vs fast fashion)
        "momentum_vs_maturity",          # contrapone marcas en alza vs consolidadas
        "category_saturation",           # qué categorías están saturadas vs emergentes
        "cross_source_validation",       # qué tiene triple validación
        "regional_specificity",          # qué es único de esta región
        "color_and_material_signals",    # pistas estéticas visibles en las queries
        "brand_momentum_divergence",     # marcas con mismo tier pero dirección opuesta
        "editorial_vs_search_gap",       # dónde editorial y búsqueda divergen
    ]
    enrichment["suggested_angle"] = _rnd.choice(ANGLES)
    enrichment["analysis_instruction"] = (
        f"This turn, emphasize the '{enrichment['suggested_angle']}' angle. "
        f"Do NOT repeat the same framing as previous turns. "
        f"Find a fresh interpretation of the data centered on this dimension."
    )

    results["enrichment"] = enrichment
    return results
