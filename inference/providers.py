from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from .models import ProviderConfig, ProviderMode, ProviderName


class ProviderError(RuntimeError):
    pass


class BrowserAutomationUnavailableError(ProviderError):
    pass


class ProviderNotConfiguredError(ProviderError):
    pass


class ProviderLoginRequiredError(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderHit:
    provider: str
    query: str
    title: str
    url: str
    snippet: str
    captured_at: datetime
    search_page_url: Optional[str] = None
    market_path: List[str] = field(default_factory=list)
    extracted_year: Optional[int] = None
    extracted_market_size: Optional[float] = None
    extracted_ratio: Optional[float] = None
    extracted_growth_rate: Optional[float] = None
    confidence: float = 0.0
    source_verified: bool = False
    quote_text: Optional[str] = None
    market_size_original_value: Optional[float] = None
    market_size_original_unit: Optional[str] = None
    market_size_original_currency: Optional[str] = None
    usd_cny_rate_used: Optional[float] = None
    conversion_formula: Optional[str] = None


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


class BaseMarketProvider(ABC):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.usd_cny_rate: float = 7.2
        self.usd_cny_rate_source: str = "unset"
        self.usd_cny_rate_realtime: bool = False

    @property
    def name(self) -> str:
        return self.config.name.value

    def is_enabled(self) -> bool:
        return bool(self.config.enabled)

    @abstractmethod
    def search(self, query: str, max_results: int = 5, market_path: Optional[Sequence[str]] = None) -> List[ProviderHit]:
        raise NotImplementedError

    def healthcheck(self) -> bool:
        return self.is_enabled()

    def _stub_placeholder(self, _query: str) -> List[ProviderHit]:
        return []

    def set_fx_rate(self, rate: float, source: str, is_realtime: bool) -> None:
        self.usd_cny_rate = float(rate)
        self.usd_cny_rate_source = str(source or "unknown")
        self.usd_cny_rate_realtime = bool(is_realtime)

    def _import_playwright(self):
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise BrowserAutomationUnavailableError(
                "未安装或未正确初始化 Playwright，请先执行依赖安装。"
            ) from exc
        return sync_playwright, PlaywrightError, PlaywrightTimeoutError

    def _resolve_headless(self) -> bool:
        provider_key = self.name.upper()
        raw = os.getenv(f"{provider_key}_BROWSER_HEADLESS")
        if raw is None:
            raw = os.getenv("INFER_BROWSER_HEADLESS", "1")
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}

    def _resolve_profile_dir(self) -> Optional[str]:
        provider_key = self.name.upper()
        direct = os.getenv(f"{provider_key}_BROWSER_USER_DATA_DIR")
        if direct and direct.strip():
            path = os.path.abspath(direct.strip())
            os.makedirs(path, exist_ok=True)
            return path
        shared = os.getenv("INFER_BROWSER_USER_DATA_DIR")
        if shared and shared.strip():
            path = os.path.abspath(os.path.join(shared.strip(), provider_key.lower()))
            os.makedirs(path, exist_ok=True)
            return path
        # 默认启用固定 profile，登录一次后自动复用，避免每轮掉登录态。
        default_path = os.path.abspath(
            os.path.join("data", "inference", "browser_profiles", provider_key.lower())
        )
        os.makedirs(default_path, exist_ok=True)
        return default_path

    def _open_browser_page(self):
        sync_playwright, _, _ = self._import_playwright()
        timeout_ms = int(self.config.timeout_seconds * 1000)
        headless = self._resolve_headless()
        profile_dir = self._resolve_profile_dir()

        class _BrowserSession:
            def __init__(self, provider: BaseMarketProvider):
                self.provider = provider
                self.playwright = None
                self.context = None
                self.browser = None
                self.page = None

            def __enter__(self):
                self.playwright = sync_playwright().start()
                chromium = self.playwright.chromium
                if profile_dir:
                    self.context = chromium.launch_persistent_context(profile_dir, headless=headless)
                    self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                else:
                    self.browser = chromium.launch(headless=headless)
                    self.context = self.browser.new_context()
                    self.page = self.context.new_page()
                self.page.set_default_timeout(timeout_ms)
                return self.page

            def __exit__(self, exc_type, exc_val, exc_tb):
                try:
                    if self.context is not None:
                        self.context.close()
                finally:
                    if self.browser is not None:
                        self.browser.close()
                    if self.playwright is not None:
                        self.playwright.stop()

        return _BrowserSession(self)

    def _first_visible_locator(self, page, selectors: Sequence[str]):
        for selector in selectors:
            if not selector:
                continue
            locator = page.locator(selector).first
            try:
                if locator.count() > 0 and locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    def _type_and_submit(self, page, locator, query: str) -> None:
        locator.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(query, delay=22)

        if self.config.submit_selector:
            submit = self._first_visible_locator(page, [self.config.submit_selector])
            if submit is not None:
                submit.click()
                return
        page.keyboard.press("Enter")

    def _click_first_visible(self, page, selectors: Sequence[str]) -> bool:
        locator = self._first_visible_locator(page, selectors)
        if locator is None:
            return False
        try:
            locator.click(timeout=1200)
            page.wait_for_timeout(250)
            return True
        except Exception:
            return False

    def _try_enable_controls_by_text(self, page, labels: Sequence[str]) -> None:
        for label in labels:
            text = (label or "").strip()
            if not text:
                continue
            selectors = [
                f"button:has-text('{text}')",
                f"[role='button']:has-text('{text}')",
                f"label:has-text('{text}')",
                f"span:has-text('{text}')",
                f"div:has-text('{text}')",
            ]
            self._click_first_visible(page, selectors)

    def _deduplicate_hits(self, hits: Sequence[ProviderHit], max_results: int) -> List[ProviderHit]:
        deduped: List[ProviderHit] = []
        seen = set()
        for item in hits:
            key = (item.url.strip().lower(), item.title.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= max_results:
                break
        return deduped

    def _optimized_provider_query(self, query: str) -> str:
        base = _compact_text(query, limit=1400)
        guidance = (
            "【任务目标】\n"
            "围绕给定企业与主导产品，在全网公开信息中检索“细分市场规模”证据，用于后续计算市场占有率。\n"
            "【硬性约束】\n"
            "1) 必须优先检索公开可核验来源：行业报告、协会统计、上市公司年报、研究机构、政府/权威媒体。\n"
            "2) 必须给出最近年份市场规模，并注明单位、币种、年份；若有 CAGR/增长率一并给出。\n"
            "3) 若无直接市场规模，必须明确写“未找到直接规模”，并给“占比 + 上级市场规模”可推导口径。\n"
            "4) 严禁编造数据、严禁臆测链接；无法确认时必须明确“不确定”。\n"
            "5) 必须尽量覆盖多来源，避免只引用单一站点。\n"
            "【返回格式】\n"
            "请按条目列出：来源标题 | 来源链接 | 原文数据句（带数字） | 年份 | 市场规模 | 单位/币种 | 增长率/占比（如有） | 备注。\n"
            "若找不到可核验原文，必须单独标注：未找到可核验原文依据。"
        )
        if self.name == ProviderName.MITATA.value:
            return f"{base}\n{guidance}"
        if self.name == ProviderName.YUANBAO.value:
            return f"{base}\n{guidance}"
        if self.name == ProviderName.DOUBAO.value:
            return f"{base}\n{guidance}"
        return base

    def _collect_links(self, page, max_results: int) -> List[Tuple[str, str]]:
        links: List[Tuple[str, str]] = []
        seen = set()
        for anchor in page.locator("a[href^='http']").all()[: max_results * 8]:
            try:
                href = (anchor.get_attribute("href") or "").strip()
                if not href or _looks_like_noise_link(href):
                    continue
                host = (urlparse(href).netloc or "").lower()
                if host.endswith("metaso.cn") or host.endswith("doubao.com") or host.endswith("yuanbao.tencent.com"):
                    continue
                if href in seen:
                    continue
                title = _compact_text(anchor.inner_text(), limit=120)
                if not title:
                    title = href
                links.append((title, href))
                seen.add(href)
                if len(links) >= max_results:
                    break
            except Exception:
                continue
        return links

    def _verify_original_link(
        self,
        *,
        context,
        query: str,
        provider_name: str,
        market_path: Sequence[str],
        link_title: str,
        link_url: str,
        relevance_keywords: Sequence[str],
        search_page_url: str,
    ) -> Optional[ProviderHit]:
        page = None
        try:
            page = context.new_page()
            link_timeout = min(int(self.config.timeout_seconds * 1000), 7000)
            page.set_default_timeout(link_timeout)
            page.goto(link_url, wait_until="domcontentloaded", timeout=link_timeout)
            page.wait_for_timeout(700)

            title = _compact_text(page.title(), limit=160) or _compact_text(link_title, limit=160) or link_url
            body_text = _compact_text(page.locator("body").inner_text(), limit=5000)
            if not body_text:
                return None
            if not _is_relevant_to_market(title, body_text[:3000], relevance_keywords):
                return None

            (
                year,
                market_size,
                ratio,
                growth_rate,
                confidence,
                quote,
                original_value,
                original_unit,
                original_currency,
                usd_cny_rate_used,
                conversion_formula,
            ) = _extract_metrics_from_text(
                body_text,
                usd_cny_rate=self.usd_cny_rate,
                preferred_quote=None,
            )

            if market_size is None and ratio is None:
                return None

            quote_text = quote or _pick_quote_sentence(body_text) or _compact_text(body_text, limit=180)
            return ProviderHit(
                provider=provider_name,
                query=query,
                title=title,
                url=link_url,
                snippet=quote_text,
                captured_at=datetime.utcnow(),
                search_page_url=search_page_url,
                market_path=list(market_path),
                extracted_year=year,
                extracted_market_size=market_size,
                extracted_ratio=ratio,
                extracted_growth_rate=growth_rate,
                confidence=max(0.55, confidence),
                source_verified=True,
                quote_text=quote_text,
                market_size_original_value=original_value,
                market_size_original_unit=original_unit,
                market_size_original_currency=original_currency,
                usd_cny_rate_used=usd_cny_rate_used,
                conversion_formula=conversion_formula,
            )
        except Exception:
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def _build_hits_from_page(
        self,
        *,
        page,
        query: str,
        provider_name: str,
        market_path: Sequence[str],
        max_results: int,
        title: str,
    ) -> List[ProviderHit]:
        body_text = _compact_text(page.locator("body").inner_text(), limit=1800)
        (
            year,
            market_size,
            ratio,
            growth_rate,
            confidence,
            quote,
            original_value,
            original_unit,
            original_currency,
            usd_cny_rate_used,
            conversion_formula,
        ) = _extract_metrics_from_text(
            body_text,
            usd_cny_rate=self.usd_cny_rate,
            preferred_quote=None,
        )
        now = datetime.utcnow()
        ai_hit = ProviderHit(
            provider=provider_name,
            query=query,
            title=title,
            url=page.url,
            snippet=quote or _compact_text(body_text, limit=220),
            captured_at=now,
            search_page_url=page.url,
            market_path=list(market_path),
            extracted_year=year,
            extracted_market_size=market_size,
            extracted_ratio=ratio,
            extracted_growth_rate=growth_rate,
            confidence=confidence,
            source_verified=False,
            quote_text=quote,
            market_size_original_value=original_value,
            market_size_original_unit=original_unit,
            market_size_original_currency=original_currency,
            usd_cny_rate_used=usd_cny_rate_used,
            conversion_formula=conversion_formula,
        )

        verified_hits: List[ProviderHit] = []
        relevance_keywords = _build_relevance_keywords(query, market_path)
        source_links = self._collect_links(page, max_results=max_results)
        for link_title, link_url in source_links:
            verified = self._verify_original_link(
                context=page.context,
                query=query,
                provider_name=provider_name,
                market_path=market_path,
                link_title=link_title,
                link_url=link_url,
                relevance_keywords=relevance_keywords,
                search_page_url=page.url,
            )
            if verified is None:
                continue
            verified_hits.append(verified)
            if len(verified_hits) >= max_results:
                break

        if verified_hits:
            return verified_hits[:max_results]

        hits: List[ProviderHit] = [ai_hit]

        for link_title, link_url in source_links:
            hits.append(
                ProviderHit(
                    provider=provider_name,
                    query=query,
                    title=link_title,
                    url=link_url,
                    snippet=f"来源链接：{link_title}",
                    captured_at=now,
                    search_page_url=page.url,
                    market_path=list(market_path),
                    extracted_year=year,
                    extracted_market_size=market_size,
                    extracted_ratio=ratio,
                    extracted_growth_rate=growth_rate,
                    confidence=max(0.35, confidence - 0.1),
                )
            )

        # 保证至少 1 条证据
        if not hits:
            return []
        return hits[:max_results]


class MitataProvider(BaseMarketProvider):
    def search(self, query: str, max_results: int = 5, market_path: Optional[Sequence[str]] = None) -> List[ProviderHit]:
        if self.config.mode == ProviderMode.BROWSER:
            return self._search_with_browser(query, max_results=max_results, market_path=market_path)
        if self.config.mode == ProviderMode.HTTP:
            # 秘塔接口依赖动态 token，HTTP 模式统一走浏览器以保证可用性。
            return self._search_with_browser(query, max_results=max_results, market_path=market_path)
        return self._stub_placeholder(query)

    def _search_with_browser(self, query: str, max_results: int, market_path: Optional[Sequence[str]]) -> List[ProviderHit]:
        target_path = list(market_path or [])
        base_url = self.config.base_url or "https://metaso.cn"
        selectors = [
            self.config.search_input_selector or "",
            "textarea.search-consult-textarea",
            "textarea[placeholder*='请输入']",
            "textarea",
            "div[contenteditable='true']",
        ]

        try:
            with self._open_browser_page() as page:
                page.goto(base_url, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)

                input_locator = self._first_visible_locator(page, selectors)
                if input_locator is None:
                    raise BrowserAutomationUnavailableError("秘塔页面输入框未找到")
                all_hits: List[ProviderHit] = []
                scope_modes = ["全网", "文库", "学术"]
                for mode in scope_modes:
                    self._try_enable_controls_by_text(page, ["深度研究", mode])
                    scoped_query = f"{self._optimized_provider_query(query)}\n检索模式：{mode}"
                    self._type_and_submit(page, input_locator, scoped_query)

                    try:
                        page.wait_for_url("**/search*", timeout=int(self.config.timeout_seconds * 1000))
                    except Exception:
                        pass
                    page.wait_for_timeout(5000)

                    body = _compact_text(page.locator("body").inner_text(), limit=400)
                    if not body:
                        continue
                    scoped_hits = self._build_hits_from_page(
                        page=page,
                        query=f"{query} [{mode}]",
                        provider_name=self.name,
                        market_path=target_path,
                        max_results=max_results,
                        title=f"秘塔AI搜索结果（{mode}）",
                    )
                    all_hits.extend(scoped_hits)
                    if len(self._deduplicate_hits(all_hits, max_results=max_results)) >= max_results:
                        break

                deduped = self._deduplicate_hits(all_hits, max_results=max_results)
                if not deduped:
                    raise ProviderError("秘塔返回空白结果")
                return deduped
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"秘塔浏览器检索失败: {exc}") from exc


