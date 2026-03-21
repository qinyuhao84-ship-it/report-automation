from __future__ import annotations

import os
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, root_validator, validator


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    REACHED = "REACHED"
    NOT_REACHED = "NOT_REACHED"
    FAILED = "FAILED"


class MarketScope(str, Enum):
    CN = "CN"
    GLOBAL = "GLOBAL"


class EstimationMethod(str, Enum):
    SHARE_X_PARENT = "share_x_parent"
    CAGR_PROJECTION = "cagr_projection"
    ANALOGOUS_BENCHMARK = "analogous_benchmark"


class ProviderName(str, Enum):
    MITATA = "mitata"
    DOUBAO = "doubao"
    YUANBAO = "yuanbao"


class ProviderMode(str, Enum):
    BROWSER = "browser"
    HTTP = "http"
    STUB = "stub"


class SearchAction(str, Enum):
    EXPLORE = "explore"
    EXPAND = "expand"
    FALLBACK = "fallback"
    STOP = "stop"
    SKIP = "skip"


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().split())


def _normalize_optional_text(value):
    if value is None:
        return None
    text = _normalize_text(str(value))
    return text or None


def _enum_to_text(value) -> str:
    if isinstance(value, Enum):
        return str(value.value).strip().lower()
    return str(value).strip().lower()


