"""
Etsy Open API v3 integration.
------------------------------
Datos REALES de marketplace artesanal/vintage: productos, precios, shops, reviews.

Complementa a eBay con la perspectiva de "moda artesanal/handmade" — segunda
mano, vintage, joyería independiente, accesorios únicos. Etsy es donde están
las tendencias antes de llegar al mainstream.

Auth: API Key (keystring) en header `x-api-key`. NO necesita OAuth para
datos públicos de listings y shops.

Variables de entorno requeridas en .env:
    ETSY_API_KEY = tu API keystring
    ETSY_SHARED_SECRET = (opcional) solo para flujos OAuth, no usado aquí

Docs: https://developers.etsy.com/documentation/reference
Rate limits: 10 req/segundo, 10.000 req/día.
"""

import os
import sys
import json
import urllib.request
import urllib.parse
from datetime import datetime
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed


def _log(msg: str) -> None:
    print(f"[etsy] {msg}", file=sys.stderr, flush=True)


# Configuración
ETSY_API_BASE = "https://openapi.etsy.com/v3/application"
ETSY_API_KEY = os.getenv("ETSY_API_KEY", "")
ETSY_SHARED_SECRET = os.getenv("ETSY_SHARED_SECRET", "")


def _build_auth_header() -> str:
    """
    Construye el header x-api-key con o sin shared secret.

    Etsy acepta dos formatos según el tipo de aplicación:
    - Solo API key: para apps tipo PUBLIC (poco común)
    - API key + Shared secret separados por ':': para apps PRIVATE/personales

    Si tienes el shared secret en el .env, se usa el formato combinado
    que es lo que la mayoría de apps aprobadas necesitan.
    """
    if ETSY_SHARED_SECRET:
        return f"{ETSY_API_KEY}:{ETSY_SHARED_SECRET}"
    return ETSY_API_KEY

# Mapeo región ISO → región Etsy. Etsy filtra ventas y envíos por país,
# pero los listings son globales. El filtro de region se aplica vía
# ship_to / location_query en algunos endpoints.
REGION_TO_ETSY = {
    "US": "US", "GB": "GB", "UK": "GB",
    "DE": "DE", "AT": "AT", "CH": "CH",
    "IT": "IT", "ES": "ES", "FR": "FR",
    "NL": "NL", "BE": "BE", "PT": "PT",
    "AU": "AU", "CA": "CA", "JP": "JP",
    "BR": "BR", "MX": "MX", "AR": "AR",
}

# Idiomas Etsy soportados para keywords
ETSY_LANGUAGES = {
    "ES": "es", "MX": "es", "AR": "es",
    "IT": "it",
    "FR": "fr", "BE": "fr",
    "DE": "de", "AT": "de",
    "GB": "en", "UK": "en", "US": "en", "AU": "en",
    "PT": "pt", "BR": "pt",
    "NL": "nl",
    "JP": "ja",
}


# ---------------------------------------------------------------------------
# Petición base con auth por API key
# ---------------------------------------------------------------------------

