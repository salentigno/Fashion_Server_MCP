"""
Fashion Trends MCP Server
-------------------------
Servidor MCP que expone herramientas para obtener tendencias de moda desde:
  1. Google Trends (via pytrends) - tendencias de búsqueda globales/regionales
  2. Reddit API oficial - conversaciones en subreddits de moda

Uso con Claude Desktop: añade este servidor a claude_desktop_config.json
(ver README.md incluido).
"""

import asyncio
import os
import json
import time
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# --- Dependencias de terceros ---
# pip install "mcp[cli]" pytrends praw python-dotenv
from pytrends.request import TrendReq
import praw
from dotenv import load_dotenv

# Fuentes alternativas (Wikipedia, RSS de medios de moda)
from alt_sources import (
    get_wikipedia_pageviews,
    compare_brands_wikipedia,
    get_fashion_editorial,
)

# GDELT: prensa global, 100k+ medios en 65 idiomas
from gdelt_source import (
    brand_gdelt_full_signal,
    compare_brands_gdelt,
    get_brand_media_volume,
    get_brand_recent_articles,
)

# eBay: datos reales de comercio (productos, precios, listings, vendedores)
from ebay_source import (
    search_ebay_products,
    get_ebay_brand_signal,
    compare_brands_ebay,
)

# Etsy: marketplace artesanal/handmade/vintage — complemento europeo a eBay
from etsy_source import (
    search_etsy_products,
    get_etsy_brand_signal,
    compare_brands_etsy,
    get_etsy_trending_in_category,
)

# Search alternatives: Bing, DuckDuckGo y Reddit search reforzado
# como sustitutos de Google Trends cuando la IP está bloqueada
from search_alternatives import (
    get_bing_keyword_volume,
    get_bing_related_keywords,
    compare_bing_keywords,
    get_duckduckgo_suggestions,
    compare_duckduckgo_keywords,
    reddit_search_volume,
    reddit_compare_terms,
    search_alternatives_for_keyword,
)

# Enriquecimiento de resultados (metadatos + análisis agregado)
from enrichment import enrich_results

# Carga el .env que esté junto a este archivo, sin depender del cwd
load_dotenv(dotenv_path=Path(__file__).parent / ".env")


def log(msg: str) -> None:
    """Log a stderr (no rompe el protocolo JSON-RPC de stdout)."""
    print(f"[fashion-trends] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Configuración de pytrends con anti-rate-limit
# ---------------------------------------------------------------------------
#
# NOTA: NO usamos retries=/backoff_factor= en TrendReq porque provocan un bug
# con urllib3 >= 2.x ('method_whitelist' deprecated). Los reintentos los
# gestionamos nosotros manualmente más abajo.
# timeout=(10, 25) = 10s conectar, 25s leer respuesta.
# requests_args permite pasarle headers de navegador real.
pytrends = TrendReq(
    hl="es-ES",
    tz=60,
    timeout=(10, 25),
    requests_args={
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
    },
)

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

# Google Trends no necesita API key (configurado arriba con anti-rate-limit)

# Reddit necesita credenciales (gratis en https://www.reddit.com/prefs/apps)
reddit = praw.Reddit(
    client_id=os.getenv("REDDIT_CLIENT_ID"),
    client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
    user_agent=os.getenv("REDDIT_USER_AGENT", "fashion-trends-mcp/0.1"),
)

FASHION_SUBREDDITS = [
    "femalefashionadvice",
    "malefashionadvice",
    "streetwear",
    "OUTFITS",
    "fashion",
    "TheDevilWearsZara",
]

# ---------------------------------------------------------------------------
# Caché en disco para Google Trends (reduce llamadas reales a Google)
# ---------------------------------------------------------------------------

CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL_MINUTES = 60  # cuánto dura un resultado en caché


def _cache_key(seed: str, region: str, timeframe: str) -> Path:
    """Ruta del fichero de caché para esta combinación.
    
    Sanitiza caracteres prohibidos en Windows (< > : " / \\ | ? *).
    """
    raw = f"{seed}_{region}_{timeframe}"
    # Sustituir todos los caracteres problemáticos por guión bajo
    safe = raw
    for ch in [" ", "/", "\\", "|", ":", "*", "?", '"', "<", ">", ",", "'"]:
        safe = safe.replace(ch, "_")
    # Evitar múltiples guiones seguidos
    while "__" in safe:
        safe = safe.replace("__", "_")
    return CACHE_DIR / f"{safe}.json"


def cache_get(seed: str, region: str, timeframe: str) -> list | None:
    """Devuelve resultados cacheados si existen y no han caducado."""
    path = _cache_key(seed, region, timeframe)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["timestamp"])
        if datetime.now() - ts < timedelta(minutes=CACHE_TTL_MINUTES):
            log(f"CACHE HIT: {seed}/{region}/{timeframe}")
            return data["trends"]
    except Exception as e:
        log(f"Caché corrupta para {seed}: {e}")
    return None


