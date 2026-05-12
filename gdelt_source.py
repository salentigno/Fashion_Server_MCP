"""
GDELT Project integration.
---------------------------
Consulta el GDELT DOC 2.0 API para obtener:
  - Volumen de cobertura de una marca/keyword en medios globales
  - Tendencia temporal (subiendo o bajando la atención mediática)
  - Tono medio (positivo / negativo / neutro)
  - Top medios que están cubriendo el tema
  - Países donde más se habla del tema

Las llamadas se paralelizan con ThreadPoolExecutor cuando sea posible
para evitar timeouts del cliente.

Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
Sin auth, sin rate limits estrictos. 100.000+ medios en 65 idiomas.
"""

import sys
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed


GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_UA = "fashion-trends-mcp/1.0"


def _log(msg: str) -> None:
    print(f"[gdelt] {msg}", file=sys.stderr, flush=True)


# Mapeo región ISO → código GDELT de país (formato FIPS, no ISO)
# Ejemplo: IT=IT, ES=SP, FR=FR, US=US, UK=UK, DE=GM
REGION_TO_GDELT_COUNTRY = {
    "ES": "SP", "IT": "IT", "FR": "FR", "DE": "GM", "UK": "UK",
    "GB": "UK", "US": "US", "MX": "MX", "AR": "AR", "BR": "BR",
    "PT": "PO", "NL": "NL", "BE": "BE", "CH": "SZ", "AT": "AU",
    "JP": "JA", "CN": "CH", "IN": "IN", "AU": "AS",
}

# Mapeo región → idioma GDELT (formato 3 letras GDELT)
REGION_TO_GDELT_LANG = {
    "ES": "spa", "MX": "spa", "AR": "spa", "CO": "spa", "PE": "spa",
    "IT": "ita",
    "FR": "fre", "BE": "fre",
    "DE": "ger", "AT": "ger", "CH": "ger",
    "US": "eng", "UK": "eng", "GB": "eng", "AU": "eng", "IE": "eng",
    "PT": "por", "BR": "por",
    "NL": "dut",
    "JP": "jpn",
}


def _fetch_gdelt(params: dict, timeout: int = 15) -> dict:
    """Llama al GDELT DOC API y devuelve JSON parseado."""
    url = GDELT_BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": GDELT_UA})
    _log(f"Query: {params.get('query', '')[:80]}... ({params.get('mode')})")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    # GDELT a veces devuelve texto vacío cuando no hay resultados
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        _log(f"Respuesta no-JSON: {text[:200]}")
        return {}


def _build_query(term: str, region: str | None = None, lang_only: bool = False) -> str:
    """
    Construye una query GDELT con filtros opcionales de región/idioma.

    GDELT usa una sintaxis especial:
      - "gucci"          → busca la palabra
      - sourcecountry:IT → solo medios italianos
      - sourcelang:ita   → solo medios en italiano

    lang_only=True: solo filtro por idioma (más resultados, menos preciso geo)
    lang_only=False: idioma + país (más preciso, menos resultados)
    """
    parts = [f'"{term}"']
    if region:
        region_up = region.upper()
        lang = REGION_TO_GDELT_LANG.get(region_up)
        country = REGION_TO_GDELT_COUNTRY.get(region_up)
        if lang:
            parts.append(f"sourcelang:{lang}")
        if country and not lang_only:
            parts.append(f"sourcecountry:{country}")
    return " ".join(parts)