def _etsy_request(endpoint: str, params: dict = None) -> dict | None:
    """Hace una petición autenticada a Etsy Open API v3."""
    if not ETSY_API_KEY:
        return None

    if params is None:
        params = {}

    url = f"{ETSY_API_BASE}/{endpoint}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(
        url,
        headers={
            "x-api-key": _build_auth_header(),
            "Accept": "application/json",
            "User-Agent": "fashion-trends-mcp/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()[:300]
        except Exception:
            err_body = str(e)
        _log(f"HTTP {e.code} en {endpoint}: {err_body[:200]}")
        return None
    except Exception as e:
        _log(f"Error en {endpoint}: {e}")
        return None


def _resolve_language(region: str | None) -> str | None:
    if not region:
        return None
    return ETSY_LANGUAGES.get(region.upper())


# ---------------------------------------------------------------------------
# Funciones públicas
# ---------------------------------------------------------------------------

def search_etsy_products(
    keyword: str,
    region: str | None = None,
    limit: int = 20,
    sort_on: str = "score",
    sort_order: str = "desc",
    min_price: float | None = None,
    max_price: float | None = None,
    language: str | None = None,
) -> dict:
    """
    Busca listings activos en Etsy por keyword.

    keyword: texto de búsqueda (ej: "swarovski necklace", "vintage dress")
    region: código ISO (US, GB, IT, ES, DE...). Filtra por ship_to.
    limit: número de listings (1-100, default 20)
    sort_on: 'score' (relevance), 'price', 'created', 'updated'
    sort_order: 'asc' o 'desc'
    min_price / max_price: filtros opcionales
    language: filtra por idioma del listing ('en', 'es', 'it', 'de', 'fr'...).
              Si es None, se deduce automáticamente de la región.
              Pasa 'any' (o cadena vacía) para NO filtrar por idioma.
    """
    params = {
        "keywords": keyword,
        "limit": min(limit, 100),
        "sort_on": sort_on,
        "sort_order": sort_order,
    }

    if region:
        ship_to = REGION_TO_ETSY.get(region.upper())
        if ship_to:
            params["ship_to"] = ship_to

    # Filtro por idioma del listing
    # - None (default) → auto-detecta el idioma de la región
    # - "any" o "" → no filtra (devuelve todos los idiomas)
    # - código concreto ('en', 'es', etc.) → filtra por ese idioma
    if language is None:
        auto_lang = _resolve_language(region)
        if auto_lang:
            params["language"] = auto_lang
    elif language and language.lower() not in ("any", ""):
        params["language"] = language

    if min_price is not None:
        params["min_price"] = min_price
    if max_price is not None:
        params["max_price"] = max_price

    lang_log = params.get("language", "any")
    _log(f"🔍 Búsqueda: '{keyword}' (sort={sort_on}, region={region or 'global'}, lang={lang_log})")
    data = _etsy_request("listings/active", params)
    if data is None:
        return {"keyword": keyword, "error": "request failed", "source": "etsy"}

    listings = data.get("results", [])
    simplified = []

    for item in listings:
        price = item.get("price", {})
        # Etsy usa formato {amount, divisor, currency_code} para precios
        # ej: {amount: 1500, divisor: 100, currency_code: "EUR"} = 15.00 EUR
        price_value = None
        if isinstance(price, dict):
            amt = price.get("amount")
            div = price.get("divisor", 100)
            if amt is not None and div:
                try:
                    price_value = round(float(amt) / float(div), 2)
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

        simplified.append({
            "listing_id": item.get("listing_id"),
            "title": item.get("title"),
            "price": price_value,
            "currency": price.get("currency_code") if isinstance(price, dict) else None,
            "url": item.get("url"),
            "shop_id": item.get("shop_id"),
            "num_favorers": item.get("num_favorers"),
            "views": item.get("views"),
            "tags": item.get("tags", [])[:8],
            "materials": item.get("materials", [])[:5],
            "made_to_order": item.get("is_personalizable"),
            "creation_tsz": item.get("created_timestamp"),
            "state": item.get("state"),
        })

    # Estadísticas rápidas
    prices = [s["price"] for s in simplified if s.get("price")]
    avg_price = sum(prices) / len(prices) if prices else None

    favorers = [s["num_favorers"] for s in simplified if s.get("num_favorers")]
    total_favorers = sum(favorers) if favorers else 0
    avg_favorers = sum(favorers) / len(favorers) if favorers else 0

    # Top tags más comunes (señal cualitativa de a qué se asocia el keyword)
    all_tags = []
    for s in simplified:
        all_tags.extend(s.get("tags", []))
    top_tags = Counter(all_tags).most_common(10)

    return {
        "keyword": keyword,
        "region": region,
        "total_found": data.get("count"),
        "items_returned": len(simplified),
        "average_price": round(avg_price, 2) if avg_price else None,
        "price_range": (
            {"min": min(prices), "max": max(prices)} if prices else None
        ),
        "total_favorers": total_favorers,
        "average_favorers_per_listing": round(avg_favorers, 1),
        "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
        "items": simplified,
        "source": "etsy",
    }


def get_etsy_brand_signal(
    brand: str,
    region: str | None = None,
    limit: int = 30,
) -> dict:
    """
    Señal de comercio de una marca en Etsy: productos disponibles, precios,
    shops más activos, distribución y popularidad (favorers).

    Hace 3 búsquedas en paralelo:
      - Score (relevancia, productos más buscados)
      - Newest (recién subidos, lo que sigue añadiéndose)
      - Top price (productos premium de la marca)
    """
    _log(f"📊 Señal Etsy para '{brand}' en {region or 'global'}")

    queries = {
        "best_match": ("score", "desc", limit),
        "newest": ("created", "desc", limit // 2),
        "premium": ("price", "desc", 10),
    }

    results: dict = {
        "brand": brand,
        "region": region,
        "source": "etsy_brand_signal",
    }

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                search_etsy_products,
                brand, region, lim, sort_on, sort_order
            ): name
            for name, (sort_on, sort_order, lim) in queries.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result(timeout=20)
            except Exception as e:
                results[name] = {"error": str(e)[:120]}

    # Cálculos transversales
    all_prices = []
    all_shops: Counter[int] = Counter()
    all_favorers = []
    all_tags: list[str] = []

    for key in ("best_match", "newest", "premium"):
        sub = results.get(key, {})
        items = sub.get("items", []) if isinstance(sub, dict) else []
        for it in items:
            if it.get("price"):
                try:
                    all_prices.append(float(it["price"]))
                except (TypeError, ValueError):
                    pass
            if it.get("shop_id"):
                all_shops[it["shop_id"]] += 1
            if it.get("num_favorers"):
                all_favorers.append(it["num_favorers"])
            all_tags.extend(it.get("tags", []))

    if all_prices:
        all_prices_sorted = sorted(all_prices)
        results["price_summary"] = {
            "min": min(all_prices),
            "max": max(all_prices),
            "median": all_prices_sorted[len(all_prices_sorted) // 2],
            "average": round(sum(all_prices) / len(all_prices), 2),
            "total_items_with_price": len(all_prices),
        }

    if all_favorers:
        results["popularity_summary"] = {
            "total_favorers": sum(all_favorers),
            "average_favorers": round(sum(all_favorers) / len(all_favorers), 1),
            "max_favorers_single_listing": max(all_favorers),
        }

    results["top_shops_aggregated"] = [
        {"shop_id": s, "listings": c} for s, c in all_shops.most_common(8)
    ]

    if all_tags:
        results["top_tags_aggregated"] = [
            {"tag": t, "count": c} for t, c in Counter(all_tags).most_common(15)
        ]

    return results


def compare_brands_etsy(
    brands: list[str],
    region: str | None = None,
) -> dict:
    """
    Compara varias marcas en Etsy por volumen de listings y precio medio.
    Útil para ver qué marca tiene más oferta en el mercado artesanal.
    """
    if len(brands) > 6:
        brands = brands[:6]

    _log(f"⚖️  Comparando en Etsy ({region or 'global'}): {brands}")

    def _quick_check(brand: str) -> dict:
        data = search_etsy_products(brand, region, limit=20, sort_on="score")
        if "error" in data:
            return {"brand": brand, "error": data["error"]}
        return {
            "brand": brand,
            "total_listings": data.get("total_found"),
            "average_price": data.get("average_price"),
            "price_range": data.get("price_range"),
            "total_favorers_top20": data.get("total_favorers"),
            "top_tag": (
                data["top_tags"][0]["tag"]
                if data.get("top_tags") else None
            ),
        }

    ranking: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(brands), 4)) as executor:
        futures = {
            executor.submit(_quick_check, brand): brand for brand in brands
        }
        for future in as_completed(futures):
            try:
                ranking.append(future.result(timeout=20))
            except Exception as e:
                ranking.append({"brand": futures[future], "error": str(e)[:100]})

    # Ordenar por volumen de listings descendente
    ranking_with_data = [r for r in ranking if "total_listings" in r and r["total_listings"]]
    ranking_with_data.sort(
        key=lambda x: x.get("total_listings") or 0, reverse=True
    )

    return {
        "region": region or "global",
        "ranking": ranking_with_data,
        "errors": [r for r in ranking if "error" in r],
        "source": "etsy_brand_comparison",
    }