def _coerce_float(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned.endswith("%"):
            cleaned = cleaned[:-1]
        return float(cleaned)
    return float(value)


def _normalize_market_scope(value):
    if value is None:
        return MarketScope.CN
    if isinstance(value, MarketScope):
        return value
    text = str(value).strip().upper()
    if text in {"CN", "CHINA", "CN_MAINLAND"}:
        return MarketScope.CN
    if text in {"GLOBAL", "WORLD", "OVERSEAS"}:
        return MarketScope.GLOBAL
    raise ValueError("market_scope 只能是 CN 或 GLOBAL")


def _default_llm_api_base():
    return _normalize_optional_text(os.getenv("OPENAI_API_BASE") or os.getenv("LLM_API_BASE"))


def _default_llm_enabled():
    api_base = _default_llm_api_base()
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    return bool(api_base and api_key)


def _default_llm_model():
    return _normalize_optional_text(os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5.1-codex")


class InferenceInput(BaseModel):
    company_name: str = Field(..., min_length=1)
    product_name: str = Field(..., min_length=1)
    product_intro: str = ""
    product_category: str = ""
    company_intro: str = ""
    sales_2023: Optional[float] = Field(default=None, alias="sale_23")
    sales_2024: Optional[float] = Field(default=None, alias="sale_24")
    sales_2025: Optional[float] = Field(default=None, alias="sale_25")
    competitors: List[str] = Field(default_factory=list)
    market_scope: MarketScope = Field(default=MarketScope.CN, alias="target_scope")

    model_config = ConfigDict(populate_by_name=True, use_enum_values=False)

    @validator("company_name", "product_name", "product_intro", "product_category", "company_intro", pre=True)
    def _strip_text(cls, value):
        if value is None:
            return ""
        return _normalize_text(str(value))

    @validator("sales_2023", "sales_2024", "sales_2025", pre=True)
    def _coerce_sales(cls, value):
        return _coerce_float(value)

    @validator("competitors", pre=True)
    def _normalize_competitors(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            raw_items = [item.strip() for item in value.replace("；", ";").replace("，", ",").split(",")]
            return [item for item in raw_items if item]
        items = []
        for item in value:
            text = _normalize_text(str(item))
            if text:
                items.append(text)
        return items

    @validator("market_scope", pre=True)
    def _coerce_market_scope(cls, value):
        return _normalize_market_scope(value)

    @root_validator(skip_on_failure=True)
    def _validate_sales_presence(cls, values):
        sales = [values.get("sales_2023"), values.get("sales_2024"), values.get("sales_2025")]
        if all(item is None for item in sales):
            raise ValueError("至少需要提供 2023/2024/2025 中的一项销售额")
        return values

    @property
    def latest_sales_year(self) -> int:
        for year, field_name in ((2025, "sales_2025"), (2024, "sales_2024"), (2023, "sales_2023")):
            if getattr(self, field_name) is not None:
                return year
        return 2025

    @property
    def latest_sales_value(self) -> float:
        for field_name in ("sales_2025", "sales_2024", "sales_2023"):
            value = getattr(self, field_name)
            if value is not None:
                return float(value)
        return 0.0


class ProviderConfig(BaseModel):
    name: ProviderName
    enabled: bool = True
    mode: ProviderMode = ProviderMode.BROWSER
    base_url: str = ""
    query_param: str = "q"
    timeout_seconds: int = Field(default=25, ge=5, le=120)
    max_results: int = Field(default=5, ge=1, le=20)
    search_input_selector: Optional[str] = None
    submit_selector: Optional[str] = None
    result_item_selector: Optional[str] = None
    title_selector: Optional[str] = None
    link_selector: Optional[str] = None
    snippet_selector: Optional[str] = None

    model_config = ConfigDict(use_enum_values=False)

    @validator("name", pre=True)
    def _normalize_name(cls, value):
        if isinstance(value, ProviderName):
            return value
        text = str(value).strip().lower()
        if text in {"mita", "mitata"}:
            return ProviderName.MITATA
        if text == "doubao":
            return ProviderName.DOUBAO
        if text == "yuanbao":
            return ProviderName.YUANBAO
        raise ValueError("provider name 必须是 mitata / doubao / yuanbao")

    @validator("mode", pre=True)
    def _normalize_mode(cls, value):
        if isinstance(value, ProviderMode):
            return value
        if value is None:
            return ProviderMode.BROWSER
        text = str(value).strip().lower()
        if text in {"browser", "web"}:
            return ProviderMode.BROWSER
        if text in {"http", "api"}:
            return ProviderMode.HTTP
        if text in {"stub", "mock"}:
            return ProviderMode.STUB
        raise ValueError("mode 只能是 browser / http / stub")


class InferenceConfig(BaseModel):
    market_scope_default: MarketScope = MarketScope.CN
    max_search_rounds: int = Field(default=10, ge=1, le=50)
    target_share_threshold: float = Field(default=0.10, ge=0.01, le=1.0)
    estimation_priority: List[EstimationMethod] = Field(
        default_factory=lambda: [
            EstimationMethod.SHARE_X_PARENT,
            EstimationMethod.CAGR_PROJECTION,
            EstimationMethod.ANALOGOUS_BENCHMARK,
        ]
    )
    provider_priority: List[ProviderName] = Field(
        default_factory=lambda: [
            ProviderName.MITATA,
            ProviderName.DOUBAO,
            ProviderName.YUANBAO,
        ]
    )
    evidence_min_sources: int = Field(default=1, ge=1, le=5)
    max_children_per_round: int = Field(default=4, ge=1, le=10)
    cny_per_usd: float = Field(default=7.2, gt=0)
    llm_enabled: bool = Field(default_factory=_default_llm_enabled)
    llm_api_base: Optional[str] = Field(default_factory=_default_llm_api_base)
    llm_api_key_env: str = Field(default="OPENAI_API_KEY")
    llm_model: str = Field(default_factory=_default_llm_model)
    llm_planning_model: Optional[str] = None
    llm_extraction_model: Optional[str] = None
    llm_timeout_seconds: int = Field(default=60, ge=5, le=300)
    llm_max_output_tokens: int = Field(default=1200, ge=64, le=8192)
    llm_planning_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_extraction_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_retry_attempts: int = Field(default=2, ge=0, le=5)
    llm_user_agent: str = Field(default="report-automation")
    providers: List[ProviderConfig] = Field(
        default_factory=lambda: [
            ProviderConfig(name=ProviderName.MITATA, mode=ProviderMode.BROWSER, base_url="https://metaso.cn"),
            ProviderConfig(name=ProviderName.DOUBAO, mode=ProviderMode.BROWSER, base_url="https://www.doubao.com"),
            ProviderConfig(name=ProviderName.YUANBAO, mode=ProviderMode.BROWSER, base_url="https://yuanbao.tencent.com"),
        ]
    )

    model_config = ConfigDict(use_enum_values=False)

    @validator("market_scope_default", pre=True)
    def _normalize_default_scope(cls, value):
        return _normalize_market_scope(value)

    @validator(
        "llm_api_base",
        "llm_api_key_env",
        "llm_model",
        "llm_planning_model",
        "llm_extraction_model",
        "llm_user_agent",
        pre=True,
    )
    def _strip_llm_text(cls, value):
        return _normalize_optional_text(value)

    @validator("estimation_priority", pre=True)
    def _normalize_estimation_priority(cls, value):
        if value is None:
            return []
        normalized = []
        for item in value:
            text = _enum_to_text(item)
            if text in {"share_x_parent", "ratio_times_parent", "direct"}:
                normalized.append(EstimationMethod.SHARE_X_PARENT)
            elif text in {"cagr_projection", "cagr"}:
                normalized.append(EstimationMethod.CAGR_PROJECTION)
            elif text in {"analogous_benchmark", "analog_projection", "analog"}:
                normalized.append(EstimationMethod.ANALOGOUS_BENCHMARK)
        return normalized or [
            EstimationMethod.SHARE_X_PARENT,
            EstimationMethod.CAGR_PROJECTION,
            EstimationMethod.ANALOGOUS_BENCHMARK,
        ]

    @validator("provider_priority", pre=True)
    def _normalize_provider_priority(cls, value):
        if value is None:
            return []
        normalized = []
        for item in value:
            text = _enum_to_text(item)
            if text in {"mita", "mitata"}:
                normalized.append(ProviderName.MITATA)
            elif text == "doubao":
                normalized.append(ProviderName.DOUBAO)
            elif text == "yuanbao":
                normalized.append(ProviderName.YUANBAO)
        return normalized or [
            ProviderName.MITATA,
            ProviderName.DOUBAO,
            ProviderName.YUANBAO,
        ]


class EvidenceRecord(BaseModel):
    provider: str
    query: str
    title: str
    url: str
    snippet: str
    captured_at: datetime
    extracted_year: Optional[int] = None
    extracted_market_size: Optional[float] = None
    extracted_ratio: Optional[float] = None
    method: Optional[EstimationMethod] = None
    market_path: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    model_config = ConfigDict(use_enum_values=False)


class AttemptRecord(BaseModel):
    round_index: int
    provider: str
    path: List[str]
    query: str
    market_size_latest_year: Optional[float]
    market_share_latest_year: Optional[float]
    evidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    action: SearchAction
    reason: str
    method: Optional[EstimationMethod] = None

    model_config = ConfigDict(use_enum_values=False)


class TaskResult(BaseModel):
    task_id: str
    status: TaskStatus
    started_at: datetime
    finished_at: Optional[datetime] = None
    input: InferenceInput
    target_scope: MarketScope = Field(default=MarketScope.CN)
    latest_year: int = 2025
    target_share_threshold: float = Field(default=0.10, ge=0.01, le=1.0)
    final_market_path: List[str] = Field(default_factory=list)
    market_size_latest_year: Optional[float] = Field(default=None, alias="market_size_latest_year_wan_cny")
    market_share_latest_year: Optional[float] = None
    reached_target: bool = False
    evidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_chain: List[EvidenceRecord] = Field(default_factory=list)
    attempt_log: List[AttemptRecord] = Field(default_factory=list)
    assumption_notes: List[str] = Field(default_factory=list)
    error_message: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True, use_enum_values=False)

    @property
    def market_size_latest_year_wan_cny(self) -> Optional[float]:
        return self.market_size_latest_year


class CreateTaskResponse(BaseModel):
    task_id: str
    status: TaskStatus


class InferConfigPatch(BaseModel):
    market_scope_default: Optional[MarketScope] = None
    max_search_rounds: Optional[int] = Field(default=None, ge=1, le=50)
    target_share_threshold: Optional[float] = Field(default=None, ge=0.01, le=1.0)
    estimation_priority: Optional[List[EstimationMethod]] = None
    provider_priority: Optional[List[ProviderName]] = None
    evidence_min_sources: Optional[int] = Field(default=None, ge=1, le=5)
    max_children_per_round: Optional[int] = Field(default=None, ge=1, le=10)
    cny_per_usd: Optional[float] = Field(default=None, gt=0)
    llm_enabled: Optional[bool] = None
    llm_api_base: Optional[str] = None
    llm_api_key_env: Optional[str] = None
    llm_model: Optional[str] = None
    llm_planning_model: Optional[str] = None
    llm_extraction_model: Optional[str] = None
    llm_timeout_seconds: Optional[int] = Field(default=None, ge=5, le=300)
    llm_max_output_tokens: Optional[int] = Field(default=None, ge=64, le=8192)
    llm_planning_temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    llm_extraction_temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    llm_retry_attempts: Optional[int] = Field(default=None, ge=0, le=5)
    llm_user_agent: Optional[str] = None
    providers: Optional[List[ProviderConfig]] = None
