"""
Fashion Trends REST API
-----------------------
Expone las mismas funciones del servidor MCP pero como API REST clásica,
pensada para integrarse con herramientas tipo "API Request" de bajo código
(AppCentral Intelligence Studio, Zapier, n8n, Make, etc.).

Endpoints:
    GET  /health
    GET  /trends/google   (alias simple con query params)
    POST /trends/google   (forma recomendada con JSON body)
    GET  /trends/reddit
    POST /trends/compare

Autenticación: header X-API-Key obligatorio si MCP_API_KEYS está definido.

Uso:
    python server_rest.py

Escucha en http://HOST:PORT/  (por defecto 127.0.0.1:8001)
Expón con Cloudflare Tunnel:
    cloudflared tunnel --url http://localhost:8001
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# Reutilizamos TODA la lógica del servidor original
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

from server import (
    search_alternatives_combined,
    reddit_search_volume_wrap,
    reddit_compare_terms_wrap,
)
from search_alternatives import (
    get_bing_keyword_volume,
    compare_bing_keywords,
    get_duckduckgo_suggestions,
    compare_duckduckgo_keywords,
)

from fastapi import FastAPI, Request, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
import uvicorn

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

API_KEYS = {
    k.strip() for k in os.getenv("MCP_API_KEYS", "").split(",") if k.strip()
}
HOST = os.getenv("REST_HOST", "127.0.0.1")
PORT = int(os.getenv("REST_PORT", "8001"))

app = FastAPI(
    title="Fashion Trends API",
    description="API REST para tendencias de moda (Google Trends + Reddit)",
    version="1.0.0",
)

# CORS abierto para demos. Para producción, restringe a dominios concretos.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------


def check_auth(x_api_key: Optional[str]) -> None:
    """Valida el header X-API-Key. Si no hay keys configuradas, deja pasar."""
    if not API_KEYS:
        return  # modo dev: sin auth
    if x_api_key not in API_KEYS:
        raise HTTPException(
            status_code=401, detail="invalid or missing X-API-Key header"
        )


# ---------------------------------------------------------------------------
# Modelos de entrada (Pydantic = validación automática + docs)
# ---------------------------------------------------------------------------


class GoogleTrendsRequest(BaseModel):
    region: str = Field("ES", description="Código país ISO (ES, IT, US, ...)")
    timeframe: str = Field("now 7-d", description="'now 1-d','now 7-d','today 1-m','today 3-m'")
    seeds: Optional[list[str]] = Field(
        None, description="Semillas de búsqueda (máx 5). Ej: ['bolso','zapatillas']"
    )
    category: Optional[int] = Field(
        None,
        description=(
            "Opcional. Se auto-detecta por las semillas. "
            "Para forzarla: 185=Ropa, 1036=Bolsos, 1076=Calzado, 44=Belleza, 0=Todas"
        ),
    )
    fast_mode: bool = Field(
        False,
        description="Si True, responde en <5s (sin reintentos tras 429). Ideal para demos.",
    )
    enrich: bool = Field(
        True,
        description="Si True (default), añade metadatos y análisis agregado a los trends.",
    )


class BrandDeepDiveRequest(BaseModel):
    brand: str = Field(..., description="Marca a analizar. Ej: 'swarovski', 'zara'.")
    region: str = Field("ES")
    timeframe: str = Field("today 1-m")
    product_types: Optional[list[str]] = Field(
        None, description="Productos a combinar. Si se omite, defaults por idioma."
    )
    fast_mode: bool = Field(True)


class RedditRequest(BaseModel):
    time_filter: str = Field("week", description="day | week | month")
    limit: int = Field(10, description="Posts por subreddit (1-25)")


class CompareRequest(BaseModel):
    keywords: list[str] = Field(..., description="Entre 1 y 5 keywords a comparar")
    region: str = Field("ES", description="Código país ISO")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    """Healthcheck sin auth."""
    return {"status": "ok", "service": "fashion-trends-rest"}


@app.get("/")
def root():
    """Lista los endpoints disponibles."""
    return {
        "service": "Fashion Trends REST API",
        "endpoints": {
            "GET  /health": "Healthcheck (sin auth)",
            "GET  /trends/google": "Google Trends con query params (+ fast_mode)",
            "POST /trends/google": "Google Trends con JSON body",
            "GET  /trends/brand": "Deep dive de una marca específica",
            "POST /trends/brand": "Deep dive de una marca (JSON)",
            "GET  /trends/brand/full": "🚀 Inteligencia completa: Google+Wiki+GDELT+eBay en paralelo",
            "GET  /trends/category/summary": "📊 Panorama de varias marcas con rankings comparativos",
            "GET  /trends/reddit": "Reddit fashion posts",
            "POST /trends/compare": "Comparar keywords en Google Trends",
            "GET  /trends/wikipedia": "Comparar marcas con Wikipedia Pageviews (robusto)",
            "GET  /trends/editorial": "Titulares recientes de Vogue/Elle/WWD",
            "GET  /trends/gdelt/brand": "Señal completa de marca en prensa global (volumen+tono)",
            "GET  /trends/gdelt/compare": "Comparar marcas en cobertura mediática global",
            "GET  /trends/ebay/brand": "Datos comerciales reales de una marca en eBay",
            "GET  /trends/ebay/compare": "Comparar marcas por listings y precios en eBay",
            "GET  /trends/ebay/search": "Buscar productos en eBay con precios y vendedores",
            "GET  /trends/etsy/brand": "🎨 Señal de marca en Etsy (artesanal/handmade)",
            "GET  /trends/etsy/compare": "Comparar marcas en Etsy",
            "GET  /trends/etsy/search": "Buscar productos en Etsy",
            "GET  /trends/etsy/trending": "Productos trending en categoría Etsy",
            "GET  /trends/alt/keyword": "🔍 Sustituto Google Trends — Bing+DuckDuckGo+Reddit",
            "GET  /trends/bing/keyword": "Volumen Bing Webmaster (sustituto cuantitativo)",
            "GET  /trends/reddit/search": "Menciones reales en subreddits + engagement",
        },
        "fallback_strategy": (
            "Si Google Trends falla o viene vacío, /trends/google activa "
            "automáticamente Wikipedia y RSS como fuentes alternativas."
        ),
        "auth": "Incluye header 'X-API-Key: <tu_clave>' en todas las peticiones",
    }


# ---------- Google Trends ----------

@app.get("/trends/google")
def google_trends_get(
    region: str = Query("ES"),
    timeframe: str = Query("now 7-d"),
    seeds: Optional[str] = Query(
        None, description="Lista separada por comas: 'bolso,zapatillas'"
    ),
    category: Optional[int] = Query(
        None,
        description="Opcional, se auto-detecta. 185=Ropa, 1036=Bolsos, 1076=Calzado, 44=Belleza, 0=Todas",
    ),
    fast_mode: bool = Query(
        False, description="Si true, responde en <5s sin reintentos. Ideal demos."
    ),
    enrich: bool = Query(
        True, description="Si true (default), añade metadatos y análisis agregado."
    ),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Versión GET con query params — ideal para Intelligence Studio / Zapier."""
    check_auth(x_api_key)
    seed_list = [s.strip() for s in seeds.split(",")] if seeds else None
    return get_google_trending_fashion(
        region=region, timeframe=timeframe, seeds=seed_list,
        category=category, fast_mode=fast_mode, enrich=enrich,
    )