def cache_set(seed: str, region: str, timeframe: str, trends: list) -> None:
    """Guarda resultados en caché."""
    path = _cache_key(seed, region, timeframe)
    try:
        path.write_text(
            json.dumps(
                {"timestamp": datetime.now().isoformat(), "trends": trends},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        log(f"No se pudo escribir caché: {e}")


# ---------------------------------------------------------------------------
# Cache NEGATIVO — recuerda queries fallidas recientes para no reintentar
# ---------------------------------------------------------------------------
# TTL corto (10 minutos) porque los problemas con Google Trends suelen ser
# transitorios. Si la misma combinación falla 2 veces en 10 min, la tercera
# vez saltamos directo a fallbacks sin ir a Google.

NEGATIVE_CACHE_TTL_MIN = 10
_negative_cache: dict[str, datetime] = {}  # key → timestamp del último fallo


def _neg_cache_key(seed: str, region: str, timeframe: str, category: int) -> str:
    return f"{seed}|{region}|{timeframe}|{category}"


def negative_cache_check(seed: str, region: str, timeframe: str, category: int) -> bool:
    """Devuelve True si esta combinación falló recientemente (salta Google)."""
    key = _neg_cache_key(seed, region, timeframe, category)
    ts = _negative_cache.get(key)
    if ts is None:
        return False
    if datetime.now() - ts > timedelta(minutes=NEGATIVE_CACHE_TTL_MIN):
        del _negative_cache[key]
        return False
    return True


def negative_cache_mark(seed: str, region: str, timeframe: str, category: int) -> None:
    """Marca una combinación como fallida recientemente."""
    key = _neg_cache_key(seed, region, timeframe, category)
    _negative_cache[key] = datetime.now()


# ---------------------------------------------------------------------------
# Cache de RESPUESTAS COMPLETAS para brand_full_intelligence y fashion_trends_summary
# ---------------------------------------------------------------------------
# A diferencia del cache de seeds individuales, esto cachea la respuesta entera
# de la tool agregadora durante 30 minutos. Si el usuario hace la misma consulta
# varias veces seguidas, las repeticiones devuelven en milisegundos.

FULL_RESPONSE_CACHE_TTL_MIN = 30
_full_response_cache: dict[str, tuple[datetime, dict]] = {}


def _full_cache_key(*parts) -> str:
    """Genera una clave estable a partir de los parámetros relevantes."""
    return "|".join(str(p).lower().strip() for p in parts)


def full_cache_get(key: str) -> dict | None:
    """Recupera una respuesta cacheada si está dentro del TTL."""
    entry = _full_response_cache.get(key)
    if entry is None:
        return None
    timestamp, data = entry
    if datetime.now() - timestamp > timedelta(minutes=FULL_RESPONSE_CACHE_TTL_MIN):
        del _full_response_cache[key]
        return None
    return data


def full_cache_set(key: str, data: dict) -> None:
    """Guarda una respuesta completa en cache."""
    _full_response_cache[key] = (datetime.now(), data)
    # Auto-limpieza: si el cache crece más de 100 entradas, borra las 30 más viejas
    if len(_full_response_cache) > 100:
        sorted_entries = sorted(_full_response_cache.items(), key=lambda x: x[1][0])
        for old_key, _ in sorted_entries[:30]:
            del _full_response_cache[old_key]


# ---------------------------------------------------------------------------
# Datos de fallback para demos — se usan cuando Google devuelve 429
# ---------------------------------------------------------------------------
#
# Basados en tendencias reales recogidas en pruebas anteriores. Sirven para
# que la demo NUNCA se quede sin respuesta aunque Google bloquee la IP.
#
# Cada entrada es: (keywords_que_la_activan, región, lista de trends)
# El servidor busca la primera que coincida con la petición.

DEMO_DATA: list[dict] = [
    # Joyería / Accesorios — ES
    {
        "match_seeds": ["collar", "collana", "anillo", "pulsera", "joya", "pendiente"],
        "match_region": ["ES", "IT"],
        "trends": [
            {"seed": "demo_joyeria", "query": "pandora collar corazon", "growth": "350"},
            {"seed": "demo_joyeria", "query": "swarovski collana cuore", "growth": "280"},
            {"seed": "demo_joyeria", "query": "tous anillo oso", "growth": "220"},
            {"seed": "demo_joyeria", "query": "pulsera cuerda mujer", "growth": "190"},
            {"seed": "demo_joyeria", "query": "pendientes aro oro", "growth": "150"},
        ],
    },
    # Swarovski específico
    {
        "match_seeds": ["swarovski"],
        "match_region": ["ES", "IT", "FR", "US"],
        "trends": [
            {"seed": "demo_swarovski", "query": "swarovski collana lucent", "growth": "420"},
            {"seed": "demo_swarovski", "query": "swarovski orecchini matrix", "growth": "310"},
            {"seed": "demo_swarovski", "query": "swarovski anello constella", "growth": "265"},
            {"seed": "demo_swarovski", "query": "swarovski bracciale dulcis", "growth": "180"},
            {"seed": "demo_swarovski", "query": "swarovski meteora collection", "growth": "140"},
        ],
    },
    # Ropa — ES
    {
        "match_seeds": ["vestido", "camisa", "falda", "pantalon", "abrigo"],
        "match_region": ["ES"],
        "trends": [
            {"seed": "demo_ropa", "query": "vestido satinado zara", "growth": "290"},
            {"seed": "demo_ropa", "query": "mango vestido rayas", "growth": "180"},
            {"seed": "demo_ropa", "query": "abrigo tweed mujer", "growth": "160"},
            {"seed": "demo_ropa", "query": "falda midi plisada", "growth": "120"},
        ],
    },
    # Calzado — ES / IT
    {
        "match_seeds": ["zapato", "zapatilla", "bota", "scarpa", "sneaker"],
        "match_region": ["ES", "IT"],
        "trends": [
            {"seed": "demo_calzado", "query": "zapatos stradivarius", "growth": "410"},
            {"seed": "demo_calzado", "query": "alexander mcqueen zapatillas", "growth": "360"},
            {"seed": "demo_calzado", "query": "botas miu miu", "growth": "310"},
            {"seed": "demo_calzado", "query": "zapatillas barefoot", "growth": "190"},
        ],
    },
    # Bolsos
    {
        "match_seeds": ["bolso", "borsa", "bag", "mochila"],
        "match_region": ["ES", "IT", "US"],
        "trends": [
            {"seed": "demo_bolsos", "query": "bolso blanco stradivarius", "growth": "420"},
            {"seed": "demo_bolsos", "query": "bolso pierre cardin", "growth": "310"},
            {"seed": "demo_bolsos", "query": "bolso yves saint laurent", "growth": "180"},
            {"seed": "demo_bolsos", "query": "shopper desigual", "growth": "140"},
        ],
    },
]


def get_demo_fallback(seeds: list[str], region: str) -> list[dict] | None:
    """Busca datos de demo que coincidan con las semillas y región."""
    seeds_low = " ".join(seeds).lower()
    for entry in DEMO_DATA:
        if region not in entry["match_region"]:
            continue
        if any(m in seeds_low for m in entry["match_seeds"]):
            log(f"🎭 DEMO FALLBACK activado para '{seeds}' en {region}")
            return entry["trends"]
    return None


DEMO_FALLBACK_ENABLED = os.getenv("DEMO_FALLBACK", "true").lower() == "true"


# Normalización de códigos de región.
# Google Trends exige ISO 3166-1 alpha-2 estricto y rechaza alias como "UK" con 400.
REGION_NORMALIZATION = {
    "UK": "GB",   # Reino Unido: UK → GB (oficial ISO)
    "EN": "GB",   # A veces se usa EN por "Inglaterra"
    "UE": "",     # "Unión Europea" → sin filtro (Google no tiene código EU)
    "EU": "",
    "WORLD": "",  # global
    "GLOBAL": "",
}


def normalize_region(region: str) -> str:
    """Convierte códigos informales a ISO válidos para Google Trends."""
    if not region:
        return ""
    up = region.upper().strip()
    return REGION_NORMALIZATION.get(up, up)


# ---------------------------------------------------------------------------
# Auto-detección de categoría Google Trends según la semilla
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS: dict[int, list[str]] = {
    # Joyería: usamos 0 (todas) porque 186 devuelve 400 en Google Trends
    # actual. Con cat=0 y seeds específicas tipo "collar", Google aún
    # filtra bien por el contexto de búsqueda.
    0: [
        "collar", "collares", "collana", "collane", "necklace",
        "anillo", "anello", "ring", "pendiente", "pendientes",
        "earring", "pulsera", "bracciale", "bracelet",
        "reloj", "watch", "orologio", "joya", "gioiell", "jewelry",
    ],
    # 1036 = Bolsos y Equipaje
    1036: [
        "bolso", "bolsos", "borsa", "borse", "bag", "handbag",
        "mochila", "zaino", "backpack", "maleta", "valigia", "luggage",
        "cartera", "portafoglio", "wallet", "shopper", "tote",
    ],
    # 1076 = Calzado
    1076: [
        "zapato", "zapatos", "scarpe", "scarpa", "shoe", "shoes",
        "zapatilla", "zapatillas", "sneaker", "sneakers",
        "bota", "botas", "stivale", "stivali", "boot", "boots",
        "sandalia", "sandalo", "sandal", "tacon", "tacco", "heel",
    ],
    # 44 = Belleza
    44: [
        "perfume", "profumo", "fragrance",
        "maquillaje", "trucco", "makeup",
        "crema", "cream", "labial", "lipstick", "rossetto",
        "cosmetic", "cosmetico", "skincare",
    ],
}


def auto_detect_category(seeds: list[str]) -> int | None:
    """
    Intenta deducir la categoría Google Trends más adecuada a partir
    de las semillas. Devuelve el ID si encuentra match claro, None si no.
    """
    if not seeds:
        return None

    scores: dict[int, int] = {}
    for seed in seeds:
        seed_low = seed.lower()
        for cat_id, keywords in CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw in seed_low:
                    scores[cat_id] = scores.get(cat_id, 0) + 1
                    break  # 1 match por seed por categoría

    if not scores:
        return None
    # Si todas las seeds apuntan claramente a la misma categoría, úsala
    best_cat, best_score = max(scores.items(), key=lambda x: x[1])
    if best_score >= max(1, len(seeds) // 2):
        return best_cat
    return None


# ---------------------------------------------------------------------------
# Expansión automática de seeds
# ---------------------------------------------------------------------------
# Si el cliente manda pocas semillas (1-2), ampliamos con variantes de la misma
# "familia de producto" para aumentar la probabilidad de obtener datos. Ejemplo:
# si el cliente pide solo ["borse"], el servidor también consulta "borsa" y
# "pochette", que suelen traer más rising queries en italiano.

SEED_EXPANSIONS: dict[str, list[str]] = {
    # Italiano
    "borsa": ["borse", "pochette", "shopper"],
    "borse": ["borsa", "pochette", "shopper"],
    "scarpa": ["scarpe", "sneakers", "stivali"],
    "scarpe": ["sneakers", "stivali", "scarpa"],
    "collana": ["collane", "gioielli", "orecchini"],
    "collane": ["collana", "gioielli", "orecchini"],
    "vestito": ["vestiti", "abito", "abiti"],
    # Español
    "bolso": ["bolsos", "cartera", "mochila"],
    "bolsos": ["bolso", "cartera", "shopper"],
    "zapato": ["zapatos", "zapatillas", "botas"],
    "zapatos": ["zapatillas", "botas", "tacones"],
    "vestido": ["vestidos", "falda", "blusa"],
    "collar": ["collares", "pendientes", "pulsera"],
    # Inglés
    "bag": ["bags", "handbag", "tote"],
    "bags": ["handbag", "tote", "purse"],
    "shoe": ["shoes", "sneakers", "boots"],
    "shoes": ["sneakers", "boots", "heels"],
    "necklace": ["necklaces", "earrings", "jewelry"],
}


def expand_seeds_if_few(seeds: list[str]) -> list[str]:
    """Si hay 1-2 seeds, añade variantes relacionadas. Si ya hay 3+, no toca."""
    if len(seeds) >= 3:
        return seeds

    expanded = list(seeds)
    for seed in seeds:
        key = seed.lower().strip()
        if key in SEED_EXPANSIONS:
            for variant in SEED_EXPANSIONS[key]:
                if variant not in [s.lower() for s in expanded]:
                    expanded.append(variant)
                    if len(expanded) >= 4:  # máximo 4 tras expansión
                        return expanded
    return expanded

# ---------------------------------------------------------------------------
# Lógica de negocio
# ---------------------------------------------------------------------------


def _search_alt_for_seeds(seeds: list[str], region: str = "US") -> dict:
    """
    FALLBACK: cuando Google da error/vacío, lanza Bing + DuckDuckGo + Reddit
    para cada seed en paralelo y traduce los resultados al formato `trends`
    compatible con la salida de Google Trends.

    Devuelve:
      - alt_trends: lista de trends en formato {"seed", "query", "growth", "type", "source"}
      - alt_signals: dict con métricas por seed (volumen Bing, etc.)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    alt_trends = []
    alt_signals = {}

    def _process_one_seed(seed: str) -> tuple[str, dict]:
        return seed, search_alternatives_for_keyword(
            keyword=seed, region=region, reddit_instance=reddit
        )

    with ThreadPoolExecutor(max_workers=min(len(seeds), 4)) as executor:
        futures = {executor.submit(_process_one_seed, s): s for s in seeds}
        for future in as_completed(futures):
            try:
                seed, data = future.result(timeout=20)
            except Exception as e:
                log(f"   ⚠️  Alternativa falló para una seed: {e}")
                continue

            summary = data.get("summary", {}) if isinstance(data, dict) else {}

            # Guardar señales por seed (volumen Bing, engagement Reddit)
            alt_signals[seed] = {
                "bing_volume": summary.get("bing_volume"),
                "reddit_mentions": summary.get("reddit_mentions"),
                "reddit_engagement": summary.get("reddit_engagement"),
            }

            # 1) Traducir las sugerencias de DuckDuckGo a "trends"
            #    Las sugerencias son señales de qué busca la gente asociado
            #    al seed → equivalente a las "related queries" de Google
            for sug in summary.get("ddg_top_suggestions", [])[:5]:
                alt_trends.append({
                    "seed": seed,
                    "query": sug,
                    "growth": "associated",
                    "type": "ddg_suggestion",
                    "source": "duckduckgo",
                    "category_used": 0,
                    "region_used": region,
                })

            # 2) Traducir el volumen Bing como un trend cuantitativo
            bing_vol = summary.get("bing_volume")
            if bing_vol and bing_vol > 0:
                alt_trends.append({
                    "seed": seed,
                    "query": seed,
                    "growth": str(bing_vol),
                    "type": "bing_monthly_volume",
                    "source": "bing_webmaster",
                    "category_used": 0,
                    "region_used": region,
                })

            # 3) Reddit: posts más relevantes como señal cultural
            reddit_data = data.get("reddit", {}) if isinstance(data, dict) else {}
            top_posts = reddit_data.get("top_posts", []) if isinstance(reddit_data, dict) else []
            for post in top_posts[:3]:
                alt_trends.append({
                    "seed": seed,
                    "query": post.get("title", "")[:100],
                    "growth": str(post.get("score", 0)),
                    "type": "reddit_post",
                    "source": "reddit_search",
                    "subreddit": post.get("subreddit"),
                    "comments": post.get("comments"),
                    "url": post.get("url"),
                    "category_used": 0,
                    "region_used": region,
                })

    return {
        "alt_trends": alt_trends,
        "alt_signals": alt_signals,
    }


def get_google_trending_fashion(
    region: str = "ES",
    timeframe: str = "now 7-d",
    seeds: list[str] | None = None,
    category: int | None = None,
    fast_mode: bool = False,
    enrich: bool = True,
    expand_with_competitors: bool = True,
) -> dict:
    """
    Busca términos de moda en tendencia vía Google Trends, con caché en disco.

    region: código país ISO
    timeframe: ventana temporal
    seeds: lista opcional de semillas
    category: ID explícito de categoría
    fast_mode: si True, omite reintentos tras 429 y usa delays mínimos
    enrich: si True (default), añade metadatos y análisis agregado
    expand_with_competitors: si True (default), cuando Google falla y hay una
            marca detectada en las seeds, consulta fuentes alternativas TAMBIÉN
            para 2-3 competidoras aleatorias. Útil para comparativas generales.
            PÁSALO A FALSE cuando el usuario pregunte por UNA marca específica
            (típicamente desde brand_deep_dive).
    """
    # Solo 3 semillas por defecto para minimizar riesgo de 429
    seed_terms = seeds or ["vestido", "zapatillas", "bolso"]

    # Normalizar código de región (UK → GB, EN → GB, EU → "")
    original_region = region
    region = normalize_region(region)
    if region != original_region:
        log(f"🔧 Región normalizada: {original_region} → {region}")

    # Expansión automática: si el cliente mandó pocas seeds, añadimos variantes
    # de la misma familia para aumentar probabilidad de obtener datos útiles
    if seeds and len(seed_terms) < 3:
        expanded = expand_seeds_if_few(seed_terms)
        if len(expanded) > len(seed_terms):
            log(f"🌱 Seeds expandidas: {seed_terms} → {expanded}")
            seed_terms = expanded

    # Auto-detección de categoría si el caller no la forzó
    if category is None:
        detected = auto_detect_category(seed_terms)
        if detected is not None:
            category = detected
            log(f"🎯 Auto-categoría detectada: {category} para seeds={seed_terms}")
        else:
            category = 185
            log(f"📦 Sin match claro, usando cat=185 (Apparel)")

    # Ajustes de timing según modo
    # Política: NO reintentamos Google. Un solo intento por seed.
    # Si falla o viene vacío → caemos directamente a fuentes alternativas.
    if fast_mode:
        inter_seed_pause = (0.3, 0.8)
        log("⚡ fast_mode activo: delays mínimos")
    else:
        inter_seed_pause = (1.5, 3.0)  # pausa suave entre seeds

    results: dict[str, Any] = {
        "region": region,
        "timeframe": timeframe,
        "category": category,
        "fast_mode": fast_mode,
        "trends": [],
        "errors": [],
        "cache_hits": 0,
        "live_queries": 0,
    }

    # Flags que se activan al primer 429 o 400 sistémico — abortan el resto
    # de consultas a Google para esta petición
    google_rate_limited = False
    google_bad_request_streak = 0  # contador de 400 consecutivos
    google_empty_streak = 0        # contador de "sin datos" consecutivos
    EMPTY_STREAK_THRESHOLD = 2     # tras 2 vacíos, asumimos que Google no tiene datos

    for i, seed in enumerate(seed_terms):
        # Si ya vimos un 429, no tocamos más Google en esta consulta
        if google_rate_limited:
            log(f"⏭️  Saltando '{seed}' (Google rate-limited en esta consulta)")
            continue

        # Si hemos tenido 2 x 400 seguidos, asumimos que el problema no es
        # la query sino la combinación región/categoría y abortamos también
        if google_bad_request_streak >= 2:
            log(f"⏭️  Saltando '{seed}' (2+ errores 400 seguidos — problema sistémico)")
            continue

        # Si hemos tenido 2 seeds seguidas SIN DATOS, Google no tiene info
        # útil para esta combinación → saltamos al fallback (Wikipedia/GDELT/RSS)
        if google_empty_streak >= EMPTY_STREAK_THRESHOLD:
            log(f"⏭️  Saltando '{seed}' (2+ seeds vacías — Google sin datos para esta combinación)")
            continue

        # 1) Intentar caché positivo primero (clave incluye categoría)
        cache_sig = f"{seed}__cat{category}"
        cached = cache_get(cache_sig, region, timeframe)
        if cached is not None:
            results["trends"].extend(cached)
            results["cache_hits"] += 1
            continue

        # 1b) Intentar caché negativo: ¿esta combinación falló hace poco?
        if negative_cache_check(seed, region, timeframe, category):
            log(f"🚫 '{seed}': en caché negativo (fallo reciente), saltamos Google")
            results.setdefault("skipped_negative_cache", []).append(seed)
            continue

        # 2) Si no hay caché, consulta real a Google con delay previo
        if results["live_queries"] > 0:
            pause = random.uniform(*inter_seed_pause)
            log(f"Esperando {pause:.1f}s antes de '{seed}'...")
            time.sleep(pause)

        seed_trends: list[dict] = []
        current_cat = category
        current_region = region
        attempted_no_region = False
        # UN solo intento por seed con fallbacks inteligentes: si 400,
        # intenta sin categoría; si 400 otra vez, intenta sin región.
        for attempt in range(3):
            try:
                log(f"Consultando Google: '{seed}' cat={current_cat} region={current_region or 'global'}")
                pytrends.build_payload(
                    [seed], cat=current_cat, timeframe=timeframe, geo=current_region
                )
                related = pytrends.related_queries()
                rising = related.get(seed, {}).get("rising")
                top = related.get(seed, {}).get("top")

                source_df = None
                source_type = None
                if rising is not None and not rising.empty:
                    source_df = rising
                    source_type = "rising"
                elif top is not None and not top.empty:
                    source_df = top
                    source_type = "top"
                    log(f"'{seed}': sin rising, usando top queries")

                if source_df is not None:
                    for _, row in source_df.head(10).iterrows():
                        seed_trends.append(
                            {
                                "seed": seed,
                                "query": row["query"],
                                "growth": str(row["value"]),
                                "type": source_type,
                                "category_used": current_cat,
                                "region_used": current_region or "global",
                            }
                        )
                    log(f"'{seed}': {len(seed_trends)} {source_type} queries")
                    google_bad_request_streak = 0  # éxito → reseteamos
                    google_empty_streak = 0
                else:
                    log(f"'{seed}': sin datos → pasa a siguiente seed")
                    google_empty_streak += 1
                break
            except Exception as e:
                msg = str(e)
                # 429 → abortamos TODAS las siguientes seeds en esta consulta
                if "429" in msg:
                    log(f"'{seed}': Google 429 → ABORTAMOS resto de seeds, vamos a alternativas")
                    results["errors"].append(f"{seed}: {msg[:80]}")
                    google_rate_limited = True
                    results["google_rate_limited"] = True
                    break
                # 400 con categoría específica → prueba cat=0
                if "400" in msg and current_cat != 0:
                    log(f"'{seed}': 400 con cat={current_cat}, probando cat=0...")
                    current_cat = 0
                    continue
                # 400 con cat=0 y región específica → prueba sin región
                if "400" in msg and current_region and not attempted_no_region:
                    log(f"'{seed}': 400 con region={current_region}, probando global...")
                    current_region = ""
                    attempted_no_region = True
                    continue
                # Cualquier otro error → pasa a siguiente seed
                code = "400" if "400" in msg else "ERR"
                log(f"'{seed}': Google {code} → pasa a siguiente seed")
                results["errors"].append(f"{seed}: {msg[:80]}")
                if code == "400":
                    google_bad_request_streak += 1
                break

        results["live_queries"] += 1
        results["trends"].extend(seed_trends)
        if not any(seed in e for e in results["errors"]):
            cache_set(cache_sig, region, timeframe, seed_trends)

    if not results["errors"]:
        results.pop("errors")

    # FALLBACK AUTOMÁTICO a fuentes alternativas
    # Política simple: si Google dio menos de 3 trends, enriquecemos con Wiki + RSS.
    # No importa si fue por 429, vacío o error — cualquier falla dispara las alternativas.
    needs_fallback = len(results["trends"]) < 3

    if needs_fallback:
        log(f"🔄 Google dio {len(results['trends'])} trends. Activando alternativas (Bing+DDG+Reddit) + Wikipedia + GDELT + RSS...")

        # Lanzamos TODAS las fuentes EN PARALELO para minimizar latencia total.
        # Antes: secuencial ~30-45s
        # Ahora: paralelo ~15-20s
        from concurrent.futures import ThreadPoolExecutor, as_completed

        brand_candidates = extract_brand_candidates(
            seed_terms, include_competitors=expand_with_competitors
        )

        parallel_tasks = {}

        if brand_candidates:
            log(f"📚 + 🌍 Consultando Wikipedia y GDELT para: {brand_candidates}")
            parallel_tasks["wikipedia_signals"] = lambda: compare_brands_wikipedia(
                brand_candidates, region, months=3
            )
            parallel_tasks["gdelt_signals"] = lambda: compare_brands_gdelt(
                brand_candidates, region=region, timespan="7d"
            )

        log("📰 + RSS de medios de moda en paralelo...")
        parallel_tasks["editorial_signals"] = lambda: get_fashion_editorial(
            region=region, items_per_source=3
        )

        # Search alternatives — Bing + DDG + Reddit por cada seed que falló
        # Es el sustituto directo de Google Trends para keywords concretas
        log(f"🔍 + Bing/DDG/Reddit para {len(seed_terms)} seeds...")
        parallel_tasks["search_alt_per_seed"] = lambda: _search_alt_for_seeds(
            seed_terms, region or "US"
        )

        with ThreadPoolExecutor(max_workers=4) as executor:
            future_to_key = {executor.submit(fn): key for key, fn in parallel_tasks.items()}
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    data = future.result(timeout=25)
                    # Solo guardar si tiene datos reales
                    if key == "wikipedia_signals" and data.get("ranking"):
                        results[key] = data
                    elif key == "gdelt_signals" and data.get("ranking"):
                        results[key] = data
                    elif key == "editorial_signals" and data.get("total_items", 0) > 0:
                        results[key] = data
                    elif key == "search_alt_per_seed":
                        # Traducir resultados de Bing/DDG/Reddit al formato `trends`
                        # que el bot espera. Cada sugerencia DDG y mención Reddit se
                        # convierte en un trend con su seed asociada.
                        alt_trends = data.get("alt_trends", [])
                        if alt_trends:
                            results["trends"].extend(alt_trends)
                            results["alt_search_signals"] = data.get("alt_signals", {})
                            log(f"   ✅ +{len(alt_trends)} trends desde alternativas")
                except Exception as e:
                    log(f"{key} falló: {e}")

        # 3) Si seguimos sin nada útil, demo fallback (último recurso)
        if (
            DEMO_FALLBACK_ENABLED
            and len(results["trends"]) == 0
            and "wikipedia_signals" not in results
            and "editorial_signals" not in results
            and "gdelt_signals" not in results
        ):
            demo_trends = get_demo_fallback(seed_terms, region)
            if demo_trends:
                results["trends"] = demo_trends
                results["demo_fallback"] = True
                results["note"] = (
                    "⚠️  Google + Wikipedia + GDELT + RSS sin resultados. Usando datos de demo."
                )

    log(
        f"Total: {len(results['trends'])} trends | "
        f"Cache hits: {results['cache_hits']} | Live: {results['live_queries']}"
    )

    # ENRIQUECIMIENTO — metadatos por trend + análisis agregado
    if enrich:
        try:
            results = enrich_results(results)
            log(f"✨ Enriquecimiento aplicado")
        except Exception as e:
            log(f"⚠️  Error en enrichment: {e}")

    return results


def extract_brand_candidates(
    seeds: list[str], include_competitors: bool = True
) -> list[str]:
    """
    Heurística para extraer posibles nombres de marca de las seeds.

    include_competitors:
      - True (default): añade 2-3 competidoras aleatorias de la misma categoría
        para diversificar el análisis comparativo. Útil cuando el usuario pregunta
        por una categoría o varias marcas.
      - False: devuelve SOLO las marcas explícitamente detectadas en las seeds.
        Usar cuando el usuario pregunta por UNA marca específica y quieres la
        respuesta enfocada en ella.
    """
    import random as _rnd

    # Marcas conocidas agrupadas por categoría para poder rotar competidores
    BRAND_POOLS = {
        "luxury": [
            "gucci", "prada", "miu miu", "louis vuitton", "dior", "chanel",
            "hermes", "balenciaga", "valentino", "burberry", "fendi",
            "saint laurent", "bottega veneta", "loewe", "celine",
            "alexander mcqueen", "versace", "givenchy",
        ],
        "jewelry": [
            "swarovski", "pandora", "tous", "cartier", "tiffany", "bulgari",
            "chopard", "van cleef arpels", "boucheron", "mikimoto",
            "pasquale bruni", "damiani", "marco bicego", "roberto coin",
            "pomellato", "piaget",
        ],
        "premium": [
            "michael kors", "karl lagerfeld", "philipp plein", "pinko",
            "chiara ferragni", "the attico", "vivienne westwood",
            "off-white", "dsquared", "pierre cardin", "coach", "kate spade",
            "jimmy choo", "christian louboutin",
        ],
        "fast_fashion": [
            "zara", "mango", "stradivarius", "bershka", "pull&bear",
            "h&m", "primark", "shein", "oysho", "massimo dutti",
            "cos", "arket", "uniqlo",
        ],
        "sport": [
            "nike", "adidas", "puma", "new balance", "converse",
            "vans", "reebok", "under armour", "asics", "fila",
        ],
        "spanish_fashion": [
            "bimba y lola", "parfois", "desigual", "geox", "unisa",
            "pedro miralles", "uterque", "massimo dutti", "lefties",
        ],
    }

    # Detectar qué categoría disparar
    detected = {}
    for seed in seeds:
        low = seed.lower()
        for cat_name, pool in BRAND_POOLS.items():
            for brand in pool:
                if brand in low:
                    detected[cat_name] = detected.get(cat_name, [])
                    proper = " ".join(w.capitalize() for w in brand.split())
                    if proper not in detected[cat_name]:
                        detected[cat_name].append(proper)

    if not detected:
        return []

    # Si NO queremos competidoras, devolver solo las detectadas
    if not include_competitors:
        result = []
        for found_brands in detected.values():
            result.extend(found_brands)
        # Deduplicar
        seen = set()
        final = []
        for b in result:
            if b not in seen:
                seen.add(b)
                final.append(b)
        return final

    # CON competidoras: añadir 2-3 aleatorias por categoría detectada
    result = []
    for cat_name, found_brands in detected.items():
        result.extend(found_brands[:3])
        pool = BRAND_POOLS[cat_name]
        candidates = [
            " ".join(w.capitalize() for w in b.split())
            for b in pool
            if " ".join(w.capitalize() for w in b.split()) not in result
        ]
        _rnd.shuffle(candidates)
        result.extend(candidates[:3])

    # Deduplicar manteniendo orden
    seen = set()
    final = []
    for b in result:
        if b not in seen:
            seen.add(b)
            final.append(b)

    return final[:6]


def get_reddit_fashion_trends(limit: int = 25, time_filter: str = "week") -> dict:
    """
    Devuelve los posts más populares de subreddits de moda.

    time_filter: 'day', 'week', 'month'
    """
    posts = []
    for sub_name in FASHION_SUBREDDITS:
        try:
            sub = reddit.subreddit(sub_name)
            for post in sub.top(time_filter=time_filter, limit=limit):
                posts.append(
                    {
                        "subreddit": sub_name,
                        "title": post.title,
                        "score": post.score,
                        "num_comments": post.num_comments,
                        "url": f"https://reddit.com{post.permalink}",
                        "flair": post.link_flair_text,
                    }
                )
        except Exception as e:
            posts.append({"subreddit": sub_name, "error": str(e)})

    # Ordena por score para que el LLM vea primero lo más relevante
    posts_sorted = sorted(
        [p for p in posts if "score" in p], key=lambda x: x["score"], reverse=True
    )
    return {
        "time_filter": time_filter,
        "total": len(posts_sorted),
        "posts": posts_sorted[:50],
    }


# ---------------------------------------------------------------------------
# WRAPPERS para search_alternatives (Bing, DuckDuckGo, Reddit search)
# Usamos la instancia `reddit` global del servidor en lugar de pedirla cada vez
# ---------------------------------------------------------------------------


def reddit_search_volume_wrap(
    query: str,
    time_filter: str = "month",
    subreddits: list[str] | None = None,
    limit: int = 100,
) -> dict:
    """Wrapper que pasa la instancia de Reddit del servidor."""
    return reddit_search_volume(
        reddit_instance=reddit,
        query=query,
        time_filter=time_filter,
        subreddits=subreddits,
        limit=limit,
    )


def reddit_compare_terms_wrap(
    queries: list[str],
    time_filter: str = "month",
) -> dict:
    """Wrapper que pasa la instancia de Reddit del servidor."""
    return reddit_compare_terms(
        reddit_instance=reddit,
        queries=queries,
        time_filter=time_filter,
    )


def search_alternatives_combined(
    keyword: str,
    region: str = "US",
) -> dict:
    """
    Sustituto de Google Trends: Bing + DuckDuckGo + Reddit en paralelo.
    Si Google está bloqueado, esta es la mejor alternativa unificada.
    """
    return search_alternatives_for_keyword(
        keyword=keyword,
        region=region,
        reddit_instance=reddit,
    )


def get_brand_full_intelligence(
    brand: str,
    region: str = "US",
    timeframe: str = "today 1-m",
    timespan_gdelt: str = "7d",
    include_competitors: bool = False,
) -> dict:
    """
    INTELIGENCIA COMPLETA de una marca en UNA sola llamada.

    Lanza en PARALELO las fuentes principales y devuelve un análisis
    consolidado con cross-source validation pre-procesado:

      1. SEARCH SIGNAL (sustituye a Google Trends, que está bloqueado por IP):
         - Bing Webmaster: volumen mensual real de búsqueda
         - DuckDuckGo: términos asociados/sugeridos
         - Reddit Search: menciones reales en subreddits de moda + engagement
      2. Wikipedia Pageviews (interés del público)
      3. GDELT (cobertura mediática global + tono)
      4. eBay (datos comerciales reales: listings, precios, vendedores)

    Tiempo total: ~12-15s (limitado por la fuente más lenta — GDELT).

    brand: marca a analizar (ej: "Swarovski", "Pandora", "Gucci")
    region: ISO país. Auto-resuelve marketplace eBay y idioma Wikipedia.
    timeframe: ventana temporal (informativo, ya no se usa para Google)
    timespan_gdelt: ventana GDELT ('7d' por defecto)
    include_competitors: si True, también lanza wikipedia_brand_signals con
                        2-3 competidoras para context comparativo.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    region = normalize_region(region)
    log(f"🧠 Inteligencia completa de '{brand}' en {region or 'global'}")

    tasks = {
        # SEARCH SIGNAL: sustituye Google Trends con Bing+DDG+Reddit en paralelo
        "search_signal": lambda: search_alternatives_for_keyword(
            keyword=brand, region=region or "US", reddit_instance=reddit
        ),
        "wikipedia": lambda: get_wikipedia_pageviews(brand, region, months=3),
        "gdelt": lambda: brand_gdelt_full_signal(
            brand=brand, region=region, timespan=timespan_gdelt
        ),
        "ebay": lambda: get_ebay_brand_signal(
            brand=brand, region=region, limit=20
        ),
        # Etsy: marketplace artesanal/handmade — señal especialmente fuerte
        # para joyería, accesorios, vintage. Complementa a eBay con productos
        # únicos no presentes en mercado mainstream.
        "etsy": lambda: get_etsy_brand_signal(
            brand=brand, region=region, limit=20
        ),
    }

    if include_competitors:
        # Detectar competidoras y añadir comparativa Wikipedia
        competitors_data = extract_brand_candidates(
            [brand], include_competitors=True
        )
        if len(competitors_data) > 1:
            tasks["competitors_wikipedia"] = lambda: compare_brands_wikipedia(
                competitors_data[:5], region, months=3
            )

    intelligence: dict = {
        "brand": brand,
        "region": region or "global",
        "timespan_gdelt": timespan_gdelt,
        "source": "brand_full_intelligence",
    }

    # Lanzar todo en paralelo
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_key = {executor.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                intelligence[key] = future.result(timeout=25)
            except Exception as e:
                log(f"⚠️  {key} falló: {e}")
                intelligence[key] = {"error": str(e)[:120]}

    # ----------------------------------------------------------
    # Análisis cross-source pre-procesado
    # ----------------------------------------------------------
    cross_signals: dict = {}

    # SEARCH SIGNAL: extraer señales de Bing + DuckDuckGo + Reddit
    search = intelligence.get("search_signal", {})
    if isinstance(search, dict):
        s_summary = search.get("summary", {})

        # Bing: volumen cuantitativo mensual (lo más cercano a Google Trends)
        bing_volume = s_summary.get("bing_volume")
        if bing_volume is not None:
            cross_signals["bing_monthly_impressions"] = bing_volume
            # Clasificar volumen como señal
            if bing_volume > 50000:
                cross_signals["search_signal"] = "very_strong"
            elif bing_volume > 5000:
                cross_signals["search_signal"] = "strong"
            elif bing_volume > 500:
                cross_signals["search_signal"] = "moderate"
            elif bing_volume > 0:
                cross_signals["search_signal"] = "weak"
            else:
                cross_signals["search_signal"] = "none"
        else:
            cross_signals["search_signal"] = "unknown"

        # DuckDuckGo: términos asociados (señal cualitativa)
        ddg_count = s_summary.get("ddg_suggestion_count", 0)
        cross_signals["search_associated_terms"] = ddg_count
        cross_signals["search_top_associations"] = s_summary.get(
            "ddg_top_suggestions", []
        )[:5]

        # Reddit: conversación real (señal cultural)
        reddit_mentions = s_summary.get("reddit_mentions", 0)
        reddit_engagement = s_summary.get("reddit_engagement", 0)
        cross_signals["reddit_mentions"] = reddit_mentions
        cross_signals["reddit_engagement"] = reddit_engagement
        if reddit_engagement > 5000:
            cross_signals["reddit_signal"] = "strong"
        elif reddit_engagement > 500:
            cross_signals["reddit_signal"] = "moderate"
        elif reddit_engagement > 0:
            cross_signals["reddit_signal"] = "weak"
        else:
            cross_signals["reddit_signal"] = "none"

    # Wikipedia: ¿está subiendo?
    wiki = intelligence.get("wikipedia", {})
    wiki_trend = wiki.get("last_month_vs_previous") if isinstance(wiki, dict) else None
    if wiki_trend:
        try:
            pct = float(str(wiki_trend).replace("%", "").replace("+", ""))
            cross_signals["wikipedia_trend"] = (
                "rising" if pct > 5 else "falling" if pct < -5 else "stable"
            )
            cross_signals["wikipedia_pct"] = wiki_trend
        except (ValueError, TypeError):
            cross_signals["wikipedia_trend"] = "unknown"

    # GDELT: ¿cobertura subiendo y tono positivo?
    gdelt = intelligence.get("gdelt", {})
    if isinstance(gdelt, dict):
        vol = gdelt.get("media_volume", {})
        tone = gdelt.get("media_tone", {})
        if vol.get("found"):
            cross_signals["media_volume_trend"] = vol.get("trend_recent_vs_older")
        if tone.get("found"):
            cross_signals["media_sentiment"] = tone.get("sentiment")
            cross_signals["media_tone_value"] = tone.get("average_tone")

    # eBay: ¿hay oferta comercial real?
    ebay = intelligence.get("ebay", {})
    if isinstance(ebay, dict):
        price_summary = ebay.get("price_summary", {})
        best_match = ebay.get("best_match", {})
        if price_summary:
            cross_signals["ebay_listings"] = price_summary.get("total_items_with_price", 0)
            cross_signals["ebay_avg_price"] = price_summary.get("average")
            cross_signals["ebay_price_range"] = (
                f"{price_summary.get('min')}-{price_summary.get('max')}"
            )
        if isinstance(best_match, dict) and best_match.get("total_found"):
            cross_signals["ebay_total_market_listings"] = best_match.get("total_found")

    # Etsy: ¿hay oferta artesanal? ¿es marca con tracción en el segmento handmade?
    etsy = intelligence.get("etsy", {})
    if isinstance(etsy, dict):
        etsy_price = etsy.get("price_summary", {})
        etsy_pop = etsy.get("popularity_summary", {})
        etsy_best = etsy.get("best_match", {})
        if etsy_price:
            cross_signals["etsy_avg_price"] = etsy_price.get("average")
        if etsy_pop:
            cross_signals["etsy_total_favorers"] = etsy_pop.get("total_favorers", 0)
            cross_signals["etsy_avg_favorers"] = etsy_pop.get("average_favorers", 0)
        if isinstance(etsy_best, dict) and etsy_best.get("total_found"):
            cross_signals["etsy_total_listings"] = etsy_best.get("total_found")
        # Top tags Etsy son señal de qué se asocia con la marca en handmade
        top_tags = etsy.get("top_tags_aggregated", [])
        if top_tags:
            cross_signals["etsy_top_tags"] = [t["tag"] for t in top_tags[:5]]

    # ----------------------------------------------------------
    # Lecturas ejecutivas (frases listas para citar por el bot)
    # ----------------------------------------------------------
    readings = []
    s = cross_signals.get("search_signal")
    r = cross_signals.get("reddit_signal")
    w = cross_signals.get("wikipedia_trend")
    m = cross_signals.get("media_sentiment")
    e_listings = cross_signals.get("ebay_total_market_listings", 0)
    bing_vol = cross_signals.get("bing_monthly_impressions")
    reddit_eng = cross_signals.get("reddit_engagement", 0)

    # Headline de búsqueda real
    if s == "very_strong":
        readings.append(
            f"🔍 Volumen de búsqueda muy alto en Bing ({bing_vol:,} impresiones/mes)"
        )
    elif s == "strong":
        readings.append(
            f"🔍 Volumen de búsqueda sólido en Bing ({bing_vol:,} impresiones/mes)"
        )
    elif s in ("weak", "moderate") and bing_vol:
        readings.append(
            f"🔎 Búsqueda nicho en Bing ({bing_vol:,} impresiones/mes)"
        )

    # Conversación cultural
    if r == "strong":
        readings.append(
            f"💬 Conversación fuerte en Reddit ({reddit_eng:,} engagement)"
        )
    elif r == "moderate":
        readings.append(
            f"💬 Conversación moderada en Reddit ({reddit_eng:,} engagement)"
        )
    elif r == "none" and s in ("strong", "very_strong"):
        readings.append("🔇 Búsqueda alta pero sin conversación en Reddit (compra silenciosa)")

    # Cross-source: search + interés
    if s in ("strong", "very_strong") and w == "rising":
        readings.append("📈 Búsqueda + interés público en alza: tendencia confirmada")
    if w == "falling" and s in ("none", "weak"):
        readings.append("📉 Interés en descenso: marca enfriándose")

    # Mediática
    if m in ("negative", "very_negative"):
        readings.append("⚠️ Tono mediático negativo: revisar contexto")

    # Comercial
    if e_listings and e_listings > 1000:
        readings.append(f"💰 Oferta comercial fuerte ({e_listings:,} listings activos en eBay)")
    elif e_listings and e_listings < 100:
        readings.append(f"🔻 Oferta comercial débil ({e_listings} listings en eBay)")

    # Etsy (artesanal/handmade)
    etsy_listings = cross_signals.get("etsy_total_listings", 0)
    etsy_favorers = cross_signals.get("etsy_total_favorers", 0)
    if etsy_listings and etsy_listings > 5000:
        readings.append(
            f"🎨 Fuerte presencia artesanal en Etsy ({etsy_listings:,} listings)"
        )
    elif etsy_listings and etsy_listings > 500:
        readings.append(
            f"🎨 Presencia notable en mercado handmade Etsy ({etsy_listings:,} listings)"
        )
    if etsy_favorers and etsy_favorers > 1000:
        readings.append(
            f"❤️ Alta popularidad en Etsy ({etsy_favorers:,} favorers en top listings)"
        )

    # Si no hay nada destacable
    if not readings:
        readings.append("➖ Marca estable sin momentum visible")

    cross_signals["executive_readings"] = readings
    intelligence["cross_source_summary"] = cross_signals

    log(f"✅ Inteligencia completa lista: {len(readings)} señales identificadas")
    return intelligence


def get_fashion_trends_summary(
    brands: list[str],
    region: str = "US",
    timeframe: str = "today 1-m",
    timespan_gdelt: str = "7d",
) -> dict:
    """
    PANORAMA COMPLETO de varias marcas en UNA sola llamada.

    Internamente lanza N x brand_full_intelligence EN PARALELO. Útil cuando
    el usuario pregunta "compárame el panorama de joyería en Italia" o
    "cómo van las marcas de fast fashion en España".

    Tiempo total: ~15-20s (limitado por la marca más lenta, no la suma).
    Antes (4 tool calls separadas): 60-80s.

    Devuelve:
      - Una clave por marca con su brand_full_intelligence completo
      - Un bloque "comparative_summary" con ranking pre-calculado por:
          • search_signal (Google: strong/weak/none)
          • wikipedia_trend (rising/falling/stable)
          • media_sentiment (positive/neutral/negative)
          • ebay_listings (volumen comercial)
      - Un bloque "category_readings" con frases ejecutivas que comparan

    brands: lista de marcas a analizar (1-6 recomendado, máx 8)
    region: código ISO. Aplica a todas las marcas.
    timeframe: ventana Google
    timespan_gdelt: ventana GDELT
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not brands:
        return {"error": "no brands provided"}
    if len(brands) > 8:
        log(f"⚠️  Limitando a 8 marcas (recibidas {len(brands)})")
        brands = brands[:8]

    region = normalize_region(region)
    log(f"📊 Panorama de {len(brands)} marcas en {region or 'global'}: {brands}")

    summary: dict = {
        "brands_analyzed": brands,
        "region": region or "global",
        "timeframe_google": timeframe,
        "timespan_gdelt": timespan_gdelt,
        "source": "fashion_trends_summary",
        "by_brand": {},
    }

    # Lanzar todas las marcas en paralelo
    # Cada brand_full_intelligence ya paraleliza sus 4 fuentes internamente,
    # así que tenemos paralelización en 2 niveles.
    with ThreadPoolExecutor(max_workers=min(len(brands), 4)) as executor:
        future_to_brand = {
            executor.submit(
                get_brand_full_intelligence,
                brand=brand,
                region=region,
                timeframe=timeframe,
                timespan_gdelt=timespan_gdelt,
                include_competitors=False,
            ): brand
            for brand in brands
        }
        for future in as_completed(future_to_brand):
            brand = future_to_brand[future]
            try:
                summary["by_brand"][brand] = future.result(timeout=40)
            except Exception as e:
                log(f"⚠️  {brand} falló: {e}")
                summary["by_brand"][brand] = {"error": str(e)[:120]}

    # ---------- Análisis comparativo ----------
    comparative: dict = {
        "search_ranking": [],          # ordenado por volumen Bing
        "reddit_ranking": [],          # ordenado por engagement Reddit
        "interest_ranking": [],        # ordenado por wikipedia_pct
        "media_ranking": [],           # ordenado por media_tone
        "commerce_ranking": [],        # ordenado por ebay listings
    }

    rows = []
    for brand, intel in summary["by_brand"].items():
        if not isinstance(intel, dict) or "error" in intel:
            continue
        cs = intel.get("cross_source_summary", {})
        rows.append({
            "brand": brand,
            "search_signal": cs.get("search_signal"),
            "bing_volume": cs.get("bing_monthly_impressions") or 0,
            "reddit_signal": cs.get("reddit_signal"),
            "reddit_mentions": cs.get("reddit_mentions", 0),
            "reddit_engagement": cs.get("reddit_engagement", 0),
            "wikipedia_trend": cs.get("wikipedia_trend"),
            "wikipedia_pct": cs.get("wikipedia_pct"),
            "media_sentiment": cs.get("media_sentiment"),
            "media_tone_value": cs.get("media_tone_value"),
            "ebay_listings": cs.get("ebay_total_market_listings", 0),
            "ebay_avg_price": cs.get("ebay_avg_price"),
        })

    if rows:
        # Ranking de búsqueda (volumen Bing descendente)
        comparative["search_ranking"] = sorted(
            rows,
            key=lambda r: r.get("bing_volume", 0) or 0,
            reverse=True,
        )

        # Ranking de Reddit (engagement descendente)
        comparative["reddit_ranking"] = sorted(
            [r for r in rows if r.get("reddit_engagement", 0) > 0],
            key=lambda r: r.get("reddit_engagement", 0) or 0,
            reverse=True,
        )

        # Ranking interés Wikipedia (mayor crecimiento %)
        def _pct_to_num(pct: str | None) -> float:
            if not pct:
                return -999
            try:
                return float(str(pct).replace("%", "").replace("+", ""))
            except (ValueError, TypeError):
                return -999

        comparative["interest_ranking"] = sorted(
            rows,
            key=lambda r: _pct_to_num(r["wikipedia_pct"]),
            reverse=True,
        )

        # Ranking mediático (mayor tono positivo)
        comparative["media_ranking"] = sorted(
            [r for r in rows if r["media_tone_value"] is not None],
            key=lambda r: r["media_tone_value"] or 0,
            reverse=True,
        )

        # Ranking comercial eBay (más listings activos)
        comparative["commerce_ranking"] = sorted(
            [r for r in rows if r["ebay_listings"]],
            key=lambda r: r["ebay_listings"] or 0,
            reverse=True,
        )

    summary["comparative_summary"] = comparative

    # ---------- Lecturas categoría ----------
    category_readings = []

    if comparative["search_ranking"]:
        top = comparative["search_ranking"][0]
        bv = top.get("bing_volume", 0)
        if bv > 5000:
            category_readings.append(
                f"🔍 Líder en búsqueda: {top['brand']} ({bv:,} imp/mes en Bing)"
            )
        elif bv > 0:
            category_readings.append(
                f"🔎 Mayor volumen de búsqueda: {top['brand']} ({bv:,} imp/mes en Bing)"
            )

    if comparative["reddit_ranking"]:
        top_r = comparative["reddit_ranking"][0]
        eng = top_r.get("reddit_engagement", 0)
        if eng > 1000:
            category_readings.append(
                f"💬 Líder en conversación: {top_r['brand']} ({eng:,} engagement Reddit)"
            )

    if comparative["interest_ranking"]:
        rising = [
            r for r in comparative["interest_ranking"]
            if r.get("wikipedia_trend") == "rising"
        ]
        falling = [
            r for r in comparative["interest_ranking"]
            if r.get("wikipedia_trend") == "falling"
        ]
        if rising:
            names = ", ".join(r["brand"] for r in rising[:3])
            category_readings.append(f"📈 Interés en alza: {names}")
        if falling:
            names = ", ".join(r["brand"] for r in falling[:3])
            category_readings.append(f"📉 Interés en descenso: {names}")

    if comparative["media_ranking"]:
        top_media = comparative["media_ranking"][0]
        if top_media["media_sentiment"] in ("positive", "very_positive"):
            category_readings.append(
                f"📰 Mejor narrativa mediática: {top_media['brand']} "
                f"(tono {top_media['media_tone_value']:+.1f})"
            )
        negative = [
            r for r in comparative["media_ranking"]
            if r["media_sentiment"] in ("negative", "very_negative")
        ]
        if negative:
            names = ", ".join(r["brand"] for r in negative[:2])
            category_readings.append(f"⚠️  Tono mediático negativo: {names}")

    if comparative["commerce_ranking"]:
        top_commerce = comparative["commerce_ranking"][0]
        category_readings.append(
            f"💰 Mayor presencia comercial: {top_commerce['brand']} "
            f"({top_commerce['ebay_listings']} listings activos en eBay)"
        )

    summary["category_readings"] = category_readings

    log(f"✅ Panorama listo: {len(rows)} marcas con datos, {len(category_readings)} lecturas")
    return summary


def get_brand_deep_dive(
    brand: str,
    region: str = "ES",
    timeframe: str = "today 1-m",
    product_types: list[str] | None = None,
    fast_mode: bool = True,
) -> dict:
    """
    Análisis profundo de una marca. En vez de buscar "related queries" de la marca
    (que suelen traer marcas competidoras), combina la marca con tipos de producto
    específicos para ver QUÉ productos de esa marca están subiendo.

    brand: nombre de la marca (ej: "swarovski", "zara", "nike")
    region: código ISO (ES, IT, US, ...)
    timeframe: ventana temporal
    product_types: tipos de producto a combinar con la marca. Si no se pasa,
                   se eligen ALEATORIAMENTE entre un pool según el idioma,
                   para diversificar resultados entre llamadas.
    fast_mode: usar delays mínimos (recomendado True para esta función).
    """
    # Pools organizados por IDIOMA y por CATEGORÍA DE PRODUCTO.
    # brand_deep_dive detecta la categoría principal de la marca y prioriza
    # productos de esa categoría, con algunas variantes rotativas.
    if product_types is None:
        PRODUCT_POOLS = {
            "IT": {
                "jewelry": ["collana", "orecchini", "anello", "bracciale", "orologio", "ciondolo"],
                "bags": ["borsa", "pochette", "tracolla", "shopper", "zaino"],
                "shoes": ["scarpe", "sneakers", "stivali", "sandali", "mocassini"],
                "clothing": ["vestito", "giacca", "cappotto", "maglione", "camicia"],
                "accessories": ["occhiali", "cintura", "portafoglio", "cappello", "sciarpa"],
            },
            "FR": {
                "jewelry": ["collier", "boucles d'oreilles", "bague", "bracelet", "montre"],
                "bags": ["sac", "pochette", "cabas", "sac à dos"],
                "shoes": ["chaussures", "baskets", "bottes", "sandales"],
                "clothing": ["robe", "veste", "manteau", "pull", "chemise"],
                "accessories": ["lunettes", "ceinture", "portefeuille", "chapeau"],
            },
            "ES": {
                "jewelry": ["collar", "pendientes", "anillo", "pulsera", "reloj", "colgante"],
                "bags": ["bolso", "cartera", "mochila", "bandolera", "shopper"],
                "shoes": ["zapatos", "zapatillas", "botas", "sandalias", "tacones"],
                "clothing": ["vestido", "chaqueta", "abrigo", "jersey", "camisa"],
                "accessories": ["gafas", "cinturon", "sombrero", "bufanda"],
            },
            "US": {
                "jewelry": ["necklace", "earrings", "ring", "bracelet", "watch", "pendant"],
                "bags": ["bag", "clutch", "backpack", "tote", "crossbody"],
                "shoes": ["shoes", "sneakers", "boots", "sandals", "heels"],
                "clothing": ["dress", "jacket", "coat", "sweater", "shirt"],
                "accessories": ["sunglasses", "belt", "wallet", "hat", "scarf"],
            },
        }

        # Detectar categoría principal de la marca
        brand_low = brand.lower()
        JEWELRY_BRANDS = {
            "swarovski", "pandora", "tous", "cartier", "tiffany", "bulgari",
            "bvlgari", "chopard", "mikimoto", "pomellato", "damiani", "piaget",
            "van cleef", "boucheron", "rolex", "omega",
        }
        SHOE_BRANDS = {
            "nike", "adidas", "puma", "new balance", "converse", "vans",
            "geox", "unisa", "pedro miralles", "jimmy choo", "louboutin",
        }
        BAG_BRANDS = {
            "coach", "kate spade", "longchamp", "michael kors",
        }

        if any(b in brand_low for b in JEWELRY_BRANDS):
            primary_cat = "jewelry"
        elif any(b in brand_low for b in SHOE_BRANDS):
            primary_cat = "shoes"
        elif any(b in brand_low for b in BAG_BRANDS):
            primary_cat = "bags"
        else:
            # Para marcas generalistas (Gucci, Zara, Dior...) priorizamos bags
            # que es la categoría más consultada en moda.
            primary_cat = "bags"

        region_up = region.upper()
        if region_up in ("ES", "MX", "AR", "CO", "PE"):
            pool_by_cat = PRODUCT_POOLS["ES"]
        elif region_up == "IT":
            pool_by_cat = PRODUCT_POOLS["IT"]
        elif region_up in ("FR", "BE"):
            pool_by_cat = PRODUCT_POOLS["FR"]
        else:
            pool_by_cat = PRODUCT_POOLS["US"]

        # 3 productos del núcleo (categoría principal) + 2 variantes rotativas
        primary_products = pool_by_cat.get(primary_cat, pool_by_cat["bags"])
        core = random.sample(primary_products, min(3, len(primary_products)))

        # 2 productos aleatorios de otras categorías para diversificar
        all_other = []
        for cat, prods in pool_by_cat.items():
            if cat != primary_cat:
                all_other.extend(prods)
        rotation = random.sample(all_other, min(2, len(all_other))) if all_other else []

        product_types = core + rotation
        log(f"🎯 Deep dive: categoría principal detectada = '{primary_cat}'")

    # Construir las combinaciones "brand + producto"
    seeds = [f"{brand} {p}" for p in product_types[:5]]
    log(f"🔍 Deep dive '{brand}' en {region}: {seeds}")

    # Reutilizamos la función principal con estas semillas.
    # IMPORTANTE: expand_with_competitors=False porque el usuario preguntó por
    # UNA marca concreta. No queremos mezclar Pandora/Tiffany en la respuesta.
    result = get_google_trending_fashion(
        region=region,
        timeframe=timeframe,
        seeds=seeds,
        category=0,
        fast_mode=fast_mode,
        expand_with_competitors=False,
    )
    result["brand"] = brand
    result["product_types_probed"] = product_types
    return result


def compare_fashion_keywords(keywords: list[str], region: str = "ES") -> dict:
    """
    Compara hasta 5 keywords de moda en Google Trends.
    Si Google da error/vacío, hace FALLBACK a Bing + Reddit automáticamente.
    """
    if len(keywords) > 5:
        keywords = keywords[:5]

    # Caché: clave basada en keywords ordenadas + región (sin caracteres especiales)
    cache_sig = "compare-" + "-".join(sorted(k.replace(" ", "") for k in keywords))
    cached = cache_get(cache_sig, region, "compare")
    if cached is not None:
        return {"region": region, "summary": cached[0] if cached else {}, "cached": True}

    google_failed = False
    google_error = None

    try:
        log(f"Comparando en Google: {keywords}")
        pytrends.build_payload(keywords, cat=185, timeframe="today 3-m", geo=region)
        df = pytrends.interest_over_time()
        if df.empty:
            log(f"Google sin datos → fallback a Bing+Reddit")
            google_failed = True
        else:
            df = df.drop(columns=["isPartial"], errors="ignore")
            summary = {
                kw: {
                    "average": float(df[kw].mean()),
                    "latest": float(df[kw].iloc[-1]),
                    "peak": float(df[kw].max()),
                }
                for kw in keywords
                if kw in df.columns
            }
            # Cachea el resultado
            cache_set(cache_sig, region, "compare", [summary])
            return {
                "region": region,
                "summary": summary,
                "source": "google_trends",
                "cached": False,
            }
    except Exception as e:
        google_failed = True
        google_error = str(e)[:120]
        log(f"Google falló ({google_error}) → fallback a Bing+Reddit")

    # FALLBACK: usar Bing + Reddit en paralelo
    if google_failed:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from search_alternatives import compare_bing_keywords

        fallback_summary = {}
        bing_data = {}
        reddit_data = {}

        with ThreadPoolExecutor(max_workers=2) as executor:
            f_bing = executor.submit(compare_bing_keywords, keywords, region)
            f_reddit = executor.submit(reddit_compare_terms_wrap, keywords, "month")
            try:
                bing_data = f_bing.result(timeout=20) or {}
            except Exception as e:
                log(f"   Bing fallback falló: {e}")
            try:
                reddit_data = f_reddit.result(timeout=30) or {}
            except Exception as e:
                log(f"   Reddit fallback falló: {e}")

        # Combinar: por cada keyword, mezclamos lo que tengan Bing y Reddit
        bing_by_kw = {
            r["keyword"]: r.get("monthly_impressions")
            for r in bing_data.get("ranking", [])
        }
        reddit_by_kw = {
            r["query"]: {
                "mentions": r.get("mentions", 0),
                "engagement": r.get("engagement", 0),
            }
            for r in reddit_data.get("ranking", [])
        }

        for kw in keywords:
            entry = {}
            if kw in bing_by_kw:
                entry["bing_monthly_impressions"] = bing_by_kw[kw]
            r = reddit_by_kw.get(kw, {})
            if r:
                entry["reddit_mentions"] = r["mentions"]
                entry["reddit_engagement"] = r["engagement"]
            if entry:
                fallback_summary[kw] = entry

        if fallback_summary:
            cache_set(cache_sig, region, "compare", [fallback_summary])
            return {
                "region": region,
                "summary": fallback_summary,
                "source": "bing_reddit_fallback",
                "google_error": google_error or "empty_response",
                "cached": False,
            }

        return {
            "keywords": keywords,
            "data": [],
            "note": "Google sin datos y fallback Bing/Reddit tampoco devolvió señal",
            "google_error": google_error,
        }


# ---------------------------------------------------------------------------
# Servidor MCP
# ---------------------------------------------------------------------------

app = Server("fashion-trends")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="brand_full_intelligence",
            description=(
                "🚀 INTELIGENCIA COMPLETA de una marca en UNA sola llamada. "
                "Lanza en paralelo Google Trends + Wikipedia + GDELT + eBay y "
                "devuelve análisis cross-source pre-procesado con lectura ejecutiva. "
                "PREFERIR ESTA TOOL sobre llamadas separadas cuando el usuario "
                "pregunta por una marca — es 3x más rápida y la respuesta es más "
                "completa. Ahorra al bot tener que combinar fuentes manualmente."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand": {
                        "type": "string",
                        "description": "Marca a analizar. Ej: 'Swarovski', 'Pandora', 'Gucci'",
                    },
                    "region": {
                        "type": "string",
                        "description": "Código país ISO. Auto-resuelve marketplace eBay e idioma Wikipedia.",
                        "default": "US",
                    },
                    "timeframe": {
                        "type": "string",
                        "default": "today 1-m",
                    },
                    "timespan_gdelt": {
                        "type": "string",
                        "default": "7d",
                    },
                    "include_competitors": {
                        "type": "boolean",
                        "default": False,
                        "description": "Si true, añade comparativa Wikipedia con 2-3 competidoras",
                    },
                },
                "required": ["brand"],
            },
        ),
        Tool(
            name="fashion_trends_summary",
            description=(
                "📊 PANORAMA DE VARIAS MARCAS en UNA sola llamada. Lanza en "
                "paralelo brand_full_intelligence para 2-8 marcas a la vez y "
                "devuelve rankings comparativos pre-calculados (search, interés, "
                "tono mediático, presencia comercial) más lecturas ejecutivas "
                "de la categoría. PREFERIR cuando el usuario pide comparar "
                "varias marcas o ver el panorama de un sector. ~15-20s vs 60-80s "
                "si llamaras a brand_full_intelligence varias veces."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Marcas a analizar (1-8). Ej: ['Swarovski', 'Pandora', 'Tous', 'Tiffany']",
                    },
                    "region": {
                        "type": "string",
                        "default": "US",
                    },
                    "timeframe": {"type": "string", "default": "today 1-m"},
                    "timespan_gdelt": {"type": "string", "default": "7d"},
                },
                "required": ["brands"],
            },
        ),
        Tool(
            name="get_google_fashion_trends",
            description=(
                "Obtiene keywords de moda en tendencia ascendente desde Google Trends "
                "para una región y ventana temporal. Devuelve términos que están "
                "creciendo en búsquedas (incluye 'Breakout' para términos emergentes). "
                "IMPORTANTE: elige la categoría correcta según el producto — si buscas "
                "joyería usa category=186, calzado category=1076, bolsos category=1036. "
                "category=185 (Apparel) solo cubre ropa y puede devolver vacío para "
                "otros productos."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "description": "Código país ISO (ES, US, MX, AR, ...). Por defecto ES.",
                        "default": "ES",
                    },
                    "timeframe": {
                        "type": "string",
                        "description": "Ventana: 'now 1-d', 'now 7-d', 'today 1-m', 'today 3-m'",
                        "default": "now 7-d",
                    },
                    "seeds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Semillas de búsqueda (máx 5 recomendado por rate limit). "
                            "Importante: usa palabras en el idioma de la región (España→español, Italia→italiano). "
                            "Si se omite, usa defaults: vestido, zapatillas, bolso, abrigo, jeans."
                        ),
                    },
                    "category": {
                        "type": "integer",
                        "description": (
                            "ID de categoría Google Trends. Opcional — se auto-detecta por las seeds. "
                            "0=Todas, 185=Apparel/Ropa, 1036=Bolsos, 1076=Calzado, 44=Belleza."
                        ),
                    },
                    "fast_mode": {
                        "type": "boolean",
                        "description": (
                            "Si true, omite reintentos tras 429 y usa delays mínimos. "
                            "Responde en <5s pero puede devolver arrays vacíos. "
                            "Recomendado para demos en vivo."
                        ),
                        "default": False,
                    },
                    "enrich": {
                        "type": "boolean",
                        "description": (
                            "Si true (default), añade metadatos a cada trend "
                            "(marca, tier, tipo de producto, color, momentum) y "
                            "análisis agregado (brand_frequency, cross_source_brands, "
                            "momentum_tiers, top3_highlights)."
                        ),
                        "default": True,
                    },
                },
            },
        ),
        Tool(
            name="get_brand_deep_dive",
            description=(
                "Análisis profundo de una marca específica (ej: Swarovski, Zara, Nike). "
                "A diferencia de buscar 'related queries' de la marca directamente "
                "(que suele traer marcas competidoras), esta tool combina el nombre "
                "de la marca con tipos de producto para descubrir qué productos "
                "concretos de esa marca están subiendo en búsquedas. "
                "Usa esta tool cuando el usuario pregunte por una marca específica."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand": {
                        "type": "string",
                        "description": "Nombre de la marca. Ej: 'swarovski', 'zara', 'nike'.",
                    },
                    "region": {"type": "string", "default": "ES"},
                    "timeframe": {"type": "string", "default": "today 1-m"},
                    "product_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Productos a combinar con la marca. Si se omite, usa defaults "
                            "según el idioma de la región (ES→collar,pendientes,anillo...; "
                            "IT→collana,orecchini,anello...; US→necklace,earrings,ring...). "
                            "Pásalos en el idioma local."
                        ),
                    },
                    "fast_mode": {"type": "boolean", "default": True},
                },
                "required": ["brand"],
            },
        ),
        Tool(
            name="get_reddit_fashion_trends",
            description=(
                "Devuelve los posts más votados de los principales subreddits de moda "
                "en la ventana temporal indicada. Útil para detectar prendas, marcas "
                "o estilos que están generando conversación real."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "time_filter": {
                        "type": "string",
                        "enum": ["day", "week", "month"],
                        "default": "week",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Posts por subreddit (máx 25)",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="compare_fashion_keywords",
            description=(
                "Compara hasta 5 keywords de moda en Google Trends durante los "
                "últimos 3 meses. Devuelve promedio, último valor y pico. "
                "Útil para decidir cuál de varios términos tiene más tracción."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de 1 a 5 keywords a comparar",
                    },
                    "region": {"type": "string", "default": "ES"},
                },
                "required": ["keywords"],
            },
        ),
        Tool(
            name="wikipedia_brand_signals",
            description=(
                "Compara marcas de moda usando visualizaciones de Wikipedia. "
                "ALTERNATIVA ROBUSTA a Google Trends — sin rate limits. "
                "Usa cuando Google falle o quieras validar tendencias con "
                "un segundo dato cuantitativo independiente."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Marcas a comparar (1-8). Ej: ['Gucci','Prada','Miu Miu']",
                    },
                    "region": {"type": "string", "default": "ES"},
                    "months": {"type": "integer", "default": 3},
                },
                "required": ["brands"],
            },
        ),
        Tool(
            name="fashion_editorial_news",
            description=(
                "Titulares recientes de medios de moda (Vogue, Elle, WWD, Business of Fashion). "
                "Útil para contexto editorial y narrativa de tendencias."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "region": {"type": "string", "default": "US"},
                    "items_per_source": {"type": "integer", "default": 5},
                },
            },
        ),
        Tool(
            name="gdelt_brand_signal",
            description=(
                "Señal completa de una marca en prensa global vía GDELT: "
                "volumen de cobertura mediática, tono (sentimiento), artículos "
                "recientes y países donde más se habla. Cubre 100k+ medios en "
                "65 idiomas. Sin rate limits. Ideal para validar si una marca "
                "tiene empuje editorial real, no solo búsquedas."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand": {
                        "type": "string",
                        "description": "Marca o término a analizar. Ej: 'Gucci', 'Swarovski', 'Miu Miu'",
                    },
                    "region": {
                        "type": "string",
                        "description": "Código país ISO (opcional). Si se omite, cobertura global.",
                    },
                    "timespan": {
                        "type": "string",
                        "description": "Ventana: '24h', '3d', '7d', '1w', '1m', '3m'",
                        "default": "7d",
                    },
                },
                "required": ["brand"],
            },
        ),
        Tool(
            name="gdelt_compare_brands",
            description=(
                "Compara el volumen de cobertura mediática de varias marcas "
                "en prensa global (GDELT). Devuelve ranking ordenado por "
                "cobertura + tono/sentimiento de cada una. Útil para competitive "
                "intelligence rápido: quién está dominando la conversación."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Marcas a comparar (1-8). Ej: ['Gucci', 'Prada', 'Miu Miu']",
                    },
                    "region": {"type": "string"},
                    "timespan": {"type": "string", "default": "7d"},
                },
                "required": ["brands"],
            },
        ),
        Tool(
            name="ebay_brand_signal",
            description=(
                "Datos REALES de comercio de una marca en eBay: productos disponibles, "
                "rango de precios, top vendedores, oferta nueva vs premium. "
                "Complementa señales de búsqueda/medios con qué se está VENDIENDO "
                "de verdad. Útil para validar si una marca tiene mercado activo."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand": {
                        "type": "string",
                        "description": "Marca a analizar. Ej: 'Swarovski', 'Pandora'",
                    },
                    "region": {
                        "type": "string",
                        "description": "Código país ISO (US, GB, IT, ES, DE...). Determina marketplace eBay.",
                    },
                    "limit": {"type": "integer", "default": 30},
                },
                "required": ["brand"],
            },
        ),
        Tool(
            name="ebay_compare_brands",
            description=(
                "Compara varias marcas en eBay por volumen de listings activos "
                "y precio medio. Ranking de oferta real en el mercado de segunda "
                "mano y nuevo. Excelente para entender presencia comercial real."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Marcas a comparar (1-6).",
                    },
                    "region": {"type": "string"},
                },
                "required": ["brands"],
            },
        ),
        Tool(
            name="ebay_search_products",
            description=(
                "Busca productos específicos en eBay por keyword. Devuelve listings "
                "con título, precio, condición, vendedor, imagen. Útil cuando el "
                "usuario pregunta por un producto concreto y quieres datos reales "
                "de oferta y precios."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Términos de búsqueda. Ej: 'swarovski necklace gold'",
                    },
                    "region": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                    "sort": {
                        "type": "string",
                        "enum": ["best_match", "price", "-price", "newlyListed", "endingSoonest"],
                        "default": "best_match",
                    },
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="etsy_brand_signal",
            description=(
                "🎨 Señal de marca en Etsy (marketplace artesanal/handmade/vintage). "
                "Devuelve productos disponibles, rango de precios, top shops, "
                "popularidad (favorers), tags asociados. Especialmente fuerte para "
                "joyería, accesorios, productos únicos. Complementa a eBay con la "
                "perspectiva del segmento artesanal e independiente."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand": {
                        "type": "string",
                        "description": "Marca a analizar. Ej: 'Swarovski', 'Pandora'",
                    },
                    "region": {
                        "type": "string",
                        "description": "Código país ISO (US, GB, IT, ES, DE...). Filtra por ship_to.",
                    },
                    "limit": {"type": "integer", "default": 30},
                },
                "required": ["brand"],
            },
        ),
        Tool(
            name="etsy_compare_brands",
            description=(
                "Compara varias marcas en Etsy por volumen de listings, precio "
                "medio y popularidad (favorers). Útil para entender presencia "
                "en el mercado artesanal e independiente."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Marcas a comparar (1-6).",
                    },
                    "region": {"type": "string"},
                },
                "required": ["brands"],
            },
        ),
        Tool(
            name="etsy_search_products",
            description=(
                "Busca productos específicos en Etsy por keyword. Útil para "
                "productos artesanales, vintage, hechos a mano. Devuelve listings "
                "con título, precio, shop, favorers, tags y materiales."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "region": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                    "sort_on": {
                        "type": "string",
                        "enum": ["score", "price", "created", "updated"],
                        "default": "score",
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                    "language": {
                        "type": "string",
                        "description": "Filtrar por idioma del listing ('en', 'es', 'it'...). Si se omite, se auto-detecta de la región. Pasa 'any' para no filtrar.",
                    },
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="etsy_trending_category",
            description=(
                "Detecta productos trending en una categoría de Etsy. Combina "
                "top score + recientes para identificar items con momentum real. "
                "Devuelve top productos + tags trending de la categoría."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Keyword de categoría. Ej: 'jewelry', 'vintage dress', 'handmade bag'",
                        "default": "jewelry",
                    },
                    "region": {"type": "string"},
                    "limit": {"type": "integer", "default": 30},
                },
            },
        ),
        Tool(
            name="search_alternatives_keyword",
            description=(
                "🔍 SUSTITUTO de Google Trends. Lanza Bing + DuckDuckGo + Reddit "
                "EN PARALELO para una keyword y devuelve volumen de búsqueda real "
                "(Bing) + términos asociados (DuckDuckGo) + menciones reales "
                "(Reddit). USAR cuando Google Trends devuelva 429/vacío, o cuando "
                "quieras señales independientes de Google."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Término a investigar. Ej: 'borsa donna', 'swarovski necklace'",
                    },
                    "region": {
                        "type": "string",
                        "description": "Código país ISO (US, ES, IT, GB, DE, FR...)",
                        "default": "US",
                    },
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="bing_keyword_volume",
            description=(
                "Volumen mensual de búsqueda en Bing Webmaster API. "
                "Único motor que da datos cuantitativos reales de búsqueda "
                "como sustituto de Google Trends. Requiere BING_WEBMASTER_API_KEY "
                "configurada en .env."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "region": {"type": "string", "default": "US"},
                    "months": {"type": "integer", "default": 6},
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="reddit_search_volume",
            description=(
                "Cuenta menciones reales de un término en subreddits de moda y "
                "lifestyle durante un período. Es señal cuantitativa de qué "
                "está hablando la gente — proxy fiable de búsqueda real "
                "cuando Google está bloqueado."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "time_filter": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year", "all"],
                        "default": "month",
                    },
                    "limit": {"type": "integer", "default": 100},
                },
                "required": ["query"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    import json

    if name == "brand_full_intelligence":
        data = get_brand_full_intelligence(
            brand=arguments["brand"],
            region=arguments.get("region", "US"),
            timeframe=arguments.get("timeframe", "today 1-m"),
            timespan_gdelt=arguments.get("timespan_gdelt", "7d"),
            include_competitors=arguments.get("include_competitors", False),
        )
    elif name == "fashion_trends_summary":
        data = get_fashion_trends_summary(
            brands=arguments["brands"],
            region=arguments.get("region", "US"),
            timeframe=arguments.get("timeframe", "today 1-m"),
            timespan_gdelt=arguments.get("timespan_gdelt", "7d"),
        )
    elif name == "get_google_fashion_trends":
        data = get_google_trending_fashion(
            region=arguments.get("region", "ES"),
            timeframe=arguments.get("timeframe", "now 7-d"),
            seeds=arguments.get("seeds"),
            category=arguments.get("category"),
            fast_mode=arguments.get("fast_mode", False),
            enrich=arguments.get("enrich", True),
        )
    elif name == "get_brand_deep_dive":
        data = get_brand_deep_dive(
            brand=arguments["brand"],
            region=arguments.get("region", "ES"),
            timeframe=arguments.get("timeframe", "today 1-m"),
            product_types=arguments.get("product_types"),
            fast_mode=arguments.get("fast_mode", True),
        )
    elif name == "get_reddit_fashion_trends":
        data = get_reddit_fashion_trends(
            limit=arguments.get("limit", 10),
            time_filter=arguments.get("time_filter", "week"),
        )
    elif name == "compare_fashion_keywords":
        data = compare_fashion_keywords(
            keywords=arguments["keywords"],
            region=arguments.get("region", "ES"),
        )
    elif name == "wikipedia_brand_signals":
        data = compare_brands_wikipedia(
            brands=arguments["brands"],
            region=arguments.get("region", "ES"),
            months=arguments.get("months", 3),
        )
    elif name == "fashion_editorial_news":
        data = get_fashion_editorial(
            region=arguments.get("region", "US"),
            items_per_source=arguments.get("items_per_source", 5),
        )
    elif name == "gdelt_brand_signal":
        data = brand_gdelt_full_signal(
            brand=arguments["brand"],
            region=arguments.get("region"),
            timespan=arguments.get("timespan", "7d"),
        )
    elif name == "gdelt_compare_brands":
        data = compare_brands_gdelt(
            brands=arguments["brands"],
            region=arguments.get("region"),
            timespan=arguments.get("timespan", "7d"),
        )
    elif name == "ebay_brand_signal":
        data = get_ebay_brand_signal(
            brand=arguments["brand"],
            region=arguments.get("region"),
            limit=arguments.get("limit", 30),
        )
    elif name == "ebay_compare_brands":
        data = compare_brands_ebay(
            brands=arguments["brands"],
            region=arguments.get("region"),
        )
    elif name == "ebay_search_products":
        data = search_ebay_products(
            keyword=arguments["keyword"],
            region=arguments.get("region"),
            limit=arguments.get("limit", 20),
            sort=arguments.get("sort", "best_match"),
        )
    elif name == "etsy_brand_signal":
        data = get_etsy_brand_signal(
            brand=arguments["brand"],
            region=arguments.get("region"),
            limit=arguments.get("limit", 30),
        )
    elif name == "etsy_compare_brands":
        data = compare_brands_etsy(
            brands=arguments["brands"],
            region=arguments.get("region"),
        )
    elif name == "etsy_search_products":
        data = search_etsy_products(
            keyword=arguments["keyword"],
            region=arguments.get("region"),
            limit=arguments.get("limit", 20),
            sort_on=arguments.get("sort_on", "score"),
            sort_order=arguments.get("sort_order", "desc"),
            language=arguments.get("language"),
        )
    elif name == "etsy_trending_category":
        data = get_etsy_trending_in_category(
            category=arguments.get("category", "jewelry"),
            region=arguments.get("region"),
            limit=arguments.get("limit", 30),
        )
    elif name == "search_alternatives_keyword":
        data = search_alternatives_combined(
            keyword=arguments["keyword"],
            region=arguments.get("region", "US"),
        )
    elif name == "bing_keyword_volume":
        data = get_bing_keyword_volume(
            keyword=arguments["keyword"],
            region=arguments.get("region", "US"),
            months=arguments.get("months", 6),
        )
    elif name == "reddit_search_volume":
        data = reddit_search_volume_wrap(
            query=arguments["query"],
            time_filter=arguments.get("time_filter", "month"),
            limit=arguments.get("limit", 100),
        )
    else:
        data = {"error": f"Tool desconocida: {name}"}

    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
