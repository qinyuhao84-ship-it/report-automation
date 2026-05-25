from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse

def _compact_text(value: str, limit: int = 1200) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit]


def _parse_float(text: str) -> Optional[float]:
    cleaned = text.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_year(text: str) -> Optional[int]:
    years = re.findall(r"\b(20[0-3]\d)\b", text)
    if not years:
        return None
    return int(years[-1])


def _extract_ratio(text: str) -> Optional[float]:
    keyword_match = re.search(r"(?:市占率|市场占有率|占有率|占比)[^%\d]{0,12}(\d{1,3}(?:\.\d+)?)\s*%", text)
    if keyword_match:
        ratio_value = _parse_float(keyword_match.group(1))
        if ratio_value is not None:
            ratio = ratio_value / 100.0
            if 0.0 < ratio <= 1.0:
                return ratio

    for match in re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%", text):
        ratio_value = _parse_float(match)
        if ratio_value is None:
            continue
        ratio = ratio_value / 100.0
        if 0.0 < ratio <= 1.0:
            return ratio
    return None


def _extract_growth_rate(text: str) -> Optional[float]:
    for pattern in (
        r"(?:复合年增长率|年复合增长率|cagr)[^%\d]{0,12}(\d{1,2}(?:\.\d+)?)\s*%",
        r"(?:增长率|同比增长)[^%\d]{0,12}(\d{1,2}(?:\.\d+)?)\s*%",
    ):
        matched = re.search(pattern, text, flags=re.IGNORECASE)
        if not matched:
            continue
        value = _parse_float(matched.group(1))
        if value is None:
            continue
        ratio = value / 100.0
        if 0.0 < ratio < 1.0:
            return ratio
    return None


def _split_sentences(text: str) -> List[str]:
    if not text:
        return []
    raw = re.split(r"(?<=[。！？!?；;])\s+|\n+", text)
    cleaned = [_compact_text(item, limit=260) for item in raw if _compact_text(item, limit=260)]
    return cleaned


def _pick_quote_sentence(text: str) -> Optional[str]:
    sentences = _split_sentences(text)
    if not sentences:
        return None

    patterns = (
        r"(市场规模|市场容量|market size)",
        r"(亿美元|亿美金|亿元|万元|万亿元|million|billion|usd|美元|人民币|cny)",
    )
    for sentence in sentences:
        if re.search(patterns[0], sentence, flags=re.IGNORECASE) and re.search(patterns[1], sentence, flags=re.IGNORECASE):
            return sentence

    for sentence in sentences:
        if re.search(r"\d", sentence) and re.search(patterns[1], sentence, flags=re.IGNORECASE):
            return sentence
    return sentences[0]


@dataclass(frozen=True)
class MarketSizeParseResult:
    market_size_wan_cny: Optional[float]
    original_value: Optional[float]
    original_unit: Optional[str]
    original_currency: Optional[str]
    usd_cny_rate_used: Optional[float]
    conversion_formula: Optional[str]


def _normalize_currency(unit_text: str, currency_text: str) -> str:
    merged = f"{unit_text} {currency_text}".lower()
    if any(token in merged for token in ("usd", "美元", "美金", "$", "dollar")):
        return "USD"
    return "CNY"


def _unit_multiplier(unit_text: str) -> float:
    normalized = (unit_text or "").lower().replace(" ", "")
    if normalized in {"万亿元", "万亿"}:
        return 1e12
    if normalized in {"亿元", "亿"}:
        return 1e8
    if normalized in {"万元", "万"}:
        return 1e4
    if normalized in {"元", "人民币"}:
        return 1.0
    if normalized in {"billion", "bn", "b"}:
        return 1e9
    if normalized in {"million", "mn", "m"}:
        return 1e6
    return 1.0


def _unit_to_yuan_formula(unit_text: str) -> str:
    normalized = (unit_text or "").lower().replace(" ", "")
    if normalized in {"万亿元", "万亿"}:
        return "× 1000000000000"
    if normalized in {"亿元", "亿"}:
        return "× 100000000"
    if normalized in {"万元", "万"}:
        return "× 10000"
    if normalized in {"元", "人民币"}:
        return "× 1"
    if normalized in {"billion", "bn", "b"}:
        return "× 1000000000"
    if normalized in {"million", "mn", "m"}:
        return "× 1000000"
    return "× 1"


