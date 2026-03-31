from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, replace
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


def _normalize_text_list(value: Any, limit: int = 6) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items: List[str] = []
    for item in value:
        text = _normalize_text(item)
        if not text:
            continue
        if text in items:
            continue
        items.append(text)
        if len(items) >= limit:
            break
    return items


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
    provider_queries: Dict[str, str]
    market_path: List[str]
    next_paths: List[List[str]]
    should_stop: bool
    confidence: float
    reason: str


@dataclass(frozen=True)
class LLMPathProposal:
    market_paths: List[List[str]]
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


@dataclass(frozen=True)
class LLMFitCheck:
    is_aligned: bool
    confidence: float
    reason: str
    matched_points: List[str] = field(default_factory=list)
    conflict_points: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class LLMEvidenceReview:
    is_target_market: bool
    data_quality_passed: bool
    confidence: float
    reason: str
    issues: List[str] = field(default_factory=list)


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
    direct_value = (api_key_env or "").strip()
    # 兼容“配置文件直接写 key”的模式（如 sk-xxxx），避免只能依赖环境变量名。
    if direct_value.startswith("sk-"):
        return direct_value
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
                    "你是“细分市场检索策略规划器（Production）”。"
                    "只输出 JSON，不要输出解释。"
                    "项目总目标：基于企业与产品资料，在公开网络中找到主导产品所处细分市场规模，"
                    "并支持市场占有率=销售额/市场规模的可解释计算。"
                    "你必须优先规划“可检索、可取数、可核验原文”的路径，而不是追求理论完美。"
                    "禁止编造事实；无法确认时必须降低置信度并在 reason 写明不确定性。"
                    "规划原则：先保证可查到规模数据，再追求更细分；若过细导致无数据，给回退路径。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "PLAN",
                        "company_name": input_model.company_name,
                        "product_name": input_model.product_name,
                        "product_code": input_model.product_code,
                        "product_intro": input_model.product_intro,
                        "product_category": input_model.product_category,
                        "company_intro": input_model.company_intro,
                        "competitors": input_model.competitors,
                        "latest_sales_year": latest_year,
                        "round_index": round_index,
                        "current_path": list(current_path),
                        "evidence_summary": list(evidence_summary),
                        "fallback_query": fallback_query,
                        "hard_rules": [
                            "优先面向“行业报告/协会统计/上市公司年报/研究机构”可检索的关键词组合",
                            "优先最近年份（latest_sales_year）并保留单位币种、增长率相关关键词",
                            "若当前路径证据弱，给出至少2条下一步可执行候选路径",
                            "不要输出空泛路径节点（如“其他”“综合”）",
                            "query 必须可直接投喂给秘塔/元宝进行全网检索",
                        ],
                        "output_schema": {
                            "query": "string",
                            "provider_queries": {
                                "mitata": "string",
                                "yuanbao": "string"
                            },
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
        raw_provider_queries = payload.get("provider_queries")
        provider_queries: Dict[str, str] = {}
        if isinstance(raw_provider_queries, dict):
            for key, value in raw_provider_queries.items():
                name = _normalize_text(key).lower()
                if name not in {"mitata", "yuanbao", "doubao"}:
                    continue
                text = _normalize_text(value)
                if text:
                    provider_queries[name] = text
        market_path = self._normalize_path(payload.get("market_path")) or list(current_path)
        next_paths = self._normalize_paths(payload.get("next_paths"))
        should_stop = bool(payload.get("should_stop", False))
        confidence = self._clamp01(payload.get("confidence"))
        reason = _normalize_text(payload.get("reason")) or "LLM 规划完成"

        return LLMPlan(
            query=query,
            provider_queries=provider_queries,
            market_path=market_path,
            next_paths=next_paths,
            should_stop=should_stop,
            confidence=confidence,
            reason=reason,
        )

    def propose_market_paths(
        self,
        *,
        input_model: InferenceInput,
        latest_year: int,
        max_paths: int = 12,
    ) -> Optional[LLMPathProposal]:
        if not self.is_available():
            return None

        bounded_max_paths = max(3, min(int(max_paths or 12), 24))
        messages = [
            {
                "role": "system",
                "content": (
                    "你是“主导产品细分市场总规划器（Production）”。"
                    "只输出 JSON，不要输出解释。"
                    "项目目标：先穷举主导产品所有合理细分链，再让搜索器逐链检索市场规模证据。"
                    "你必须尽可能覆盖多维度细分：产品形态/功能/技术路线/应用场景/客户行业/标准编码映射/区域。"
                    "路径不要求学术完美，但必须业务上说得通、可检索、可落地。"
                    "禁止编造市场规模数字；当前阶段仅做路径设计，不输出任何市场规模结论。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "PATH_PROPOSAL",
                        "company_name": input_model.company_name,
                        "product_name": input_model.product_name,
                        "product_code": input_model.product_code,
                        "product_intro": input_model.product_intro,
                        "product_category": input_model.product_category,
                        "company_intro": input_model.company_intro,
                        "market_scope": input_model.market_scope.value,
                        "latest_sales_year": latest_year,
                        "constraints": [
                            "优先给出最可能拿到公开市场规模数据的路径",
                            "每条路径至少 2 层，最多 6 层",
                            "路径节点使用中文短语，避免空泛词",
                            "若产品代码可用，必须将其映射为可检索的行业或产品分类线索",
                            "路径之间要有差异化，避免同义重复",
                            "至少覆盖“功能细分/技术细分/应用细分/客户群细分”四类中的三类",
                            f"最多返回 {bounded_max_paths} 条",
                        ],
                        "output_schema": {
                            "market_paths": [["string"]],
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

        paths = self._normalize_paths(payload.get("market_paths"))
        unique_paths: List[List[str]] = []
        seen = set()
        for path in paths:
            key = tuple(path)
            if key in seen:
                continue
            seen.add(key)
            unique_paths.append(path)
            if len(unique_paths) >= bounded_max_paths:
                break

        if not unique_paths:
            return None

        return LLMPathProposal(
            market_paths=unique_paths,
            confidence=self._clamp01(payload.get("confidence")),
            reason=_normalize_text(payload.get("reason")) or "LLM 已生成细分路径候选",
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
                    "EXTRACT: 你是“证据数字抽取器（严格模式）”。"
                    "只输出 JSON，不要输出解释。"
                    "只允许抽取文本中明确出现的数字与单位，不允许猜测或补全。"
                    "若缺少明确数字，请返回 null。"
                    "ratio 必须是 0-1 小数；百分号需自行换算。"
                    "优先抽取“市场规模/占比/增长率”相关语句中的最近年份数据。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "EXTRACT",
                        "company_name": input_model.company_name,
                        "product_name": input_model.product_name,
                        "product_code": input_model.product_code,
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
                        "hard_rules": [
                            "只信原文片段，不信常识猜测",
                            "遇到多个数字时，优先与“市场规模/占比/增长率”强关联的数字",
                            "若无法区分是总市场还是细分市场，降低 confidence",
                        ],
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

    def validate_market_fit(
        self,
        *,
        input_model: InferenceInput,
        current_path: Sequence[str],
        latest_year: int,
        market_size: Optional[float],
        market_share: Optional[float],
        evidence_summary: Sequence[Dict[str, Any]],
    ) -> Optional[LLMFitCheck]:
        if not self.is_available():
            return None

        messages = [
            {
                "role": "system",
                "content": (
                    "你是“主导产品-细分市场一致性审核器（生产闸门）”。"
                    "只输出 JSON，不要输出解释。"
                    "你必须判断主导产品是否合理归属到当前细分路径。"
                    "判断要结合产品名称、产品介绍、企业介绍、产品代码线索。"
                    "可以参考秘塔/元宝摘要做判断，但若缺少原文依据必须在 reason 明确标注。"
                    "禁止为了达标而放宽到明显不相关市场。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "FIT_CHECK",
                        "company_name": input_model.company_name,
                        "product_name": input_model.product_name,
                        "product_code": input_model.product_code,
                        "product_intro": input_model.product_intro,
                        "company_intro": input_model.company_intro,
                        "current_market_path": list(current_path),
                        "latest_year": latest_year,
                        "market_size_latest_year_wan_cny": market_size,
                        "market_share_latest_year": market_share,
                        "evidence_summary": list(evidence_summary),
                        "criteria": [
                            "主导产品与细分市场定义一致",
                            "证据中的应用场景和技术特征与产品描述不冲突，且尽量对上关键特征",
                            "若细分市场范围明显大于主导产品能力边界，应优先判定不一致",
                            "即使不是完美匹配，也要达到“合理可解释”标准",
                        ],
                        "output_schema": {
                            "is_aligned": "boolean",
                            "confidence": "number",
                            "reason": "string",
                            "matched_points": "string[]",
                            "conflict_points": "string[]",
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        payload = self._complete_json(
            messages,
            model=_resolve_model(self.planning_model, self.client.model if self.client else "gpt-5.1-codex"),
            temperature=min(0.2, self.planning_temperature),
        )
        if not payload:
            return None

        matched_points = _normalize_text_list(payload.get("matched_points"))
        conflict_points = _normalize_text_list(payload.get("conflict_points"))
        reason = _normalize_text(payload.get("reason")) or "LLM 未返回一致性原因"
        if matched_points:
            reason = f"{reason}；匹配点：{'；'.join(matched_points)}"
        if conflict_points:
            reason = f"{reason}；冲突点：{'；'.join(conflict_points)}"

        return LLMFitCheck(
            is_aligned=bool(payload.get("is_aligned", False)),
            confidence=self._clamp01(payload.get("confidence")),
            reason=reason,
            matched_points=matched_points,
            conflict_points=conflict_points,
        )

    def review_evidence_hit(
        self,
        *,
        input_model: InferenceInput,
        current_path: Sequence[str],
        latest_year: int,
        hit: ProviderHit,
    ) -> Optional[LLMEvidenceReview]:
        if not self.is_available():
            return None

        messages = [
            {
                "role": "system",
                "content": (
                    "你是“检索证据审核器（严格）”。"
                    "只输出 JSON，不要输出解释。"
                    "你必须做两步判断："
                    "1) 该证据是否属于目标细分市场路径；"
                    "2) 该证据中的数字是否可用于市场规模/占有率计算。"
                    "如果证据市场不匹配或数字口径可疑，必须拒绝（false）并写明原因。"
                    "禁止为了凑结果放宽标准。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "EVIDENCE_REVIEW",
                        "company_name": input_model.company_name,
                        "product_name": input_model.product_name,
                        "product_code": input_model.product_code,
                        "product_intro": input_model.product_intro,
                        "company_intro": input_model.company_intro,
                        "target_market_path": list(current_path),
                        "latest_year": latest_year,
                        "evidence": {
                            "provider": hit.provider,
                            "title": hit.title,
                            "url": hit.url,
                            "search_page_url": hit.search_page_url,
                            "snippet": hit.snippet,
                            "extracted_year": hit.extracted_year,
                            "extracted_market_size": hit.extracted_market_size,
                            "extracted_ratio": hit.extracted_ratio,
                            "extracted_growth_rate": hit.extracted_growth_rate,
                            "source_verified": hit.source_verified,
                        },
                        "criteria": [
                            "市场定义、应用场景、技术特征与目标细分路径不冲突",
                            "数值口径清晰（年份、单位/币种、指标类型）",
                            "优先最近年份，过旧数据需降置信度",
                            "明显是泛市场/无关市场的数据应拒绝",
                        ],
                        "output_schema": {
                            "is_target_market": "boolean",
                            "data_quality_passed": "boolean",
                            "confidence": "number",
                            "reason": "string",
                            "issues": ["string"],
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        payload = self._complete_json(
            messages,
            model=_resolve_model(self.planning_model, self.client.model if self.client else "gpt-5.1-codex"),
            temperature=min(0.2, self.planning_temperature),
        )
        if not payload:
            return None

        reason = _normalize_text(payload.get("reason")) or "LLM 未返回证据审核结论"
        issues = _normalize_text_list(payload.get("issues"), limit=8)
        if issues:
            reason = f"{reason}；问题点：{'；'.join(issues)}"

        return LLMEvidenceReview(
            is_target_market=bool(payload.get("is_target_market", False)),
            data_quality_passed=bool(payload.get("data_quality_passed", False)),
            confidence=self._clamp01(payload.get("confidence")),
            reason=reason,
            issues=issues,
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
