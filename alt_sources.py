"""
Fuentes alternativas a Google Trends.
-------------------------------------
Cuando Google falla o viene vacío, estos módulos proveen señales alternativas:

1. Wikipedia Pageviews — visualizaciones mensuales de páginas de marcas/productos
   por país. API oficial, sin rate limits prácticos.

2. Fashion Editorial RSS — titulares recientes de Vogue, Elle, Harper's Bazaar,
   WWD, Business of Fashion. Contexto editorial para el LLM.

Ambas son gratis y no requieren API key.
"""

import json
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[alt-sources] {msg}", file=sys.stderr, flush=True)


# ===========================================================================
# 1. WIKIPEDIA PAGEVIEWS
# ===========================================================================
# API oficial: https://wikimedia.org/api/rest_v1/
# No requiere auth, sin rate limits razonables (1000 req/hora).
# Útil para medir interés por una marca a lo largo del tiempo.

WIKI_USER_AGENT = "fashion-trends-mcp/1.0 (contact: demo@example.com)"

# Mapeo de región ISO → idioma Wikipedia
WIKI_LANG_BY_REGION = {
    "ES": "es", "MX": "es", "AR": "es", "CO": "es", "PE": "es",
    "IT": "it",
    "FR": "fr", "BE": "fr",
    "DE": "de", "AT": "de", "CH": "de",
    "US": "en", "UK": "en", "GB": "en", "IE": "en", "AU": "en",
    "PT": "pt", "BR": "pt",
    "NL": "nl",
}


def get_wikipedia_pageviews(
    brand: str, region: str = "ES", months: int = 3
) -> dict:
    """
    Devuelve las visualizaciones mensuales de la página Wikipedia de una marca.

    brand: nombre de la marca (ej: "Gucci", "Swarovski")
    region: código ISO — determina el idioma de Wikipedia a consultar
    months: cuántos meses hacia atrás (1-6)

    Retorna estructura comparable por marca para que el LLM detecte tendencias.
    """
    lang = WIKI_LANG_BY_REGION.get(region.upper(), "en")
    # URL-encode el nombre de marca (espacios → guiones bajos es convención Wiki)
    article = urllib.parse.quote(brand.replace(" ", "_"))

    # Calcular rango de fechas (formato YYYYMMDD)
    end = datetime.now().replace(day=1) - timedelta(days=1)
    start = (end - timedelta(days=30 * months)).replace(day=1)

    url = (
        f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
        f"{lang}.wikipedia/all-access/all-agents/{article}/monthly/"
        f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}"
    )

    req = urllib.request.Request(url)
    req.add_header("User-Agent", WIKI_USER_AGENT)

    try:
        _log(f"Wiki pageviews: {brand} ({lang})")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("items", [])
        if not items:
            return {"brand": brand, "lang": lang, "found": False, "views": []}

        # Devolver solo lo esencial: mes + visitas
        views = [
            {
                "month": item["timestamp"][:6],  # YYYYMM
                "views": item["views"],
            }
            for item in items
        ]

        # Calcular crecimiento del último mes vs penúltimo
        trend = None
        if len(views) >= 2:
            last, prev = views[-1]["views"], views[-2]["views"]
            if prev > 0:
                pct = ((last - prev) / prev) * 100
                trend = f"{'+' if pct >= 0 else ''}{pct:.1f}%"

        return {
            "brand": brand,
            "lang": lang,
            "found": True,
            "views_per_month": views,
            "last_month_vs_previous": trend,
            "source": "wikipedia_pageviews",
        }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"brand": brand, "lang": lang, "found": False, "error": "Página no existe en Wikipedia"}
        return {"brand": brand, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"brand": brand, "error": str(e)}


