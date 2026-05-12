"""
Search Alternatives — sustitutos de Google Trends.
---------------------------------------------------
Tres fuentes que dan señal de búsqueda real cuando Google Trends está bloqueado:

1. **Bing Webmaster Keyword Research API** — volumen mensual real (auth: API key)
2. **DuckDuckGo Autocomplete** — términos asociados a una keyword (sin auth)
3. **Reddit Search** — menciones reales en conversaciones (auth: ya tenemos PRAW)

Cada función devuelve dicts homogéneos con datos comparables.
Las 3 son robustas a fallos: si una falla, no rompe las demás.
"""

import os
import sys
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter


def _log(msg: str) -> None:
    print(f"[search-alt] {msg}", file=sys.stderr, flush=True)


# ===========================================================================
# 1. BING WEBMASTER — Keyword Research API
# ===========================================================================

BING_API_BASE = "https://ssl.bing.com/webmaster/api.svc/json"
BING_API_KEY = os.getenv("BING_WEBMASTER_API_KEY", "")

# Mapeo región ISO → código de país y idioma para Bing
BING_REGION_MAP = {
    "ES": ("es-ES", "ES"),
    "MX": ("es-MX", "MX"),
    "AR": ("es-AR", "AR"),
    "IT": ("it-IT", "IT"),
    "FR": ("fr-FR", "FR"),
    "DE": ("de-DE", "DE"),
    "GB": ("en-GB", "GB"),
    "UK": ("en-GB", "GB"),
    "US": ("en-US", "US"),
    "BR": ("pt-BR", "BR"),
    "PT": ("pt-PT", "PT"),
    "NL": ("nl-NL", "NL"),
    "JP": ("ja-JP", "JP"),
}