class DoubaoProvider(BaseMarketProvider):
    def search(self, query: str, max_results: int = 5, market_path: Optional[Sequence[str]] = None) -> List[ProviderHit]:
        if self.config.mode == ProviderMode.BROWSER:
            return self._search_with_browser(query, max_results=max_results, market_path=market_path)
        if self.config.mode == ProviderMode.HTTP:
            raise ProviderNotConfiguredError("豆包 HTTP 模式未启用，请使用 browser 模式并提供登录态")
        return self._stub_placeholder(query)

    def _search_with_browser(self, query: str, max_results: int, market_path: Optional[Sequence[str]]) -> List[ProviderHit]:
        target_path = list(market_path or [])
        base_url = self.config.base_url or "https://www.doubao.com/chat/"
        selectors = [
            self.config.search_input_selector or "",
            "textarea.semi-input-textarea",
            "textarea[placeholder*='发消息']",
            "textarea",
        ]

        try:
            with self._open_browser_page() as page:
                page.goto(base_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                input_locator = self._first_visible_locator(page, selectors)
                if input_locator is None:
                    raise BrowserAutomationUnavailableError("豆包页面输入框未找到")

                self._type_and_submit(page, input_locator, self._optimized_provider_query(query))
                page.wait_for_timeout(5500)

                body = _compact_text(page.locator("body").inner_text(), limit=800)
                if _is_doubao_login_redirect(page.url):
                    raise ProviderLoginRequiredError(
                        "豆包当前会话未登录，已自动降级。可设置 DOUBAO_BROWSER_USER_DATA_DIR 指向已登录浏览器配置。"
                    )

                login_button_visible = False
                try:
                    login_button_visible = page.locator("button:has-text('登录'), a:has-text('登录')").count() > 0
                except Exception:
                    login_button_visible = False

                if login_button_visible and ("你好，我是豆包" in body or "发消息或输入" in body):
                    raise ProviderLoginRequiredError(
                        "豆包当前会话未登录，已自动降级。可设置 DOUBAO_BROWSER_USER_DATA_DIR 指向已登录浏览器配置。"
                    )

                if not body:
                    raise ProviderError("豆包返回空白结果")

                return self._build_hits_from_page(
                    page=page,
                    query=query,
                    provider_name=self.name,
                    market_path=target_path,
                    max_results=max_results,
                    title="豆包AI检索结果",
                )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"豆包浏览器检索失败: {exc}") from exc


