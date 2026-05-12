"""
eBay Browse API integration.
-----------------------------
Datos REALES de comercio: productos buscados, precios, watchers, ventas.

Complementa a Google/Wikipedia/GDELT con la perspectiva de "qué se está
COMPRANDO de verdad", no solo lo que se busca o se publica en medios.

Auth: OAuth 2.0 Application Token (sin usuario). Los tokens duran 2 horas
y se cachean en memoria automáticamente.

Variables de entorno requeridas en .env:
    EBAY_APP_ID       = Client ID
    EBAY_CERT_ID      = Client Secret
    EBAY_MARKETPLACE  = (opcional) EBAY_US, EBAY_GB, EBAY_IT, EBAY_ES, EBAY_DE...
                        Default: EBAY_US

Docs: https://developer.ebay.com/api-docs/buy/browse/overview.html
"""

import os
import sys
import json
import base64
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed


def _log(msg: str) -> None:
    print(f"[ebay] {msg}", file=sys.stderr, flush=True)


# Configuración
EBAY_APP_ID = os.getenv("EBAY_APP_ID", "")
EBAY_CERT_ID = os.getenv("EBAY_CERT_ID", "")
EBAY_DEFAULT_MARKETPLACE = os.getenv("EBAY_MARKETPLACE", "EBAY_US")

# Mapeo región ISO → marketplace ID de eBay
REGION_TO_EBAY_MARKETPLACE = {
    "US": "EBAY_US",
    "GB": "EBAY_GB", "UK": "EBAY_GB",
    "DE": "EBAY_DE", "AT": "EBAY_AT", "CH": "EBAY_CH",
    "IT": "EBAY_IT",
    "ES": "EBAY_ES",
    "FR": "EBAY_FR",
    "NL": "EBAY_NL",
    "AU": "EBAY_AU",
    "CA": "EBAY_ENCA",
    "IE": "EBAY_IE",
    "BE": "EBAY_BEFR",
    "PL": "EBAY_PL",
}

# Cache del token en memoria
_token_cache: dict = {"token": None, "expires_at": None}


# ---------------------------------------------------------------------------
# Autenticación OAuth 2.0 Application Token
# ---------------------------------------------------------------------------