@app.post("/trends/google")
def google_trends_post(
    body: GoogleTrendsRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Versión POST con JSON body — más limpia para clientes programáticos."""
    check_auth(x_api_key)
    return get_google_trending_fashion(
        region=body.region, timeframe=body.timeframe, seeds=body.seeds,
        category=body.category, fast_mode=body.fast_mode, enrich=body.enrich,
    )


# ---------- Brand Deep Dive ----------

@app.get("/trends/brand")
def brand_get(
    brand: str = Query(..., description="Nombre de la marca (swarovski, zara, nike)"),
    region: str = Query("ES"),
    timeframe: str = Query("today 1-m"),
    product_types: Optional[str] = Query(
        None, description="Productos separados por comas. Si se omite, usa defaults por idioma."
    ),
    fast_mode: bool = Query(True),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Análisis de una marca concreta combinada con tipos de producto."""
    check_auth(x_api_key)
    pt_list = [p.strip() for p in product_types.split(",")] if product_types else None
    return get_brand_deep_dive(
        brand=brand, region=region, timeframe=timeframe,
        product_types=pt_list, fast_mode=fast_mode,
    )


@app.post("/trends/brand")
def brand_post(
    body: BrandDeepDiveRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    check_auth(x_api_key)
    return get_brand_deep_dive(
        brand=body.brand, region=body.region, timeframe=body.timeframe,
        product_types=body.product_types, fast_mode=body.fast_mode,
    )


# ---------- Brand Full Intelligence (todo en uno) ----------

@app.get("/trends/brand/full")
def brand_full_get(
    brand: str = Query(..., description="Marca a analizar"),
    region: str = Query("US", description="ISO país (US, GB, IT, ES, DE, FR...)"),
    timeframe: str = Query("today 1-m"),
    timespan_gdelt: str = Query("7d"),
    include_competitors: bool = Query(False),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """🚀 Inteligencia completa: Google + Wikipedia + GDELT + eBay en paralelo.

    Una sola llamada → análisis cross-source pre-procesado.
    3x más rápido que llamar las 4 fuentes por separado.
    """
    check_auth(x_api_key)
    return get_brand_full_intelligence(
        brand=brand,
        region=region,
        timeframe=timeframe,
        timespan_gdelt=timespan_gdelt,
        include_competitors=include_competitors,
    )


@app.get("/trends/category/summary")
def category_summary_get(
    brands: str = Query(..., description="Marcas separadas por comas: 'swarovski,pandora,tous'"),
    region: str = Query("US"),
    timeframe: str = Query("today 1-m"),
    timespan_gdelt: str = Query("7d"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """📊 Panorama de varias marcas en paralelo: rankings + lecturas ejecutivas.

    Lanza brand_full_intelligence para todas las marcas en paralelo y devuelve
    análisis comparativo pre-calculado.
    """
    check_auth(x_api_key)
    brand_list = [b.strip() for b in brands.split(",") if b.strip()]
    return get_fashion_trends_summary(
        brands=brand_list,
        region=region,
        timeframe=timeframe,
        timespan_gdelt=timespan_gdelt,
    )


# ---------- Reddit ----------

@app.get("/trends/reddit")
def reddit_trends_get(
    time_filter: str = Query("week"),
    limit: int = Query(10),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    check_auth(x_api_key)
    return get_reddit_fashion_trends(limit=limit, time_filter=time_filter)


# ---------- Compare ----------

@app.post("/trends/compare")
def compare_post(
    body: CompareRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    check_auth(x_api_key)
    return compare_fashion_keywords(keywords=body.keywords, region=body.region)


# ---------- Wikipedia Pageviews ----------

@app.get("/trends/wikipedia")
def wikipedia_get(
    brands: str = Query(..., description="Marcas separadas por comas. Ej: 'gucci,prada,miu miu'"),
    region: str = Query("ES"),
    months: int = Query(3, ge=1, le=6),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Compara marcas con visualizaciones de Wikipedia (sin rate limits)."""
    check_auth(x_api_key)
    brand_list = [b.strip() for b in brands.split(",") if b.strip()]
    return compare_brands_wikipedia(brands=brand_list, region=region, months=months)


# ---------- Fashion Editorial RSS ----------

@app.get("/trends/editorial")
def editorial_get(
    region: str = Query("US"),
    items_per_source: int = Query(5, ge=1, le=10),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Titulares recientes de Vogue, Elle, WWD, Business of Fashion."""
    check_auth(x_api_key)
    return get_fashion_editorial(region=region, items_per_source=items_per_source)


# ---------- GDELT Global Media ----------

@app.get("/trends/gdelt/brand")
def gdelt_brand_get(
    brand: str = Query(..., description="Marca a analizar en prensa global"),
    region: Optional[str] = Query(None, description="Código ISO (opcional, global si se omite)"),
    timespan: str = Query("7d", description="'24h', '3d', '7d', '1w', '1m', '3m'"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Señal completa de una marca en prensa global: volumen + tono + artículos."""
    check_auth(x_api_key)
    return brand_gdelt_full_signal(brand=brand, region=region, timespan=timespan)


@app.get("/trends/gdelt/compare")
def gdelt_compare_get(
    brands: str = Query(..., description="Marcas separadas por comas: 'gucci,prada,miu miu'"),
    region: Optional[str] = Query(None),
    timespan: str = Query("7d"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Compara volumen de cobertura mediática entre marcas (prensa global)."""
    check_auth(x_api_key)
    brand_list = [b.strip() for b in brands.split(",") if b.strip()]
    return compare_brands_gdelt(brands=brand_list, region=region, timespan=timespan)


# ---------- eBay (datos reales de comercio) ----------

@app.get("/trends/ebay/brand")
def ebay_brand_get(
    brand: str = Query(..., description="Marca a analizar"),
    region: Optional[str] = Query(None, description="ISO (US, GB, IT, ES, DE...)"),
    limit: int = Query(30, ge=1, le=50),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Datos de comercio de una marca en eBay: listings, precios, vendedores."""
    check_auth(x_api_key)
    return get_ebay_brand_signal(brand=brand, region=region, limit=limit)


@app.get("/trends/ebay/compare")
def ebay_compare_get(
    brands: str = Query(..., description="Marcas separadas por comas"),
    region: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Compara marcas por volumen de listings y precio medio en eBay."""
    check_auth(x_api_key)
    brand_list = [b.strip() for b in brands.split(",") if b.strip()]
    return compare_brands_ebay(brands=brand_list, region=region)


@app.get("/trends/ebay/search")
def ebay_search_get(
    keyword: str = Query(..., description="Términos de búsqueda"),
    region: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=50),
    sort: str = Query("best_match"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Busca productos específicos en eBay con título, precio, vendedor, etc."""
    check_auth(x_api_key)
    return search_ebay_products(
        keyword=keyword, region=region, limit=limit, sort=sort
    )


# ---------- Etsy (marketplace artesanal/handmade/vintage) ----------

@app.get("/trends/etsy/brand")
def etsy_brand_get(
    brand: str = Query(..., description="Marca a analizar"),
    region: Optional[str] = Query(None),
    limit: int = Query(30, ge=1, le=100),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """🎨 Señal de marca en Etsy: listings, precios, popularidad, tags."""
    check_auth(x_api_key)
    return get_etsy_brand_signal(brand=brand, region=region, limit=limit)


@app.get("/trends/etsy/compare")
def etsy_compare_get(
    brands: str = Query(..., description="Marcas separadas por comas"),
    region: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Compara marcas por listings, precio medio y popularidad en Etsy."""
    check_auth(x_api_key)
    brand_list = [b.strip() for b in brands.split(",") if b.strip()]
    return compare_brands_etsy(brands=brand_list, region=region)


@app.get("/trends/etsy/search")
def etsy_search_get(
    keyword: str = Query(..., description="Términos de búsqueda"),
    region: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    sort_on: str = Query("score"),
    sort_order: str = Query("desc"),
    language: Optional[str] = Query(None, description="'en', 'es', 'it'... o 'any'. Auto-detecta si se omite."),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Busca productos específicos en Etsy."""
    check_auth(x_api_key)
    return search_etsy_products(
        keyword=keyword,
        region=region,
        limit=limit,
        sort_on=sort_on,
        sort_order=sort_order,
        language=language,
    )


@app.get("/trends/etsy/trending")
def etsy_trending_get(
    category: str = Query("jewelry"),
    region: Optional[str] = Query(None),
    limit: int = Query(30, ge=1, le=100),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Detecta productos trending en una categoría de Etsy."""
    check_auth(x_api_key)
    return get_etsy_trending_in_category(
        category=category, region=region, limit=limit
    )


# ---------- Search Alternatives (Bing + DuckDuckGo + Reddit) ----------

@app.get("/trends/alt/keyword")
def alt_keyword_get(
    keyword: str = Query(..., description="Término a investigar"),
    region: str = Query("US"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """🔍 Sustituto de Google Trends — Bing + DuckDuckGo + Reddit en paralelo."""
    check_auth(x_api_key)
    return search_alternatives_combined(keyword=keyword, region=region)


@app.get("/trends/bing/keyword")
def bing_keyword_get(
    keyword: str = Query(...),
    region: str = Query("US"),
    months: int = Query(6, ge=1, le=12),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Volumen mensual de búsqueda en Bing Webmaster API."""
    check_auth(x_api_key)
    return get_bing_keyword_volume(keyword=keyword, region=region, months=months)


@app.get("/trends/reddit/search")
def reddit_search_get(
    query: str = Query(..., description="Término a buscar en subreddits"),
    time_filter: str = Query("month"),
    limit: int = Query(100, ge=10, le=100),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Cuenta menciones reales en subreddits de moda y devuelve engagement."""
    check_auth(x_api_key)
    return reddit_search_volume_wrap(
        query=query, time_filter=time_filter, limit=limit
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log(f"🚀 Fashion Trends REST API en http://{HOST}:{PORT}")
    log(f"   Docs interactivas: http://{HOST}:{PORT}/docs")
    log(f"   API keys configuradas: {len(API_KEYS)}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
