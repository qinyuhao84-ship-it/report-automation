from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass(frozen=True)
class FxRateResult:
    usd_cny: float
    source: str
    fetched_at: float
    is_realtime: bool


_FX_LOCK = threading.Lock()
_FX_CACHE: Optional[FxRateResult] = None


def _safe_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_from_open_er_api(timeout_seconds: float = 6.0) -> Optional[FxRateResult]:
    url = "https://open.er-api.com/v6/latest/USD"
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(url)
        response.raise_for_status()
        body = response.json()
    rate = _safe_float((body.get("rates") or {}).get("CNY"))
    if rate is None or rate <= 0:
        return None
    return FxRateResult(
        usd_cny=rate,
        source="open.er-api.com",
        fetched_at=time.time(),
        is_realtime=True,
    )


def _fetch_from_frankfurter(timeout_seconds: float = 6.0) -> Optional[FxRateResult]:
    url = "https://api.frankfurter.app/latest?from=USD&to=CNY"
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(url)
        response.raise_for_status()
        body = response.json()
    rate = _safe_float((body.get("rates") or {}).get("CNY"))
    if rate is None or rate <= 0:
        return None
    return FxRateResult(
        usd_cny=rate,
        source="api.frankfurter.app",
        fetched_at=time.time(),
        is_realtime=True,
    )


def get_usd_cny_rate(default_rate: float, cache_ttl_seconds: int = 1800) -> FxRateResult:
    global _FX_CACHE

    now = time.time()
    with _FX_LOCK:
        if _FX_CACHE and (now - _FX_CACHE.fetched_at) <= cache_ttl_seconds:
            return _FX_CACHE

    fetchers = (_fetch_from_open_er_api, _fetch_from_frankfurter)
    for fetcher in fetchers:
        try:
            result = fetcher()
        except Exception:
            result = None
        if result is None:
            continue
        with _FX_LOCK:
            _FX_CACHE = result
        return result

    fallback = FxRateResult(
        usd_cny=float(default_rate),
        source="config-fallback",
        fetched_at=now,
        is_realtime=False,
    )
    with _FX_LOCK:
        _FX_CACHE = fallback
    return fallback