def _get_app_token() -> str | None:
    """
    Obtiene un access token de tipo "Application" (sin usuario).
    Cachea el token hasta que expira (~2h).
    """
    now = datetime.now()
    if _token_cache["token"] and _token_cache["expires_at"]:
        if now < _token_cache["expires_at"] - timedelta(minutes=5):
            return _token_cache["token"]

    if not EBAY_APP_ID or not EBAY_CERT_ID:
        _log("⚠️  Faltan EBAY_APP_ID o EBAY_CERT_ID en .env")
        return None

    _log("🔑 Solicitando nuevo access token a eBay...")
    auth_str = f"{EBAY_APP_ID}:{EBAY_CERT_ID}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()

    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }).encode()

    req = urllib.request.Request(
        "https://api.ebay.com/identity/v1/oauth2/token",
        data=body,
        headers={
            "Authorization": f"Basic {auth_b64}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        token = data.get("access_token")
        expires_in = data.get("expires_in", 7200)
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + timedelta(seconds=expires_in)
        _log(f"✅ Token obtenido, válido por {expires_in // 60} min")
        return token
    except Exception as e:
        _log(f"❌ Error autenticando: {e}")
        return None


def _ebay_request(path: str, params: dict, marketplace: str) -> dict | None:
    """Hace una petición autenticada a la Browse API."""
    token = _get_app_token()
    if not token:
        return None

    url = f"https://api.ebay.com{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()
        except Exception:
            err_body = str(e)
        _log(f"HTTP {e.code} en {path}: {err_body[:200]}")
        return None
    except Exception as e:
        _log(f"Error en {path}: {e}")
        return None


def _resolve_marketplace(region: str | None) -> str:
    if not region:
        return EBAY_DEFAULT_MARKETPLACE
    return REGION_TO_EBAY_MARKETPLACE.get(
        region.upper(), EBAY_DEFAULT_MARKETPLACE
    )


# ---------------------------------------------------------------------------
# Funciones públicas
# ---------------------------------------------------------------------------

def search_ebay_products(
    keyword: str,
    region: str | None = None,
    limit: int = 20,
    sort: str = "best_match",
    category_id: str | None = None,
) -> dict:
    """
    Busca productos en eBay por keyword.

    keyword: texto a buscar (ej: "swarovski necklace", "borsa pelle")
    region: código ISO (US, GB, IT, ES, DE...). Determina el marketplace.
    limit: número de productos (1-200, default 20)
    sort: 'best_match', 'price', '-price', 'newlyListed', 'endingSoonest'
    category_id: opcional, filtra por categoría eBay (ej: '281' = jewelry)
    """
    marketplace = _resolve_marketplace(region)
    params = {
        "q": keyword,
        "limit": str(min(limit, 50)),
        "sort": sort,
    }
    if category_id:
        params["category_ids"] = category_id

    _log(f"🔍 Búsqueda: '{keyword}' en {marketplace} (sort={sort})")
    data = _ebay_request("/buy/browse/v1/item_summary/search", params, marketplace)
    if data is None:
        return {"keyword": keyword, "error": "request failed"}

    items = data.get("itemSummaries", [])
    simplified = []
    for item in items:
        price = item.get("price", {})
        seller = item.get("seller", {})
        simplified.append({
            "title": item.get("title"),
            "price": price.get("value"),
            "currency": price.get("currency"),
            "condition": item.get("condition"),
            "seller_username": seller.get("username"),
            "seller_feedback_score": seller.get("feedbackScore"),
            "seller_feedback_pct": seller.get("feedbackPercentage"),
            "buying_options": item.get("buyingOptions", []),
            "image": item.get("image", {}).get("imageUrl"),
            "item_url": item.get("itemWebUrl"),
            "categories": [c.get("categoryName") for c in item.get("categories", [])],
            "shipping_cost": item.get("shippingOptions", [{}])[0].get(
                "shippingCost", {}
            ).get("value") if item.get("shippingOptions") else None,
        })

    # Estadísticas rápidas: precios y vendedores
    prices = [float(s["price"]) for s in simplified if s.get("price")]
    avg_price = sum(prices) / len(prices) if prices else None
    sellers = Counter(s["seller_username"] for s in simplified if s.get("seller_username"))

    return {
        "keyword": keyword,
        "marketplace": marketplace,
        "total_found": data.get("total"),
        "items_returned": len(simplified),
        "average_price": round(avg_price, 2) if avg_price else None,
        "price_range": (
            {"min": min(prices), "max": max(prices)} if prices else None
        ),
        "top_sellers": [{"username": u, "listings": c} for u, c in sellers.most_common(5)],
        "items": simplified,
        "source": "ebay_browse",
    }


def get_ebay_brand_signal(
    brand: str,
    region: str | None = None,
    limit: int = 30,
) -> dict:
    """
    Señal de comercio de una marca en eBay: productos disponibles, precios,
    distribución y top vendedores.

    Hace 3 búsquedas en paralelo:
      - Best match (relevancia general)
      - Newly listed (lo recién subido = lo que la gente sigue ofertando)
      - Sorted by price descending (los productos premium de la marca)
    """
    marketplace = _resolve_marketplace(region)
    _log(f"📊 Señal eBay para '{brand}' en {marketplace}")

    queries = {
        "best_match": ("best_match", limit),
        "newest": ("newlyListed", limit // 2),
        "premium": ("-price", 10),
    }

    results: dict = {
        "brand": brand,
        "marketplace": marketplace,
        "source": "ebay_brand_signal",
    }

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(search_ebay_products, brand, region, lim, sort): name
            for name, (sort, lim) in queries.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result(timeout=20)
            except Exception as e:
                results[name] = {"error": str(e)[:120]}

    # Cálculos transversales
    all_prices = []
    all_sellers: Counter[str] = Counter()
    for key in ("best_match", "newest", "premium"):
        sub = results.get(key, {})
        items = sub.get("items", []) if isinstance(sub, dict) else []
        for it in items:
            if it.get("price"):
                try:
                    all_prices.append(float(it["price"]))
                except (TypeError, ValueError):
                    pass
            if it.get("seller_username"):
                all_sellers[it["seller_username"]] += 1

    if all_prices:
        all_prices_sorted = sorted(all_prices)
        results["price_summary"] = {
            "min": min(all_prices),
            "max": max(all_prices),
            "median": all_prices_sorted[len(all_prices_sorted) // 2],
            "average": round(sum(all_prices) / len(all_prices), 2),
            "total_items_with_price": len(all_prices),
        }
    results["top_sellers_aggregated"] = [
        {"username": u, "listings": c} for u, c in all_sellers.most_common(8)
    ]

    return results


def compare_brands_ebay(
    brands: list[str],
    region: str | None = None,
) -> dict:
    """
    Compara varias marcas en eBay por volumen de listings y precio medio.
    Excelente para ver quién tiene más oferta real en el mercado.
    """
    if len(brands) > 6:
        brands = brands[:6]

    marketplace = _resolve_marketplace(region)
    _log(f"⚖️  Comparando en eBay {marketplace}: {brands}")

    def _quick_check(brand: str) -> dict:
        # Solo una búsqueda con 20 items para velocidad
        data = search_ebay_products(brand, region, limit=20, sort="best_match")
        if "error" in data:
            return {"brand": brand, "error": data["error"]}
        return {
            "brand": brand,
            "total_listings": data.get("total_found"),
            "average_price": data.get("average_price"),
            "price_range": data.get("price_range"),
            "top_seller": (
                data["top_sellers"][0]["username"]
                if data.get("top_sellers") else None
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
        "marketplace": marketplace,
        "ranking": ranking_with_data,
        "errors": [r for r in ranking if "error" in r],
        "source": "ebay_brand_comparison",
    }
