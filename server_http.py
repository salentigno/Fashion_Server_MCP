"""
Fashion Trends MCP Server — versión HTTP remota (usando FastMCP)
----------------------------------------------------------------
Mismo servidor que server.py pero expuesto por HTTP streamable.
Usa FastMCP (del SDK oficial) que gestiona todo el wiring HTTP
internamente — más estable entre versiones que cablear el transport a mano.

Añade autenticación por API key vía middleware Starlette.

Uso:
    python server_http.py

Variables de entorno esperadas (.env):
    REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT
    MCP_API_KEYS   = claves separadas por comas
    MCP_HOST       = host a bindear (default: 127.0.0.1)
    MCP_PORT       = puerto (default: 8000)

Endpoint MCP: http://HOST:PORT/mcp
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# Reutilizamos TODA la lógica del servidor stdio
from server import (
    get_google_trending_fashion,
    get_reddit_fashion_trends,
    compare_fashion_keywords,
    get_brand_deep_dive,
    get_brand_full_intelligence,
    get_fashion_trends_summary,
    log,
)
from alt_sources import (
    get_wikipedia_pageviews,
    compare_brands_wikipedia,
    get_fashion_editorial,
)
from gdelt_source import (
    brand_gdelt_full_signal,
    compare_brands_gdelt,
)
from ebay_source import (
    search_ebay_products,
    get_ebay_brand_signal,
    compare_brands_ebay,
)

from etsy_source import (
    search_etsy_products,
    get_etsy_brand_signal,
    compare_brands_etsy,
    get_etsy_trending_in_category,
)

# Search alternatives wrappers (usan reddit del server)
from server import (
    search_alternatives_combined,
    reddit_search_volume_wrap,
    reddit_compare_terms_wrap,
)
from search_alternatives import (
    get_bing_keyword_volume,
    get_bing_related_keywords,
    compare_bing_keywords,
    get_duckduckgo_suggestions,
    compare_duckduckgo_keywords,
)

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

API_KEYS = {
    k.strip() for k in os.getenv("MCP_API_KEYS", "").split(",") if k.strip()
}
HOST = os.getenv("MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("MCP_PORT", "8000"))

# Hosts permitidos en el header Host (protección anti-DNS-rebinding del SDK).
# Por defecto solo localhost. Para exponer via tunnel/proxy añade aquí los
# dominios publicos separados por comas. Usa "*" para aceptar cualquiera
# (no recomendado en produccion, OK para demos).
#
# Ej .env:
#   MCP_ALLOWED_HOSTS=*
#   MCP_ALLOWED_HOSTS=gig-minerals-sauce-museums.trycloudflare.com,127.0.0.1
ALLOWED_HOSTS_RAW = os.getenv("MCP_ALLOWED_HOSTS", "").strip()
if ALLOWED_HOSTS_RAW:
    ALLOWED_HOSTS = [h.strip() for h in ALLOWED_HOSTS_RAW.split(",") if h.strip()]
else:
    ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

if not API_KEYS:
    log("⚠️  AVISO: MCP_API_KEYS vacío. Servidor aceptará todas las peticiones.")

log(f"🔐 Allowed hosts: {ALLOWED_HOSTS}")


# ---------------------------------------------------------------------------
# Servidor MCP con FastMCP (wiring HTTP integrado)
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "fashion-trends",
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=(ALLOWED_HOSTS != ["*"]),
        allowed_hosts=ALLOWED_HOSTS if ALLOWED_HOSTS != ["*"] else [],
    ),
)


@mcp.tool()
def get_google_fashion_trends(
    region: str = "ES",
    timeframe: str = "now 7-d",
    seeds: list[str] | None = None,
    category: int | None = None,
    fast_mode: bool = False,
    enrich: bool = True,
) -> str:
    """Obtiene keywords de moda en tendencia ascendente desde Google Trends.

    CÓMO USARLA BIEN:
    - Pasa SIEMPRE 3-5 semillas variadas, no solo 1. Más semillas = más
      datos y mayor cobertura. Ejemplo bueno: ['borsa','borse','pochette']
      en lugar de solo ['borse'].
    - Usa palabras en el IDIOMA de la región: región IT → italiano,
      región ES → español, región US → inglés. Mezclar idiomas da vacío.
    - timeframe recomendado: 'today 1-m' suele tener más datos que 'now 7-d'.
    - category se auto-detecta por las seeds, NO la pases salvo que sepas por qué.
    - fast_mode=false es más fiable; úsalo solo si necesitas respuestas <5s.
    - enrich=true (default) añade metadatos útiles por trend y análisis agregado.

    La respuesta incluye (cuando enrich=true):
    - Cada trend con: detected_brand, brand_tier, product_type, color, momentum
    - Bloque "enrichment" con: brand_frequency, category_breakdown,
      cross_source_brands, momentum_tiers, top3_highlights

    Args:
        region: Código país ISO (ES, IT, US, MX, FR, AR, ...). Por defecto ES.
        timeframe: 'now 1-d', 'now 7-d', 'today 1-m' (RECOMENDADO), 'today 3-m'
        seeds: Lista de 3-5 semillas en el idioma de la región.
        category: Opcional, se auto-detecta. NO la pases salvo excepción.
        fast_mode: Default false. Usa true solo si la llamada debe responder <5s.
        enrich: Default true. Añade metadatos y análisis agregado local.
                Pásalo a false solo si quieres la respuesta cruda mínima.
    """
    data = get_google_trending_fashion(
        region=region, timeframe=timeframe, seeds=seeds,
        category=category, fast_mode=fast_mode, enrich=enrich,
    )
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def brand_deep_dive(
    brand: str,
    region: str = "ES",
    timeframe: str = "today 1-m",
    product_types: list[str] | None = None,
    fast_mode: bool = True,
) -> str:
    """Análisis profundo de una marca específica.

    IMPORTANTE: úsala cuando el usuario pregunte por una marca concreta
    (Swarovski, Zara, Nike, etc.). Combina el nombre de la marca con tipos
    de producto para ver qué productos de esa marca están subiendo, en vez
    de devolver marcas competidoras como haría una búsqueda simple.

    Args:
        brand: Nombre de la marca. Ej: 'swarovski', 'zara', 'nike'.
        region: Código país ISO. Los product_types por defecto se ajustan
                al idioma de la región.
        timeframe: Ventana temporal.
        product_types: Lista opcional de productos a combinar. Si se omite,
                       usa defaults según el idioma de la región.
        fast_mode: Usar delays mínimos (default True, recomendado).
    """
    data = get_brand_deep_dive(
        brand=brand, region=region, timeframe=timeframe,
        product_types=product_types, fast_mode=fast_mode,
    )
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def get_reddit_fashion_trends_tool(
    time_filter: str = "week",
    limit: int = 10,
) -> str:
    """Posts más votados de subreddits de moda.

    Args:
        time_filter: 'day', 'week' o 'month'
        limit: Posts por subreddit (máx 25)
    """
    data = get_reddit_fashion_trends(limit=limit, time_filter=time_filter)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def compare_fashion_keywords_tool(
    keywords: list[str],
    region: str = "ES",
) -> str:
    """Compara hasta 5 keywords de moda en Google Trends (últimos 3 meses).

    Args:
        keywords: Lista de 1 a 5 keywords a comparar
        region: Código país ISO
    """
    data = compare_fashion_keywords(keywords=keywords, region=region)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def wikipedia_brand_signals(
    brands: list[str],
    region: str = "ES",
    months: int = 3,
) -> str:
    """Compara marcas de moda usando visualizaciones de Wikipedia.

    Alternativa ROBUSTA a Google Trends — Wikipedia Pageviews API no tiene
    rate limits prácticos. Si Google está bloqueando, usa esta tool. Devuelve
    visualizaciones mensuales de las páginas de cada marca y el % de crecimiento
    del último mes vs el anterior.

    Args:
        brands: Lista de marcas a comparar (1-8). Ej: ['Gucci', 'Prada', 'Miu Miu']
        region: Código país ISO. Determina en qué idioma de Wikipedia buscar.
        months: Meses de historia (1-6). Default 3.
    """
    data = compare_brands_wikipedia(brands=brands, region=region, months=months)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def fashion_editorial_news(
    region: str = "US",
    items_per_source: int = 5,
) -> str:
    """Titulares recientes de medios de moda (Vogue, Elle, WWD, Business of Fashion).

    Fuente RSS oficial, sin bloqueos. Útil para:
    - Contexto editorial sobre tendencias que están publicando los medios
    - Detectar colaboraciones, lanzamientos, scandals, shows recientes
    - Complementar datos cuantitativos con narrativa

    Args:
        region: Código país ISO. US/UK → Vogue, Elle, WWD. IT → Vogue Italia.
                ES → Vogue España.
        items_per_source: Titulares por medio (1-10). Default 5.
    """
    data = get_fashion_editorial(region=region, items_per_source=items_per_source)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def gdelt_brand_signal(
    brand: str,
    region: str | None = None,
    timespan: str = "7d",
) -> str:
    """Señal completa de una marca en prensa global (GDELT).

    Devuelve volumen de cobertura mediática, tono (positivo/negativo),
    artículos recientes y países donde más se habla del tema.

    GDELT monitoriza 100.000+ medios en 65 idiomas, actualizado cada 15 min.
    Sin rate limits prácticos. Complementa Google Trends con una señal
    completamente distinta: editorial/prensa en vez de búsquedas.

    USO TÍPICO: después de detectar una marca trending en Google, valida
    con GDELT si tiene también empuje editorial real. Si sube en ambos,
    es tendencia genuina. Si solo sube en Google, puede ser hype efímero.

    Args:
        brand: Marca o término. Ej: 'Gucci', 'Swarovski', 'Miu Miu'
        region: Código país ISO (opcional). Si se omite, cobertura global.
        timespan: '24h', '3d', '7d' (default), '1w', '1m', '3m'
    """
    data = brand_gdelt_full_signal(brand=brand, region=region, timespan=timespan)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def gdelt_compare_brands(
    brands: list[str],
    region: str | None = None,
    timespan: str = "7d",
) -> str:
    """Compara varias marcas por volumen de cobertura mediática en prensa global.

    Ranking ordenado por cobertura total + tono de cada marca.
    Perfecta para competitive intelligence: ¿quién está dominando la
    conversación de prensa esta semana?

    Args:
        brands: Lista de marcas a comparar (1-8). Ej: ['Gucci', 'Prada', 'Miu Miu']
        region: Código país ISO (opcional). Global si se omite.
        timespan: '24h', '3d', '7d' (default), '1w', '1m', '3m'
    """
    data = compare_brands_gdelt(brands=brands, region=region, timespan=timespan)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def fashion_trends_summary(
    brands: list[str],
    region: str = "US",
    timeframe: str = "today 1-m",
    timespan_gdelt: str = "7d",
) -> str:
    """📊 PANORAMA DE VARIAS MARCAS en UNA sola llamada.

    Lanza en paralelo brand_full_intelligence para 2-8 marcas a la vez.
    Devuelve los datos completos de cada marca + rankings comparativos
    pre-calculados (search, interés, tono mediático, presencia comercial)
    + lecturas ejecutivas de la categoría.

    PREFERIR cuando el usuario pide:
    - "compárame estas marcas"
    - "el panorama de joyería en Italia"
    - "cómo van las marcas de fast fashion en España"

    Tiempo: ~15-20s (limitado por la marca más lenta).
    Antes (4 brand_full_intelligence separados): 60-80s.

    La respuesta incluye:
    - by_brand: dict con la inteligencia completa de cada marca
    - comparative_summary: rankings ordenados por:
        • search_ranking — Google signal (strong/weak/none)
        • interest_ranking — crecimiento Wikipedia
        • media_ranking — tono GDELT
        • commerce_ranking — listings eBay
    - category_readings: frases ejecutivas listas para citar

    Args:
        brands: Lista de marcas a analizar (1-8). Ej: ['Swarovski', 'Pandora', 'Tous'].
        region: Código país ISO (US, IT, ES, GB, DE, FR...). Aplica a todas.
        timeframe: Ventana Google. Default 'today 1-m'.
        timespan_gdelt: Ventana GDELT. Default '7d'.
    """
    data = get_fashion_trends_summary(
        brands=brands,
        region=region,
        timeframe=timeframe,
        timespan_gdelt=timespan_gdelt,
    )
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def brand_full_intelligence(
    brand: str,
    region: str = "US",
    timeframe: str = "today 1-m",
    timespan_gdelt: str = "7d",
    include_competitors: bool = False,
) -> str:
    """🚀 INTELIGENCIA COMPLETA de una marca en UNA sola llamada.

    Lanza EN PARALELO Google Trends + Wikipedia + GDELT + eBay y devuelve
    análisis cross-source pre-procesado con lectura ejecutiva.

    PREFERIR ESTA TOOL sobre llamadas separadas cuando el usuario pregunta
    por una marca. Es 3x más rápida que llamar las 4 fuentes por separado
    (12-15s vs 30-45s) y la respuesta incluye análisis cruzado pre-hecho.

    La respuesta incluye:
    - google_trends: deep dive de productos de la marca
    - wikipedia: pageviews y tendencia mensual
    - gdelt: cobertura mediática global, tono y artículos recientes
    - ebay: listings, precios, vendedores, productos premium
    - cross_source_summary: análisis pre-procesado con señales clave y
      "executive_readings" (frases listas para citar)

    Args:
        brand: Marca a analizar. Ej: 'Swarovski', 'Pandora', 'Gucci'.
        region: Código país ISO (US, GB, IT, ES, DE, FR...). Auto-resuelve
                marketplace eBay e idioma Wikipedia. Default 'US'.
        timeframe: Ventana Google Trends. Default 'today 1-m'.
        timespan_gdelt: Ventana GDELT. Default '7d'.
        include_competitors: Si True, añade comparativa Wikipedia con 2-3
                            competidoras de la misma categoría. Default False.
    """
    data = get_brand_full_intelligence(
        brand=brand,
        region=region,
        timeframe=timeframe,
        timespan_gdelt=timespan_gdelt,
        include_competitors=include_competitors,
    )
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def ebay_brand_signal(
    brand: str,
    region: str | None = None,
    limit: int = 30,
) -> str:
    """Datos REALES de comercio de una marca en eBay.

    Devuelve productos disponibles, rango de precios, top vendedores,
    oferta nueva vs premium. La perspectiva única de eBay es que ves qué
    se está VENDIENDO de verdad, no solo qué se busca.

    USO TÍPICO: validar si una marca tiene mercado activo y a qué precios.
    Si Google muestra interés pero eBay no tiene listings, es señal de marca
    débil comercialmente.

    Args:
        brand: Marca a analizar. Ej: 'Swarovski', 'Pandora', 'Gucci'
        region: Código país ISO (US, GB, IT, ES, DE, FR...).
                Determina el marketplace eBay.
        limit: Productos a recuperar (default 30, máximo 50).
    """
    data = get_ebay_brand_signal(brand=brand, region=region, limit=limit)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def ebay_compare_brands(
    brands: list[str],
    region: str | None = None,
) -> str:
    """Compara varias marcas en eBay por volumen de listings y precio medio.

    Ranking de oferta real en el mercado. Excelente complemento a Google/Wikipedia
    para entender presencia comercial real (no solo búsqueda).

    Args:
        brands: Marcas a comparar (1-6).
        region: Código país ISO. Default usa marketplace de .env.
    """
    data = compare_brands_ebay(brands=brands, region=region)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def ebay_search_products(
    keyword: str,
    region: str | None = None,
    limit: int = 20,
    sort: str = "best_match",
) -> str:
    """Busca productos específicos en eBay por keyword.

    Devuelve listings con título, precio, condición, vendedor, imagen, URL.
    Útil cuando el usuario pregunta por un producto concreto.

    Args:
        keyword: Términos de búsqueda. Ej: 'swarovski necklace gold'
        region: Código país ISO. Determina marketplace.
        limit: 1-50, default 20.
        sort: 'best_match' (default), 'price' (asc), '-price' (desc),
              'newlyListed', 'endingSoonest'.
    """
    data = search_ebay_products(
        keyword=keyword, region=region, limit=limit, sort=sort
    )
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def etsy_brand_signal(
    brand: str,
    region: str | None = None,
    limit: int = 30,
) -> str:
    """🎨 Señal de marca en Etsy — marketplace artesanal/handmade/vintage.

    Devuelve productos disponibles, rango de precios, top shops, popularidad
    (favorers), tags asociados. Especialmente fuerte para joyería, accesorios
    y productos únicos. Complementa a eBay con la perspectiva del segmento
    artesanal e independiente.

    USO TÍPICO: detectar si una marca tiene presencia en el mercado handmade
    (mucho favorers + listings activos = marca relevante para audiencia
    creativa/artesanal).

    Args:
        brand: Marca a analizar. Ej: 'Swarovski', 'Pandora', 'Vintage Chanel'.
        region: Código país ISO (US, GB, IT, ES, DE...). Filtra por ship_to.
        limit: Listings a recuperar (default 30, máximo 100).
    """
    data = get_etsy_brand_signal(brand=brand, region=region, limit=limit)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def etsy_compare_brands(
    brands: list[str],
    region: str | None = None,
) -> str:
    """Compara varias marcas en Etsy por listings, precio medio y popularidad.

    Ranking del mercado artesanal. Útil para entender qué marcas tienen más
    tracción en audiencias que valoran productos hechos a mano y vintage.

    Args:
        brands: Marcas a comparar (1-6).
        region: Código país ISO. Default global.
    """
    data = compare_brands_etsy(brands=brands, region=region)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def etsy_search_products(
    keyword: str,
    region: str | None = None,
    limit: int = 20,
    sort_on: str = "score",
    sort_order: str = "desc",
    language: str | None = None,
) -> str:
    """Busca productos específicos en Etsy por keyword.

    Útil para productos artesanales, vintage, hechos a mano. Devuelve listings
    con título, precio, shop, favorers, tags y materiales.

    Args:
        keyword: Términos de búsqueda. Ej: 'silver pendant', 'vintage handbag'.
        region: Código país ISO. Filtra por ship_to.
        limit: 1-100, default 20.
        sort_on: 'score' (relevancia, default), 'price', 'created', 'updated'.
        sort_order: 'desc' (default) o 'asc'.
        language: 'en', 'es', 'it', 'de', 'fr'... Si se omite, se auto-detecta
                  del idioma de la región. Pasa 'any' para no filtrar.
    """
    data = search_etsy_products(
        keyword=keyword,
        region=region,
        limit=limit,
        sort_on=sort_on,
        sort_order=sort_order,
        language=language,
    )
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def etsy_trending_category(
    category: str = "jewelry",
    region: str | None = None,
    limit: int = 30,
) -> str:
    """Detecta productos trending en una categoría de Etsy.

    Combina top score + recientes para identificar items con momentum real
    (productos recientes que ya tienen alta tracción). Devuelve top productos
    + tags trending de la categoría.

    Args:
        category: Keyword de categoría. Ej: 'jewelry', 'vintage dress', 'handmade bag'.
        region: Código país ISO. Filtra por ship_to.
        limit: Listings por consulta (default 30).
    """
    data = get_etsy_trending_in_category(
        category=category, region=region, limit=limit
    )
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def search_alternatives_keyword(
    keyword: str,
    region: str = "US",
) -> str:
    """🔍 SUSTITUTO de Google Trends — Bing + DuckDuckGo + Reddit en paralelo.

    Lanza las 3 fuentes alternativas simultáneamente y devuelve:
    - Bing: volumen mensual de búsqueda real (si BING_WEBMASTER_API_KEY está configurada)
    - DuckDuckGo: términos asociados/sugeridos para esta keyword
    - Reddit: menciones reales en subreddits de moda + engagement

    USAR ESTA TOOL cuando Google Trends devuelva 429/vacío, o como fuente
    independiente de Google. Es la mejor combinación posible sin pagar APIs.

    Args:
        keyword: Término a investigar. Ej: 'borsa donna', 'swarovski necklace'.
        region: Código país ISO (US, ES, IT, GB, DE, FR...). Default US.
    """
    data = search_alternatives_combined(keyword=keyword, region=region)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def bing_keyword_volume(
    keyword: str,
    region: str = "US",
    months: int = 6,
) -> str:
    """Volumen mensual de búsqueda en Bing — sustituto cuantitativo de Google Trends.

    Bing Webmaster Keyword Research API devuelve impresiones mensuales reales
    para una keyword en un país e idioma específico. Es la única fuente que
    da datos cuantitativos comparables a Google Trends sin pagar.

    Requiere BING_WEBMASTER_API_KEY en .env (gratis en Bing Webmaster Tools).

    Args:
        keyword: Término a consultar. Ej: 'bolso negro', 'jewelry trends'.
        region: Código país ISO. Default US.
        months: Meses de histórico (1-12). Default 6.
    """
    data = get_bing_keyword_volume(keyword=keyword, region=region, months=months)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def reddit_search_volume(
    query: str,
    time_filter: str = "month",
    limit: int = 100,
) -> str:
    """Cuenta menciones reales de un término en subreddits de moda + lifestyle.

    Es un proxy fiable de búsqueda real: Reddit es donde la gente pregunta
    y discute productos antes de comprar. Más menciones = más interés real.

    Devuelve:
    - total_mentions: posts encontrados
    - engagement_score: comentarios + upvotes agregados
    - top_subreddits: subs donde más se discute
    - top_posts: 5 posts con mayor engagement

    Args:
        query: Término a buscar. Ej: 'swarovski', 'mango bag'.
        time_filter: 'day', 'week', 'month' (default), 'year', 'all'.
        limit: Máx posts a recuperar (default 100, máximo 100).
    """
    data = reddit_search_volume_wrap(
        query=query, time_filter=time_filter, limit=limit
    )
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Middleware de autenticación por API Key
# ---------------------------------------------------------------------------


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Rechaza peticiones sin header X-API-Key válido."""

    async def dispatch(self, request, call_next):
        # Healthcheck sin auth
        if request.url.path == "/health":
            return JSONResponse({"status": "ok"})

        # Modo dev: sin claves configuradas = todo pasa
        if not API_KEYS:
            return await call_next(request)

        key = request.headers.get("x-api-key") or request.headers.get("X-Api-Key")
        if key not in API_KEYS:
            client = request.client.host if request.client else "unknown"
            log(f"🚫 Auth rechazada desde {client}")
            return JSONResponse(
                {"error": "invalid or missing X-API-Key header"}, status_code=401
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Obtener la app Starlette de FastMCP y añadir nuestro middleware
    app = mcp.streamable_http_app()
    app.add_middleware(APIKeyMiddleware)

    log(f"🚀 Fashion Trends MCP (HTTP) en http://{HOST}:{PORT}/mcp")
    log(f"   API keys configuradas: {len(API_KEYS)}")

    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