def get_brand_media_volume(
    brand: str,
    region: str | None = None,
    timespan: str = "7d",
) -> dict:
    """
    Devuelve el volumen de cobertura mediática de una marca en los últimos N días.

    brand: término a buscar (ej: "Gucci", "Swarovski")
    region: código ISO (ES, IT, US, ...). Si se omite, global.
    timespan: '24h', '3d', '7d', '1w', '1m', '3m'
    """
    query = _build_query(brand, region)
    params = {
        "query": query,
        "mode": "TimelineVolInfo",
        "format": "json",
        "timespan": timespan,
    }

    try:
        data = _fetch_gdelt(params)
        timeline = data.get("timeline", [])
        if not timeline or not timeline[0].get("data"):
            # Retry sin filtro de país (solo idioma)
            _log(f"Sin datos con filtro país, reintentando solo con idioma...")
            params["query"] = _build_query(brand, region, lang_only=True)
            data = _fetch_gdelt(params)
            timeline = data.get("timeline", [])

        if not timeline:
            return {"brand": brand, "region": region, "found": False, "volume_timeline": []}

        series = timeline[0].get("data", [])
        # Cada punto: {"date": "20260417T000000Z", "value": 0.0123}
        # El valor es % del total de artículos mundiales → multiplicamos por
        # un factor para tener números más legibles
        points = []
        for p in series:
            date_raw = p.get("date", "")
            date_clean = date_raw[:8] if len(date_raw) >= 8 else date_raw
            points.append({
                "date": date_clean,
                "volume_pct": round(p.get("value", 0), 6),
            })

        # Calcular tendencia: últimos 3 vs primeros 3 puntos
        trend = None
        if len(points) >= 6:
            recent = sum(p["volume_pct"] for p in points[-3:]) / 3
            older = sum(p["volume_pct"] for p in points[:3]) / 3
            if older > 0:
                pct = ((recent - older) / older) * 100
                trend = f"{'+' if pct >= 0 else ''}{pct:.1f}%"

        total = sum(p["volume_pct"] for p in points)
        return {
            "brand": brand,
            "region": region,
            "timespan": timespan,
            "found": True,
            "data_points": len(points),
            "total_coverage_pct": round(total, 6),
            "trend_recent_vs_older": trend,
            "volume_timeline": points[-14:],  # últimos 14 puntos como muestra
            "source": "gdelt",
        }
    except Exception as e:
        return {"brand": brand, "region": region, "error": str(e)[:150]}


def get_brand_media_tone(
    brand: str,
    region: str | None = None,
    timespan: str = "7d",
) -> dict:
    """
    Tono medio de la cobertura mediática: positivo, negativo o neutral.
    Rango: -10 (muy negativo) a +10 (muy positivo). 0 es neutro.
    """
    query = _build_query(brand, region)
    params = {
        "query": query,
        "mode": "TimelineTone",
        "format": "json",
        "timespan": timespan,
    }

    try:
        data = _fetch_gdelt(params)
        timeline = data.get("timeline", [])
        if not timeline or not timeline[0].get("data"):
            params["query"] = _build_query(brand, region, lang_only=True)
            data = _fetch_gdelt(params)
            timeline = data.get("timeline", [])

        if not timeline:
            return {"brand": brand, "found": False}

        series = timeline[0].get("data", [])
        tones = [p.get("value", 0) for p in series if p.get("value") is not None]

        if not tones:
            return {"brand": brand, "found": False}

        avg_tone = sum(tones) / len(tones)
        sentiment = (
            "very_positive" if avg_tone > 3 else
            "positive" if avg_tone > 1 else
            "neutral" if avg_tone > -1 else
            "negative" if avg_tone > -3 else
            "very_negative"
        )

        return {
            "brand": brand,
            "region": region,
            "timespan": timespan,
            "found": True,
            "average_tone": round(avg_tone, 2),
            "sentiment": sentiment,
            "tone_range": f"{min(tones):.2f} to {max(tones):.2f}",
            "data_points": len(tones),
            "source": "gdelt",
        }
    except Exception as e:
        return {"brand": brand, "error": str(e)[:150]}


def get_brand_recent_articles(
    brand: str,
    region: str | None = None,
    timespan: str = "3d",
    max_articles: int = 10,
) -> dict:
    """
    Lista de artículos recientes que mencionan la marca.
    Devuelve título, medio, país, URL y fecha.
    """
    query = _build_query(brand, region)
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "timespan": timespan,
        "maxrecords": str(min(max_articles, 50)),
        "sort": "DateDesc",
    }

    try:
        data = _fetch_gdelt(params)
        articles = data.get("articles", [])
        if not articles:
            params["query"] = _build_query(brand, region, lang_only=True)
            data = _fetch_gdelt(params)
            articles = data.get("articles", [])

        if not articles:
            return {"brand": brand, "found": False, "articles": []}

        simplified = [
            {
                "title": a.get("title"),
                "source": a.get("domain"),
                "country": a.get("sourcecountry"),
                "language": a.get("language"),
                "url": a.get("url"),
                "published": a.get("seendate"),
                "tone": round(a.get("tone", 0), 2) if a.get("tone") is not None else None,
            }
            for a in articles[:max_articles]
        ]

        # Estadísticas rápidas: top medios y países
        domains = Counter(a.get("domain", "") for a in articles if a.get("domain"))
        countries = Counter(a.get("sourcecountry", "") for a in articles if a.get("sourcecountry"))

        return {
            "brand": brand,
            "region": region,
            "timespan": timespan,
            "found": True,
            "total_articles_fetched": len(articles),
            "top_sources": [{"domain": d, "count": c} for d, c in domains.most_common(5)],
            "top_countries": [{"country": c, "count": n} for c, n in countries.most_common(5)],
            "articles": simplified,
            "source": "gdelt",
        }
    except Exception as e:
        return {"brand": brand, "error": str(e)[:150]}