def compare_brands_wikipedia(
    brands: list[str], region: str = "ES", months: int = 3
) -> dict:
    """Compara múltiples marcas usando Wikipedia pageviews.

    Paraleliza las consultas: N marcas a la vez en vez de secuencial.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if len(brands) > 8:
        brands = brands[:8]

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(brands), 6)) as executor:
        future_to_brand = {
            executor.submit(get_wikipedia_pageviews, brand, region, months): brand
            for brand in brands
        }
        for future in as_completed(future_to_brand):
            brand = future_to_brand[future]
            try:
                data = future.result(timeout=15)
            except Exception as e:
                results.append({"brand": brand, "error": str(e)[:100]})
                continue

            if data.get("found"):
                total = sum(v["views"] for v in data["views_per_month"])
                results.append({
                    "brand": brand,
                    "total_views": total,
                    "trend": data.get("last_month_vs_previous"),
                    "monthly": data["views_per_month"],
                })
            else:
                results.append({
                    "brand": brand,
                    "error": data.get("error", "sin datos"),
                })

    # Ordenar por total de visualizaciones descendente
    results_with_data = [r for r in results if "total_views" in r]
    results_with_data.sort(key=lambda x: x["total_views"], reverse=True)

    return {
        "region": region,
        "lang": WIKI_LANG_BY_REGION.get(region.upper(), "en"),
        "months_analyzed": months,
        "ranking": results_with_data,
        "errors": [r for r in results if "error" in r],
        "source": "wikipedia_pageviews",
    }


# ===========================================================================
# 2. FASHION EDITORIAL RSS
# ===========================================================================
# Fuentes RSS de medios de moda. Parser RSS manual sin dependencias externas.

FASHION_RSS_FEEDS = {
    "vogue": "https://www.vogue.com/feed/rss",
    "elle": "https://www.elle.com/rss/all.xml/",
    "harpers_bazaar": "https://www.harpersbazaar.com/rss/all.xml/",
    "wwd": "https://wwd.com/feed/",
    "business_of_fashion": "https://www.businessoffashion.com/feed/",
    "fashionista": "https://fashionista.com/.rss/full/",
    # Medios italianos
    "vogue_italia": "https://www.vogue.it/rss",
    # Medios españoles
    "vogue_spain": "https://www.vogue.es/feed/rss",
}


def _parse_rss_items(xml_text: str, limit: int = 10) -> list[dict]:
    """Parser RSS minimalista: extrae título, link, fecha y descripción."""
    import re
    items = []
    # Extraer bloques <item>...</item>
    for match in re.finditer(r"<item[^>]*>(.*?)</item>", xml_text, re.DOTALL | re.IGNORECASE):
        block = match.group(1)

        def grab(tag):
            m = re.search(
                rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>",
                block,
                re.DOTALL | re.IGNORECASE,
            )
            return m.group(1).strip() if m else None

        title = grab("title")
        link = grab("link")
        pubdate = grab("pubDate") or grab("dc:date")
        desc = grab("description")

        if title and link:
            # Limpiar HTML tags del description
            if desc:
                desc = re.sub(r"<[^>]+>", "", desc)
                desc = desc[:200].strip()  # primeras 200 chars
            items.append({
                "title": title,
                "link": link,
                "published": pubdate,
                "summary": desc,
            })
            if len(items) >= limit:
                break
    return items


def get_fashion_editorial(
    sources: list[str] | None = None,
    region: str = "US",
    items_per_source: int = 5,
) -> dict:
    """
    Obtiene titulares recientes de medios de moda vía RSS.

    Paraleliza las consultas a cada feed para reducir latencia total
    de ~10s (secuencial, 3 feeds) a ~3s (paralelo).

    sources: lista de medios (ver FASHION_RSS_FEEDS). Si no se pasa, elige
             automáticamente según la región.
    region: código ISO — influye en los medios por defecto
    items_per_source: titulares a devolver por medio (1-10)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if sources is None:
        region_up = region.upper()
        if region_up == "IT":
            sources = ["vogue_italia", "vogue", "business_of_fashion"]
        elif region_up in ("ES", "MX", "AR"):
            sources = ["vogue_spain", "vogue", "business_of_fashion"]
        else:
            sources = ["vogue", "elle", "harpers_bazaar", "wwd", "business_of_fashion"]

    def _fetch_single(source: str) -> tuple[str, list, str | None]:
        """Descarga y parsea un feed. Devuelve (source, items, error_msg)."""
        url = FASHION_RSS_FEEDS.get(source)
        if not url:
            return source, [], f"{source}: fuente desconocida"

        try:
            _log(f"RSS: {source}")
            req = urllib.request.Request(url, headers={"User-Agent": WIKI_USER_AGENT})
            with urllib.request.urlopen(req, timeout=12) as resp:
                xml_text = resp.read().decode("utf-8", errors="replace")
            items = _parse_rss_items(xml_text, limit=items_per_source)
            return source, items, None
        except Exception as e:
            return source, [], f"{source}: {str(e)[:80]}"

    results: dict[str, list] = {}
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=min(len(sources), 5)) as executor:
        futures = [executor.submit(_fetch_single, src) for src in sources]
        for future in as_completed(futures):
            try:
                source, items, error = future.result(timeout=15)
                results[source] = items
                if error:
                    errors.append(error)
            except Exception as e:
                errors.append(f"timeout: {str(e)[:80]}")

    total = sum(len(v) for v in results.values())
    return {
        "region": region,
        "sources_queried": list(results.keys()),
        "items_by_source": results,
        "total_items": total,
        "errors": errors if errors else None,
        "source": "fashion_editorial_rss",
    }
