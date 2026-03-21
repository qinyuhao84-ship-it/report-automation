from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional, Sequence
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
    market_path: List[str] = field(default_factory=list)
    extracted_year: Optional[int] = None
    extracted_market_size: Optional[float] = None
    extracted_ratio: Optional[float] = None
    confidence: float = 0.0


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


def _extract_market_size_wan_cny(text: str) -> Optional[float]:
    # 统一换算为“万元人民币”。
    pattern = re.compile(
        r"(\d[\d,]*(?:\.\d+)?)\s*(万亿元|万亿|亿元|亿人民币|亿|万元|万人民币|元人民币|人民币|元|billion\s*yuan|million\s*yuan)",
        flags=re.IGNORECASE,
    )
    candidates: List[float] = []
    for number, unit in pattern.findall(text):
        value = _parse_float(number)
        if value is None:
            continue
        normalized = unit.lower().replace(" ", "")
        if normalized in {"万亿元", "万亿"}:
            candidates.append(value * 1e8)
        elif normalized in {"亿元", "亿人民币", "亿"}:
            candidates.append(value * 1e4)
        elif normalized in {"万元", "万人民币"}:
            candidates.append(value)
        elif normalized in {"元人民币", "人民币", "元"}:
            candidates.append(value / 1e4)
        elif normalized == "billionyuan":
            candidates.append(value * 1e5)
        elif normalized == "millionyuan":
            candidates.append(value * 100)

    if not candidates:
        return None
    # 使用最大值，避免抓到局部字段（如同比增速中的小数字）
    return max(candidates)


def _estimate_confidence(text: str, market_size: Optional[float], ratio: Optional[float]) -> float:
    confidence = 0.35
    if market_size is not None:
        confidence += 0.25
    if ratio is not None:
        confidence += 0.2
    if "市场规模" in text:
        confidence += 0.1
    if "来源" in text or "http" in text:
        confidence += 0.05
    return max(0.0, min(1.0, confidence))


def _extract_metrics_from_text(text: str) -> tuple[Optional[int], Optional[float], Optional[float], float]:
    year = _extract_year(text)
    market_size = _extract_market_size_wan_cny(text)
    ratio = _extract_ratio(text)
    confidence = _estimate_confidence(text, market_size, ratio)
    return year, market_size, ratio, confidence


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


class BaseMarketProvider(ABC):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

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
            return direct.strip()
        shared = os.getenv("INFER_BROWSER_USER_DATA_DIR")
        if shared and shared.strip():
            return os.path.join(shared.strip(), provider_key.lower())
        return None

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

    def _collect_links(self, page, max_results: int) -> List[tuple[str, str]]:
        links: List[tuple[str, str]] = []
        for anchor in page.locator("a[href^='http']").all()[: max_results * 5]:
            try:
                href = (anchor.get_attribute("href") or "").strip()
                if not href or _looks_like_noise_link(href):
                    continue
                title = _compact_text(anchor.inner_text(), limit=120)
                if not title:
                    title = href
                links.append((title, href))
                if len(links) >= max_results:
                    break
            except Exception:
                continue
        return links

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
        year, market_size, ratio, confidence = _extract_metrics_from_text(body_text)
        now = datetime.utcnow()
        hits: List[ProviderHit] = [
            ProviderHit(
                provider=provider_name,
                query=query,
                title=title,
                url=page.url,
                snippet=body_text,
                captured_at=now,
                market_path=list(market_path),
                extracted_year=year,
                extracted_market_size=market_size,
                extracted_ratio=ratio,
                confidence=confidence,
            )
        ]

        for link_title, link_url in self._collect_links(page, max_results=max_results):
            hits.append(
                ProviderHit(
                    provider=provider_name,
                    query=query,
                    title=link_title,
                    url=link_url,
                    snippet=f"来源链接：{link_title}",
                    captured_at=now,
                    market_path=list(market_path),
                    extracted_year=year,
                    extracted_market_size=market_size,
                    extracted_ratio=ratio,
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

                self._type_and_submit(page, input_locator, query)

                try:
                    page.wait_for_url("**/search*", timeout=int(self.config.timeout_seconds * 1000))
                except Exception:
                    # URL 不变也允许继续（站点可能在同路由渲染）
                    pass
                page.wait_for_timeout(8000)

                body = _compact_text(page.locator("body").inner_text(), limit=400)
                if not body:
                    raise ProviderError("秘塔返回空白结果")
                return self._build_hits_from_page(
                    page=page,
                    query=query,
                    provider_name=self.name,
                    market_path=target_path,
                    max_results=max_results,
                    title="秘塔AI搜索结果",
                )
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

                self._type_and_submit(page, input_locator, query)
                page.wait_for_timeout(9000)

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

                self._type_and_submit(page, input_locator, query)
                page.wait_for_timeout(10000)

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