def brand_gdelt_full_signal(
    brand: str,
    region: str | None = None,
    timespan: str = "7d",
) -> dict:
    """
    Señal completa de una marca: volumen + tono + artículos recientes.
    Las 3 llamadas a GDELT se hacen EN PARALELO para reducir latencia
    (de ~45s secuencial a ~15s paralelo).
    """
    tasks = {
        "media_volume": lambda: get_brand_media_volume(brand, region, timespan),
        "media_tone": lambda: get_brand_media_tone(brand, region, timespan),
        "recent_articles": lambda: get_brand_recent_articles(
            brand, region, timespan="3d", max_articles=8
        ),
    }

    results: dict = {
        "brand": brand,
        "region": region,
        "timespan": timespan,
        "source": "gdelt_combined",
    }

    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_key = {
            executor.submit(func): key for key, func in tasks.items()
        }
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results[key] = future.result(timeout=20)
            except Exception as e:
                _log(f"Error en {key}: {e}")
                results[key] = {"error": str(e)[:100]}

    return results


def compare_brands_gdelt(
    brands: list[str],
    region: str | None = None,
    timespan: str = "7d",
) -> dict:
    """
    Compara varias marcas por volumen de cobertura mediática.
    Útil para ver quién lidera la conversación en prensa.

    Paraleliza 2 llamadas (volume + tone) × N marcas en paralelo.
    Con 4 marcas: antes ~60s secuencial → ahora ~15s paralelo.
    """
    if len(brands) > 8:
        brands = brands[:8]

    def _fetch_brand(brand: str) -> dict:
        """Obtiene volume + tone de UNA marca en paralelo interno."""
        with ThreadPoolExecutor(max_workers=2) as executor:
            vol_fut = executor.submit(get_brand_media_volume, brand, region, timespan)
            tone_fut = executor.submit(get_brand_media_tone, brand, region, timespan)
            try:
                vol = vol_fut.result(timeout=20)
            except Exception as e:
                vol = {"error": str(e)[:100]}
            try:
                tone = tone_fut.result(timeout=20)
            except Exception as e:
                tone = {"error": str(e)[:100]}

        if vol.get("found"):
            return {
                "brand": brand,
                "total_coverage_pct": vol.get("total_coverage_pct", 0),
                "trend": vol.get("trend_recent_vs_older"),
                "sentiment": tone.get("sentiment") if tone.get("found") else None,
                "average_tone": tone.get("average_tone") if tone.get("found") else None,
            }
        return {"brand": brand, "found": False}

    # Paralelizar también entre marcas — todas las marcas a la vez
    ranking: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(brands), 6)) as executor:
        future_to_brand = {
            executor.submit(_fetch_brand, brand): brand for brand in brands
        }
        for future in as_completed(future_to_brand):
            brand = future_to_brand[future]
            try:
                ranking.append(future.result(timeout=30))
            except Exception as e:
                _log(f"Error con marca {brand}: {e}")
                ranking.append({"brand": brand, "error": str(e)[:100]})

    # Separar marcas con datos vs sin datos
    ranking_found = [r for r in ranking if "total_coverage_pct" in r]
    ranking_found.sort(key=lambda x: x["total_coverage_pct"], reverse=True)
    ranking_not_found = [r for r in ranking if "found" in r and not r["found"]]

    return {
        "region": region,
        "timespan": timespan,
        "ranking": ranking_found,
        "not_found": ranking_not_found,
        "source": "gdelt_comparison",
    }