def _extract_market_size_with_unit(text: str, usd_cny_rate: float) -> MarketSizeParseResult:
    if not text:
        return MarketSizeParseResult(None, None, None, None, None, None)

    patterns = [
        re.compile(
            r"(\d[\d,]*(?:\.\d+)?)\s*(万亿元|万亿|亿元|亿|万元|万|元)\s*(人民币|美元|美金|usd|cny|rmb)?",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"(\d[\d,]*(?:\.\d+)?)\s*(billion|million|bn|mn|m|b)\s*(usd|dollar|dollars|yuan|cny|rmb)",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"(usd|dollar|dollars|yuan|cny|rmb)\s*(\d[\d,]*(?:\.\d+)?)\s*(billion|million|bn|mn|m|b)?",
            flags=re.IGNORECASE,
        ),
    ]

    candidates: List[Tuple[float, MarketSizeParseResult]] = []
    for pattern in patterns:
        for matched in pattern.findall(text):
            if len(matched) == 3:
                number_text, unit_text, currency_text = matched
            else:
                continue

            if pattern is patterns[2]:
                currency_text, number_text, unit_text = matched

            value = _parse_float(number_text)
            if value is None or value <= 0:
                continue

            currency = _normalize_currency(str(unit_text), str(currency_text))
            unit = str(unit_text or "").strip() or "元"
            unit_factor = _unit_multiplier(unit)

            if currency == "USD":
                amount_usd = value * unit_factor
                amount_cny = amount_usd * usd_cny_rate
                conversion = (
                    f"{value}{unit}{currency_text or ''} {_unit_to_yuan_formula(unit)} USD"
                    f" × {usd_cny_rate:.6f}(USD/CNY) ÷ 10000"
                    f" = {amount_cny / 10000:.2f}万元"
                )
            else:
                amount_cny = value * unit_factor
                conversion = (
                    f"{value}{unit}{currency_text or ''} {_unit_to_yuan_formula(unit)} CNY"
                    f" ÷ 10000 = {amount_cny / 10000:.2f}万元"
                )

            market_size_wan_cny = amount_cny / 10000.0
            result = MarketSizeParseResult(
                market_size_wan_cny=market_size_wan_cny,
                original_value=value,
                original_unit=unit,
                original_currency=currency,
                usd_cny_rate_used=usd_cny_rate if currency == "USD" else None,
                conversion_formula=conversion,
            )
            candidates.append((market_size_wan_cny, result))

    if not candidates:
        return MarketSizeParseResult(None, None, None, None, None, None)

    # 优先使用更大的规模值，避免误选增长率等小数字。
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _estimate_confidence(text: str, market_size: Optional[float], ratio: Optional[float], growth_rate: Optional[float]) -> float:
    confidence = 0.35
    if market_size is not None:
        confidence += 0.25
    if ratio is not None:
        confidence += 0.2
    if growth_rate is not None:
        confidence += 0.05
    if "市场规模" in text:
        confidence += 0.1
    if "来源" in text or "http" in text:
        confidence += 0.05
    return max(0.0, min(1.0, confidence))


def _extract_metrics_from_text(
    text: str,
    *,
    usd_cny_rate: float,
    preferred_quote: Optional[str] = None,
) -> tuple[
    Optional[int],
    Optional[float],
    Optional[float],
    Optional[float],
    float,
    Optional[str],
    Optional[float],
    Optional[str],
    Optional[str],
    Optional[float],
    Optional[str],
]:
    quote = preferred_quote or _pick_quote_sentence(text)
    source_text = quote or text

    year = _extract_year(source_text) or _extract_year(text)
    ratio = _extract_ratio(source_text) or _extract_ratio(text)
    growth_rate = _extract_growth_rate(text)
    size_result = _extract_market_size_with_unit(source_text, usd_cny_rate=usd_cny_rate)
    if size_result.market_size_wan_cny is None:
        size_result = _extract_market_size_with_unit(text, usd_cny_rate=usd_cny_rate)

    confidence = _estimate_confidence(text, size_result.market_size_wan_cny, ratio, growth_rate)
    return (
        year,
        size_result.market_size_wan_cny,
        ratio,
        growth_rate,
        confidence,
        quote,
        size_result.original_value,
        size_result.original_unit,
        size_result.original_currency,
        size_result.usd_cny_rate_used,
        size_result.conversion_formula,
    )


def _looks_like_noise_link(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if not host:
        return True
    blocked_hosts = {
        "static-1.metaso.cn",
        "lf-flow-web-cdn.doubao.com",
        "static.yuanbao.tencent.com",
        "open.weixin.qq.com",
        "support.weixin.qq.com",
        "open.douyin.com",
    }
    return host in blocked_hosts


def _is_doubao_login_redirect(url: str) -> bool:
    lowered = (url or "").lower()
    return (
        "from_logout=1" in lowered
        or "open.douyin.com/platform/oauth/connect" in lowered
        or "www.doubao.com/auth/" in lowered
    )


def _build_relevance_keywords(query: str, market_path: Sequence[str]) -> List[str]:
    stopwords = {
        "市场",
        "市场规模",
        "市占率",
        "市场占有率",
        "占有率",
        "占比",
        "全球",
        "中国",
        "行业",
        "细分",
        "应用",
        "技术",
        "功能",
        "company",
        "product",
        "market",
        "size",
        "global",
        "cn",
    }
    source = " ".join([query] + list(market_path))
    raw_candidates = re.findall(r"[\u4e00-\u9fff]{2,16}|[A-Za-z][A-Za-z0-9\\-]{2,30}", source)
    keywords: List[str] = []
    for item in raw_candidates:
        token = item.strip()
        if not token:
            continue
        lower = token.lower()
        if lower in stopwords:
            continue
        if token in keywords:
            continue
        keywords.append(token)
    # 额外保留路径节点作为强相关短语（例如“自锁紧型电源连接系统”）
    for node in market_path:
        text = _compact_text(str(node), limit=40)
        if len(text) < 2:
            continue
        if text in keywords:
            continue
        keywords.append(text)
    return keywords[:12]


def _is_relevant_to_market(title: str, body_text: str, keywords: Sequence[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{title} {body_text}".lower()
    hit_count = 0
    for keyword in keywords:
        key = keyword.lower().strip()
        if len(key) < 2:
            continue
        if key in haystack:
            hit_count += 1
            if hit_count >= 2:
                return True
    return False