def _bing_request(endpoint: str, params: dict) -> dict | None:
    """Hace una petición a Bing Webmaster API con la API key."""
    if not BING_API_KEY:
        return None

    params["apikey"] = BING_API_KEY
    url = f"{BING_API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(
        url,
        headers={
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
        _log(f"Bing HTTP {e.code} en {endpoint}: {err_body[:200]}")
        return None
    except Exception as e:
        _log(f"Bing error en {endpoint}: {e}")
        return None


def get_bing_keyword_volume(
    keyword: str,
    region: str = "US",
    months: int = 6,
) -> dict:
    """
    Devuelve volumen mensual de búsqueda en Bing para una keyword.
    Reemplaza (parcialmente) a Google Trends.

    keyword: término a consultar
    region: código ISO (ES, IT, US, GB, DE, FR...)
    months: cuántos meses de histórico (1-12)
    """
    if not BING_API_KEY:
        return {
            "keyword": keyword,
            "error": "BING_WEBMASTER_API_KEY no configurada en .env",
            "source": "bing_webmaster",
        }

    lang, country = BING_REGION_MAP.get(region.upper(), ("en-US", "US"))
    end = datetime.now()
    start = end - timedelta(days=30 * months)

    # Bing API espera 'language' en formato BCP-47 (it-IT, en-US...) y
    # 'country' como código ISO de 2 letras (IT, US...). El campo
    # `language` lleva la info de país, así que con eso suele bastar.
    # Probamos primero con language y SIN country (más fiable).
    params = {
        "q": keyword,
        "language": lang,
        "startDate": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "endDate": end.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    data = _bing_request("GetKeyword", params)
    if data is None:
        # Reintento con formato alternativo: lang sin BCP-47, solo idioma
        _log(f"Reintentando con formato alternativo de language...")
        params["language"] = lang.split("-")[0]  # 'it-IT' → 'it'
        data = _bing_request("GetKeyword", params)

    if data is None:
        return {"keyword": keyword, "error": "request failed", "source": "bing_webmaster"}

    # Bing devuelve { "d": { "Impressions": N, ... } } u objeto similar
    d = data.get("d") if isinstance(data.get("d"), dict) else data
    if not d:
        return {"keyword": keyword, "found": False, "source": "bing_webmaster"}

    impressions = d.get("Impressions") or d.get("BroadMatchImpressions")
    return {
        "keyword": keyword,
        "region": region,
        "language": lang,
        "found": impressions is not None,
        "monthly_impressions": impressions,
        "raw_data": d,
        "source": "bing_webmaster",
    }


def get_bing_related_keywords(
    keyword: str,
    region: str = "US",
    limit: int = 20,
) -> dict:
    """
    Keywords relacionadas con un término según Bing.
    Equivalente a "rising/top related queries" de Google Trends.
    """
    if not BING_API_KEY:
        return {
            "keyword": keyword,
            "error": "BING_WEBMASTER_API_KEY no configurada",
            "source": "bing_webmaster_related",
        }

    lang, country = BING_REGION_MAP.get(region.upper(), ("en-US", "US"))
    params = {
        "q": keyword,
        "language": lang,
    }
    data = _bing_request("GetRelatedKeywords", params)
    if data is None:
        # Reintento con language simple
        params["language"] = lang.split("-")[0]
        data = _bing_request("GetRelatedKeywords", params)

    if data is None:
        return {"keyword": keyword, "error": "request failed", "source": "bing_webmaster_related"}

    raw = data.get("d") or []
    if isinstance(raw, dict):
        raw = raw.get("results", [])

    related = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        related.append({
            "keyword": item.get("Query") or item.get("query"),
            "impressions": item.get("Impressions") or item.get("impressions"),
        })

    return {
        "keyword": keyword,
        "region": region,
        "language": lang,
        "related_count": len(related),
        "related": related,
        "source": "bing_webmaster_related",
    }


def compare_bing_keywords(
    keywords: list[str],
    region: str = "US",
) -> dict:
    """
    Compara varias keywords por volumen de búsqueda en Bing.
    Reemplazo de compare_fashion_keywords (Google Trends).
    """
    if not BING_API_KEY:
        return {
            "error": "BING_WEBMASTER_API_KEY no configurada",
            "source": "bing_keyword_compare",
        }

    if len(keywords) > 8:
        keywords = keywords[:8]

    ranking = []
    with ThreadPoolExecutor(max_workers=min(len(keywords), 4)) as executor:
        futures = {
            executor.submit(get_bing_keyword_volume, kw, region): kw
            for kw in keywords
        }
        for future in as_completed(futures):
            kw = futures[future]
            try:
                data = future.result(timeout=20)
                if data.get("found"):
                    ranking.append({
                        "keyword": kw,
                        "monthly_impressions": data.get("monthly_impressions"),
                    })
                else:
                    ranking.append({"keyword": kw, "found": False})
            except Exception as e:
                ranking.append({"keyword": kw, "error": str(e)[:80]})

    found = [r for r in ranking if "monthly_impressions" in r and r["monthly_impressions"]]
    found.sort(key=lambda x: x["monthly_impressions"] or 0, reverse=True)

    return {
        "region": region,
        "keywords_analyzed": keywords,
        "ranking": found,
        "not_found": [r for r in ranking if r.get("found") is False],
        "errors": [r for r in ranking if "error" in r],
        "source": "bing_keyword_compare",
    }


# ===========================================================================
# 2. DUCKDUCKGO — Autocomplete API (cero auth)
# ===========================================================================

DDG_AUTOCOMPLETE = "https://duckduckgo.com/ac/"

# Mapeo región ISO → código de mercado DuckDuckGo (kl=country-language)
DDG_REGION_MAP = {
    "ES": "es-es", "MX": "mx-es", "AR": "ar-es",
    "IT": "it-it", "FR": "fr-fr", "DE": "de-de",
    "GB": "uk-en", "UK": "uk-en", "US": "us-en",
    "BR": "br-pt", "PT": "pt-pt", "NL": "nl-nl",
    "JP": "jp-ja",
}


def get_duckduckgo_suggestions(
    keyword: str,
    region: str = "US",
) -> dict:
    """
    Devuelve sugerencias de autocompletado de DuckDuckGo para una keyword.
    Esto es señal de qué términos asocia la gente con tu keyword.

    Sin auth, sin rate limits relevantes. Funciona casi siempre.
    """
    market = DDG_REGION_MAP.get(region.upper(), "us-en")
    params = {
        "q": keyword,
        "type": "list",
        "kl": market,
    }
    url = f"{DDG_AUTOCOMPLETE}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/121.0.0.0 Safari/537.36",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # DuckDuckGo devuelve [keyword, [sugerencia1, sugerencia2, ...]]
        if isinstance(data, list) and len(data) >= 2:
            suggestions = data[1] if isinstance(data[1], list) else []
        elif isinstance(data, list):
            suggestions = [s for s in data if isinstance(s, str)]
        else:
            suggestions = []

        # Filtrar la propia keyword si aparece
        suggestions = [
            s for s in suggestions
            if isinstance(s, str) and s.lower() != keyword.lower()
        ]

        return {
            "keyword": keyword,
            "region": region,
            "market": market,
            "suggestion_count": len(suggestions),
            "suggestions": suggestions[:15],  # tope 15
            "source": "duckduckgo_autocomplete",
        }
    except Exception as e:
        return {
            "keyword": keyword,
            "error": str(e)[:120],
            "source": "duckduckgo_autocomplete",
        }


def compare_duckduckgo_keywords(
    keywords: list[str],
    region: str = "US",
) -> dict:
    """
    Compara qué términos tienen MÁS sugerencias asociadas en DuckDuckGo.
    Más sugerencias = término más "vivo" en búsquedas reales.
    """
    if len(keywords) > 8:
        keywords = keywords[:8]

    ranking = []
    with ThreadPoolExecutor(max_workers=min(len(keywords), 4)) as executor:
        futures = {
            executor.submit(get_duckduckgo_suggestions, kw, region): kw
            for kw in keywords
        }
        for future in as_completed(futures):
            kw = futures[future]
            try:
                data = future.result(timeout=15)
                if "error" not in data:
                    ranking.append({
                        "keyword": kw,
                        "suggestion_count": data.get("suggestion_count", 0),
                        "top_suggestions": data.get("suggestions", [])[:5],
                    })
            except Exception as e:
                ranking.append({"keyword": kw, "error": str(e)[:80]})

    ranking.sort(key=lambda x: x.get("suggestion_count", 0), reverse=True)

    return {
        "region": region,
        "ranking": ranking,
        "source": "duckduckgo_compare",
    }


# ===========================================================================
# 3. REDDIT SEARCH AVANZADO — usa la instancia PRAW del servidor
# ===========================================================================

# Subreddits de moda + lifestyle para búsqueda de menciones reales
REDDIT_FASHION_SUBS_EXTENDED = [
    "femalefashionadvice", "malefashionadvice", "streetwear",
    "fashion", "OUTFITS", "TheDevilWearsZara",
    "FashionRepsBST", "Frugalmalefashion", "Frugalfemalefashion",
    "PetiteFashionAdvice", "PlusSize", "TheFashionInsider",
    "Watches", "handbags", "jewelry", "RepladiesDesigner",
    "FemaleFashionAdvice", "OUTFITS",
]


def reddit_search_volume(
    reddit_instance,
    query: str,
    time_filter: str = "month",
    subreddits: list[str] | None = None,
    limit: int = 100,
) -> dict:
    """
    Cuenta menciones de un término en subreddits de moda durante el período.
    Es un proxy real de "volumen de búsqueda" porque Reddit es donde la gente
    pregunta y discute productos antes de comprarlos.

    query: término a buscar
    time_filter: 'day', 'week', 'month', 'year', 'all'
    subreddits: lista de subs específicos. Si None, usa los de moda/lifestyle.
    limit: máximo posts a recuperar (Reddit API limita a 100/llamada)
    """
    if subreddits is None:
        subreddits = REDDIT_FASHION_SUBS_EXTENDED[:6]  # primeros 6 = los más activos

    # Reddit acepta multi-sub con sintaxis "sub1+sub2+sub3"
    multi_sub = "+".join(subreddits)

    try:
        results = reddit_instance.subreddit(multi_sub).search(
            query,
            time_filter=time_filter,
            sort="relevance",
            limit=limit,
        )
        posts = list(results)

        # Análisis: cuántos posts, cuántos comentarios totales, top subs
        total_posts = len(posts)
        total_comments = sum(p.num_comments for p in posts)
        total_upvotes = sum(p.score for p in posts)
        sub_counter = Counter(p.subreddit.display_name for p in posts)
        top_posts = sorted(posts, key=lambda p: p.score, reverse=True)[:5]

        return {
            "query": query,
            "time_filter": time_filter,
            "subreddits_searched": len(subreddits),
            "total_mentions": total_posts,
            "total_comments_aggregated": total_comments,
            "total_upvotes_aggregated": total_upvotes,
            "engagement_score": total_comments + total_upvotes,
            "top_subreddits": [
                {"subreddit": sub, "mentions": count}
                for sub, count in sub_counter.most_common(5)
            ],
            "top_posts": [
                {
                    "title": p.title,
                    "subreddit": p.subreddit.display_name,
                    "score": p.score,
                    "comments": p.num_comments,
                    "url": f"https://reddit.com{p.permalink}",
                    "created": datetime.fromtimestamp(p.created_utc).strftime("%Y-%m-%d"),
                }
                for p in top_posts
            ],
            "source": "reddit_search",
        }
    except Exception as e:
        return {
            "query": query,
            "error": str(e)[:120],
            "source": "reddit_search",
        }


def reddit_compare_terms(
    reddit_instance,
    queries: list[str],
    time_filter: str = "month",
) -> dict:
    """
    Compara volumen de menciones en Reddit para varios términos.
    Útil para ver qué término genera más conversación.
    """
    if len(queries) > 6:
        queries = queries[:6]

    ranking = []
    with ThreadPoolExecutor(max_workers=min(len(queries), 4)) as executor:
        futures = {
            executor.submit(
                reddit_search_volume, reddit_instance, q, time_filter
            ): q
            for q in queries
        }
        for future in as_completed(futures):
            q = futures[future]
            try:
                data = future.result(timeout=30)
                if "error" not in data:
                    ranking.append({
                        "query": q,
                        "mentions": data.get("total_mentions", 0),
                        "engagement": data.get("engagement_score", 0),
                        "top_subreddit": (
                            data["top_subreddits"][0]["subreddit"]
                            if data.get("top_subreddits") else None
                        ),
                    })
                else:
                    ranking.append({"query": q, "error": data["error"]})
            except Exception as e:
                ranking.append({"query": q, "error": str(e)[:80]})

    found = [r for r in ranking if "mentions" in r]
    found.sort(key=lambda x: x.get("engagement", 0), reverse=True)

    return {
        "time_filter": time_filter,
        "ranking": found,
        "errors": [r for r in ranking if "error" in r],
        "source": "reddit_compare",
    }


# ===========================================================================
# WRAPPER: combina las 3 fuentes para sustituir Google Trends
# ===========================================================================


def search_alternatives_for_keyword(
    keyword: str,
    region: str = "US",
    reddit_instance=None,
) -> dict:
    """
    Lanza Bing + DuckDuckGo + Reddit EN PARALELO para una keyword.
    Es el sustituto más completo de Google Trends que podemos hacer
    sin depender de Google.

    Devuelve un dict con las 3 señales agregadas.
    """
    _log(f"🔍 Iniciando search alternatives para '{keyword}' en {region}")

    tasks = {
        "bing": lambda: get_bing_keyword_volume(keyword, region),
        "duckduckgo": lambda: get_duckduckgo_suggestions(keyword, region),
    }
    if reddit_instance is not None:
        tasks["reddit"] = lambda: reddit_search_volume(
            reddit_instance, keyword, time_filter="month"
        )
    else:
        _log("   ⚠️  reddit_instance=None, saltando Reddit")

    results: dict = {
        "keyword": keyword,
        "region": region,
        "source": "search_alternatives_combined",
    }

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result(timeout=20)
            except Exception as e:
                _log(f"   ❌ {name} falló: {str(e)[:100]}")
                results[name] = {"error": str(e)[:120]}

    # Resumen unificado
    summary: dict = {}
    bing = results.get("bing", {})
    if bing.get("found"):
        summary["bing_volume"] = bing.get("monthly_impressions")
    ddg = results.get("duckduckgo", {})
    if "error" not in ddg:
        summary["ddg_suggestion_count"] = ddg.get("suggestion_count", 0)
        summary["ddg_top_suggestions"] = ddg.get("suggestions", [])[:5]
    reddit_res = results.get("reddit", {})
    if "error" not in reddit_res and reddit_res:
        summary["reddit_mentions"] = reddit_res.get("total_mentions", 0)
        summary["reddit_engagement"] = reddit_res.get("engagement_score", 0)

    results["summary"] = summary

    # Log de resultado claro
    bv = summary.get("bing_volume")
    ddg_n = summary.get("ddg_suggestion_count", 0)
    rm = summary.get("reddit_mentions", 0)
    re_eng = summary.get("reddit_engagement", 0)
    _log(
        f"✅ search alternatives '{keyword}': "
        f"Bing={bv if bv is not None else 'N/A'}, "
        f"DDG={ddg_n} sug, "
        f"Reddit={rm} menciones / {re_eng} eng"
    )

    return results