class YuanbaoProvider(BaseMarketProvider):
    def search(self, query: str, max_results: int = 5, market_path: Optional[Sequence[str]] = None) -> List[ProviderHit]:
        if self.config.mode == ProviderMode.BROWSER:
            return self._search_with_browser(query, max_results=max_results, market_path=market_path)
        if self.config.mode == ProviderMode.HTTP:
            raise ProviderNotConfiguredError("元宝 HTTP 模式未启用，请使用 browser 模式并提供登录态")
        return self._stub_placeholder(query)

    def _search_with_browser(self, query: str, max_results: int, market_path: Optional[Sequence[str]]) -> List[ProviderHit]:
        target_path = list(market_path or [])
        base_url = self.config.base_url or "https://yuanbao.tencent.com"
        selectors = [
            self.config.search_input_selector or "",
            "div[contenteditable='true']",
            "[role='textbox']",
            "textarea",
        ]

        try:
            with self._open_browser_page() as page:
                page.goto(base_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3500)

                self._try_enable_controls_by_text(page, ["深度思考", "联网搜索"])

                input_locator = self._first_visible_locator(page, selectors)
                if input_locator is None:
                    raise BrowserAutomationUnavailableError("元宝页面输入框未找到")

                placeholder = ""
                try:
                    placeholder = (
                        input_locator.get_attribute("data-placeholder")
                        or input_locator.get_attribute("placeholder")
                        or ""
                    )
                except Exception:
                    placeholder = ""
                if "log in" in placeholder.lower() or "登录" in placeholder:
                    raise ProviderLoginRequiredError(
                        "元宝当前会话未登录，已自动降级。可设置 YUANBAO_BROWSER_USER_DATA_DIR 指向已登录浏览器配置。"
                    )

                self._type_and_submit(page, input_locator, self._optimized_provider_query(query))
                page.wait_for_timeout(6000)

                body = _compact_text(page.locator("body").inner_text(), limit=800)
                if "Please log in" in body or "Not logged in" in body or "登录" in body and "Download Center" in body:
                    raise ProviderLoginRequiredError(
                        "元宝当前会话未登录，已自动降级。可设置 YUANBAO_BROWSER_USER_DATA_DIR 指向已登录浏览器配置。"
                    )

                if not body:
                    raise ProviderError("元宝返回空白结果")

                return self._build_hits_from_page(
                    page=page,
                    query=query,
                    provider_name=self.name,
                    market_path=target_path,
                    max_results=max_results,
                    title="元宝AI检索结果",
                )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"元宝浏览器检索失败: {exc}") from exc


class ProviderFactory:
    _mapping = {
        ProviderName.MITATA: MitataProvider,
        ProviderName.DOUBAO: DoubaoProvider,
        ProviderName.YUANBAO: YuanbaoProvider,
    }

    @classmethod
    def build(cls, config: ProviderConfig) -> BaseMarketProvider:
        provider_cls = cls._mapping.get(config.name)
        if provider_cls is None:
            raise ProviderNotConfiguredError(f"未知平台: {config.name}")
        return provider_cls(config)


def order_providers(configs: Iterable[ProviderConfig], priority: Sequence[ProviderName]) -> List[BaseMarketProvider]:
    config_map = {cfg.name: cfg for cfg in configs if cfg.enabled}
    ordered: List[BaseMarketProvider] = []
    seen = set()
    for provider_name in priority:
        cfg = config_map.get(provider_name)
        if cfg is None:
            continue
        seen.add(provider_name)
        ordered.append(ProviderFactory.build(cfg))
    for name, cfg in config_map.items():
        if name in seen:
            continue
        ordered.append(ProviderFactory.build(cfg))
    return ordered
