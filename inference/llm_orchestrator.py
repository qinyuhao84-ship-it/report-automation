from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Sequence

import httpx

from .models import InferenceConfig, InferenceInput
from .providers import ProviderHit


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return " ".join(text.split())


def _normalize_optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1]
        try:
            return float(text) / 100.0
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _normalize_optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _extract_json_payload(text: str) -> Dict[str, Any]:
    cleaned = _strip_code_fences(text)
    if not cleaned:
        raise ValueError("LLM 返回为空")

    for candidate in (cleaned, cleaned.replace("\n", " ")):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char not in "{[":
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("无法解析 LLM JSON 输出")


def _extract_text_from_response(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    parts: List[str] = []
                    for item in content:
                        if isinstance(item, dict):
                            text = item.get("text")
                            if isinstance(text, str):
                                parts.append(text)
                    if parts:
                        return "".join(parts).strip()
            text = choice.get("text")
            if isinstance(text, str):
                return text.strip()

    if isinstance(payload.get("content"), str):
        return str(payload["content"]).strip()

    return ""


@dataclass(frozen=True)
class LLMPlan:
    query: str
    market_path: List[str]
    next_paths: List[List[str]]
    should_stop: bool
    confidence: float
    reason: str


@dataclass(frozen=True)
class LLMExtraction:
    year: Optional[int]
    market_size: Optional[float]
    ratio: Optional[float]
    confidence: float

    @property
    def extracted_year(self) -> Optional[int]:
        return self.year

    @property
    def extracted_market_size(self) -> Optional[float]:
        return self.market_size

    @property
    def extracted_ratio(self) -> Optional[float]:
        return self.ratio


class OpenAICompatibleClient:
    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        timeout_seconds: int = 60,
        max_output_tokens: int = 1200,
        user_agent: str = "report-automation",
        transport: Optional[httpx.BaseTransport] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.user_agent = user_agent
        if transport is not None and not isinstance(transport, httpx.BaseTransport):
            transport = httpx.MockTransport(transport)  # type: ignore[arg-type]
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def complete(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_output_tokens: Optional[int] = None,
    ) -> str:
        payload = {
            "model": model or self.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_output_tokens or self.max_output_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        response = self._client.post(f"{self.api_base}/chat/completions", json=payload, headers=headers)
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("LLM 返回不是合法 JSON") from exc
        return _extract_text_from_response(body)


def _default_api_base(config: InferenceConfig) -> Optional[str]:
    return config.llm_api_base or os.getenv("OPENAI_API_BASE") or os.getenv("LLM_API_BASE")


def _resolve_api_key(api_key_env: str) -> Optional[str]:
    for key_name in (api_key_env, "OPENAI_API_KEY", "LLM_API_KEY"):
        value = os.getenv(key_name)
        if value and value.strip():
            return value.strip()
    return None


def _resolve_model(value: Optional[str], fallback: str) -> str:
    text = _normalize_text(value)
    return text or fallback


class LLMOrchestrator:
    def __init__(
        self,
        client: Optional[OpenAICompatibleClient] = None,
        *,
        enabled: bool = True,
        planning_model: Optional[str] = None,
        extraction_model: Optional[str] = None,
        planning_temperature: float = 0.2,
        extraction_temperature: float = 0.0,
        max_output_tokens: int = 1200,
        retry_attempts: int = 2,
    ) -> None:
        self.client = client
        self.enabled = enabled
        self.planning_model = planning_model
        self.extraction_model = extraction_model
        self.planning_temperature = planning_temperature
        self.extraction_temperature = extraction_temperature
        self.max_output_tokens = max_output_tokens
        self.retry_attempts = retry_attempts

    @classmethod
    def from_config(
        cls,
        config: InferenceConfig,
        *,
        client: Optional[OpenAICompatibleClient] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> "LLMOrchestrator":
        api_base = _default_api_base(config)
        api_key = _resolve_api_key(config.llm_api_key_env)
        enabled = bool(config.llm_enabled and api_base and api_key and config.llm_model)

        resolved_client = client
        if resolved_client is None and enabled:
            resolved_client = OpenAICompatibleClient(
                api_base=api_base or "",
                api_key=api_key or "",
                model=_resolve_model(config.llm_model, "gpt-5.1-codex"),
                timeout_seconds=config.llm_timeout_seconds,
                max_output_tokens=config.llm_max_output_tokens,
                user_agent=config.llm_user_agent,
                transport=transport,
            )

        return cls(
            client=resolved_client,
            enabled=enabled,
            planning_model=config.llm_planning_model or config.llm_model,
            extraction_model=config.llm_extraction_model or config.llm_model,
            planning_temperature=config.llm_planning_temperature,
            extraction_temperature=config.llm_extraction_temperature,
            max_output_tokens=config.llm_max_output_tokens,
            retry_attempts=config.llm_retry_attempts,
        )

    def is_available(self) -> bool:
        return bool(self.enabled and self.client is not None)

    def plan_round(
        self,
        *,
        input_model: InferenceInput,
        current_path: Sequence[str],
        latest_year: int,
        round_index: int,
        evidence_summary: Sequence[Dict[str, Any]],
        fallback_query: str,
    ) -> Optional[LLMPlan]:
        if not self.is_available():
            return None

        messages = [
            {
                "role": "system",
                "content": (
                    "你是市场细分和搜索规划器。"
                    "只输出 JSON，不要输出解释。"
                    "目标是帮助找到最细分且有公开数据来源的细分市场，并生成可搜索的查询词。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "PLAN",
                        "company_name": input_model.company_name,
                        "product_name": input_model.product_name,
                        "product_intro": input_model.product_intro,
                        "product_category": input_model.product_category,
                        "company_intro": input_model.company_intro,
                        "competitors": input_model.competitors,
                        "latest_sales_year": latest_year,
                        "round_index": round_index,
                        "current_path": list(current_path),
                        "evidence_summary": list(evidence_summary),
                        "fallback_query": fallback_query,
                        "output_schema": {
                            "query": "string",
                            "market_path": ["string"],
                            "next_paths": [["string"]],
                            "should_stop": "boolean",
                            "confidence": "number",
                            "reason": "string",
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        payload = self._complete_json(
            messages,
            model=_resolve_model(self.planning_model, self.client.model if self.client else "gpt-5.1-codex"),
            temperature=self.planning_temperature,
        )
        if not payload:
            return None

        query = _normalize_text(payload.get("query")) or _normalize_text(fallback_query)
        market_path = self._normalize_path(payload.get("market_path")) or list(current_path)
        next_paths = self._normalize_paths(payload.get("next_paths"))
        should_stop = bool(payload.get("should_stop", False))
        confidence = self._clamp01(payload.get("confidence"))
        reason = _normalize_text(payload.get("reason")) or "LLM 规划完成"

        return LLMPlan(
            query=query,
            market_path=market_path,
            next_paths=next_paths,
            should_stop=should_stop,
            confidence=confidence,
            reason=reason,
        )

    def enrich_hit(
        self,
        *,
        input_model: InferenceInput,
        hit: ProviderHit,
        current_path: Sequence[str],
        round_index: int,
    ) -> Optional[LLMExtraction]:
        if not self.is_available():
            return None

        messages = [
            {
                "role": "system",
                "content": (
                    "EXTRACT: 你是证据抽取器。"
                    "只输出 JSON，不要输出解释。"
                    "任务是从证据文本中抽取年份、市场规模、占比和置信度。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "EXTRACT",
                        "company_name": input_model.company_name,
                        "product_name": input_model.product_name,
                        "current_path": list(current_path),
                        "round_index": round_index,
                        "title": hit.title,
                        "url": hit.url,
                        "snippet": hit.snippet,
                        "existing_extraction": {
                            "year": hit.extracted_year,
                            "market_size": hit.extracted_market_size,
                            "ratio": hit.extracted_ratio,
                            "confidence": hit.confidence,
                        },
                        "output_schema": {
                            "year": "integer|null",
                            "market_size": "number|null",
                            "ratio": "number|null",
                            "confidence": "number",
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        payload = self._complete_json(
            messages,
            model=_resolve_model(self.extraction_model, self.client.model if self.client else "gpt-5.1-codex"),
            temperature=self.extraction_temperature,
        )
        if not payload:
            return None

        return LLMExtraction(
            year=_normalize_optional_int(payload.get("year")),
            market_size=_normalize_optional_float(payload.get("market_size")),
            ratio=_normalize_optional_float(payload.get("ratio")),
            confidence=self._clamp01(payload.get("confidence")),
        )

    def _complete_json(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        model: str,
        temperature: float,
    ) -> Dict[str, Any]:
        if not self.is_available():
            return {}

        last_error: Optional[Exception] = None
        for attempt in range(self.retry_attempts + 1):
            try:
                content = self.client.complete(  # type: ignore[union-attr]
                    messages,
                    model=model,
                    temperature=temperature,
                    max_output_tokens=self.max_output_tokens,
                )
                if not content:
                    return {}
                return _extract_json_payload(content)
            except Exception as exc:  # pragma: no cover - 防御兜底
                last_error = exc
                if attempt < self.retry_attempts:
                    time.sleep(min(0.5 * (attempt + 1), 1.5))
                    continue
                return {}
        if last_error is not None:
            return {}
        return {}

    @staticmethod
    def _normalize_path(value: Any) -> List[str]:
        if not isinstance(value, (list, tuple)):
            return []
        path: List[str] = []
        for item in value:
            text = _normalize_text(item)
            if text:
                path.append(text)
        return path

    def _normalize_paths(self, value: Any) -> List[List[str]]:
        if not isinstance(value, (list, tuple)):
            return []
        paths: List[List[str]] = []
        for item in value:
            path = self._normalize_path(item)
            if path:
                paths.append(path)
        return paths

    @staticmethod
    def _clamp01(value: Any) -> float:
        normalized = _normalize_optional_float(value)
        if normalized is None:
            return 0.0
        return max(0.0, min(1.0, normalized))


def apply_llm_extraction(hit: ProviderHit, extraction: Optional[LLMExtraction]) -> ProviderHit:
    if extraction is None:
        return hit
    return replace(
        hit,
        extracted_year=extraction.year if extraction.year is not None else hit.extracted_year,
        extracted_market_size=(
            extraction.market_size if extraction.market_size is not None else hit.extracted_market_size
        ),
        extracted_ratio=extraction.ratio if extraction.ratio is not None else hit.extracted_ratio,
        confidence=max(hit.confidence, extraction.confidence),
    )