def get_etsy_trending_in_category(
    category: str = "jewelry",
    region: str | None = None,
    limit: int = 30,
) -> dict:
    """
    Detecta lo que está trending en una categoría de Etsy.
    Combina 'top scored' + 'most recent' para detectar productos que están
    subiendo rápido (mucho score + reciente = momentum real).

    category: keyword genérico de la categoría. Ej: 'jewelry', 'vintage dress',
              'handmade bag', 'shoes', 'accessories'
    """
    _log(f"📈 Trending en '{category}' (Etsy {region or 'global'})")

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_top = executor.submit(
            search_etsy_products, category, region, limit, "score", "desc"
        )
        f_new = executor.submit(
            search_etsy_products, category, region, limit, "created", "desc"
        )
        try:
            top = f_top.result(timeout=15)
        except Exception as e:
            top = {"error": str(e)[:100]}
        try:
            new = f_new.result(timeout=15)
        except Exception as e:
            new = {"error": str(e)[:100]}

    # Items que aparecen tanto en "top score" como en "newest" son los más
    # interesantes — productos recientes con alta tracción
    top_ids = {it["listing_id"] for it in top.get("items", []) if it.get("listing_id")}
    new_ids = {it["listing_id"] for it in new.get("items", []) if it.get("listing_id")}
    momentum_ids = top_ids & new_ids

    momentum_items = [
        it for it in top.get("items", [])
        if it.get("listing_id") in momentum_ids
    ]

    # Tags agregados de top + new para ver qué subcategorías están subiendo
    all_tags: list[str] = []
    for it in top.get("items", []) + new.get("items", []):
        all_tags.extend(it.get("tags", []))

    return {
        "category": category,
        "region": region or "global",
        "top_scored": top.get("items", [])[:10],
        "newest_high_score": momentum_items[:10],
        "trending_tags": [
            {"tag": t, "count": c}
            for t, c in Counter(all_tags).most_common(20)
        ],
        "total_top_listings": top.get("total_found"),
        "source": "etsy_trending_category",
    }
