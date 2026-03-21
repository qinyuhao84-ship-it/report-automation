from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from .models import EvidenceRecord, EstimationMethod, InferenceConfig, InferenceInput
from .scoring import clamp


@dataclass(frozen=True)
class EstimateResult:
    method: EstimationMethod
    market_size_latest_year: float
    market_share_latest_year: float
    confidence: float
    market_path: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def estimate_share_x_parent(parent_market_size: float, ratio: float) -> float:
    if parent_market_size <= 0:
        raise ValueError("parent_market_size must be positive")
    if ratio <= 0 or ratio > 1:
        raise ValueError("ratio must be in (0, 1]")
    return float(parent_market_size) * float(ratio)


def estimate_cagr_projection(base_market_size: float, cagr: float, years: int) -> float:
    if base_market_size <= 0:
        raise ValueError("base_market_size must be positive")
    if years < 0:
        raise ValueError("years must be non-negative")
    if cagr <= -1:
        raise ValueError("cagr must be greater than -1")
    return float(base_market_size) * ((1.0 + float(cagr)) ** years)


def estimate_analogous_benchmark(revenue: float, benchmark_share: float) -> float:
    if revenue < 0:
        raise ValueError("revenue must be non-negative")
    if benchmark_share <= 0 or benchmark_share > 1:
        raise ValueError("benchmark_share must be in (0, 1]")
    return float(revenue) / float(benchmark_share)


def _available_sales(input_model: InferenceInput) -> List[Tuple[int, float]]:
    series = [
        (2023, input_model.sales_2023),
        (2024, input_model.sales_2024),
        (2025, input_model.sales_2025),
    ]
    return [(year, float(value)) for year, value in series if value is not None]


def _latest_sales(input_model: InferenceInput) -> Tuple[int, float]:
    series = _available_sales(input_model)
    if not series:
        return input_model.latest_sales_year, input_model.latest_sales_value
    return series[-1]


def _cagr(series: Sequence[Tuple[int, float]]) -> Optional[float]:
    if len(series) < 2:
        return None
    start_year, start_value = series[0]
    end_year, end_value = series[-1]
    if start_value <= 0 or end_value <= 0:
        return None
    years = max(1, end_year - start_year)
    return (end_value / start_value) ** (1 / years) - 1.0


def _competitor_penalty(input_model: InferenceInput) -> float:
    count = len(input_model.competitors)
    if count <= 0:
        return 0.0
    if count <= 2:
        return 0.01
    if count <= 5:
        return 0.025
    return 0.04


def _base_share_guess(input_model: InferenceInput, threshold: float) -> float:
    guess = threshold
    guess -= _competitor_penalty(input_model)
    text = " ".join([input_model.product_name, input_model.product_category, input_model.product_intro, input_model.company_intro]).lower()
    if any(k in text for k in ["龙头", "leading", "leader"]):
        guess += 0.02
    if any(k in text for k in ["niche", "细分", "专精"]):
        guess += 0.01
    if any(k in text for k in ["global", "海外"]):
        guess -= 0.01
    return clamp(guess, 0.02, 0.45)


def _evidence_support(evidence_chain: Iterable[EvidenceRecord]) -> Tuple[Optional[float], Optional[float]]:
    market_sizes: List[float] = []
    ratios: List[float] = []
    for item in evidence_chain:
        if item.extracted_market_size is not None and item.extracted_market_size > 0:
            market_sizes.append(float(item.extracted_market_size))
        if item.extracted_ratio is not None and item.extracted_ratio > 0:
            ratios.append(float(item.extracted_ratio))
    return (market_sizes[-1] if market_sizes else None, ratios[-1] if ratios else None)


def _build_market_path(input_model: InferenceInput, current_path: Sequence[str], method: EstimationMethod) -> List[str]:
    path = list(current_path)
    if not path:
        path.append(input_model.market_scope.value)
    if input_model.product_category and input_model.product_category not in path:
        path.append(input_model.product_category)
    if method == EstimationMethod.SHARE_X_PARENT and input_model.product_name not in path:
        path.append(input_model.product_name)
    if method == EstimationMethod.CAGR_PROJECTION and "增长率视角" not in path:
        path.append("增长率视角")
    if method == EstimationMethod.ANALOGOUS_BENCHMARK and "类比视角" not in path:
        path.append("类比视角")
    return path


class MarketEstimator:
    def __init__(self, config: InferenceConfig) -> None:
        self.config = config

    def estimate(
        self,
        input_model: InferenceInput,
        evidence_chain: Sequence[EvidenceRecord],
        round_index: int,
        current_path: Sequence[str],
        parent_market_size: Optional[float] = None,
    ) -> EstimateResult:
        _latest_year, latest_sales = _latest_sales(input_model)
        latest_sales = max(0.0, float(latest_sales))
        series = _available_sales(input_model)
        cagr = _cagr(series)
        support_market_size, support_ratio = _evidence_support(evidence_chain)
        base_share = _base_share_guess(input_model, self.config.target_share_threshold)

        for method in self.config.estimation_priority:
            if method == EstimationMethod.SHARE_X_PARENT:
                ratio = support_ratio if support_ratio is not None else base_share
                ratio = clamp(ratio, 0.01, 0.95)
                if support_market_size is not None:
                    derived_ratio = clamp(latest_sales / support_market_size, 0.01, 0.95)
                    ratio = clamp((ratio + derived_ratio) / 2.0, 0.01, 0.95)

                if parent_market_size is not None and parent_market_size > 0:
                    market_size = estimate_share_x_parent(parent_market_size, clamp(1.0 - ratio, 0.05, 0.95))
                    market_size = max(market_size, latest_sales)
                else:
                    market_size = estimate_analogous_benchmark(max(latest_sales, 0.01), ratio)

                return EstimateResult(
                    method=method,
                    market_size_latest_year=float(market_size),
                    market_share_latest_year=float(clamp(latest_sales / market_size if market_size else 0.0, 0.0, 1.0)),
                    confidence=clamp(0.62 + 0.08 * bool(support_market_size) + 0.06 * bool(support_ratio) - round_index * 0.015),
                    market_path=_build_market_path(input_model, current_path, method),
                    notes=["采用占比乘以上层规模优先策略"],
                )

            if method == EstimationMethod.CAGR_PROJECTION and cagr is not None:
                share = clamp(base_share * (1 + cagr / 2.0), 0.01, 0.95)
                market_size = estimate_analogous_benchmark(max(latest_sales, 0.01), share)
                return EstimateResult(
                    method=method,
                    market_size_latest_year=float(market_size),
                    market_share_latest_year=float(clamp(latest_sales / market_size if market_size else 0.0, 0.0, 1.0)),
                    confidence=clamp(0.56 + 0.08 * (1 if len(series) >= 3 else 0) - round_index * 0.015),
                    market_path=_build_market_path(input_model, current_path, method),
                    notes=["采用 CAGR 推算策略"],
                )

            if method == EstimationMethod.ANALOGOUS_BENCHMARK:
                share = clamp(base_share * 0.9, 0.01, 0.95)
                market_size = estimate_analogous_benchmark(max(latest_sales, 0.01), share)
                return EstimateResult(
                    method=method,
                    market_size_latest_year=float(market_size),
                    market_share_latest_year=float(clamp(latest_sales / market_size if market_size else 0.0, 0.0, 1.0)),
                    confidence=clamp(0.48 + 0.05 * min(len(input_model.competitors), 3) - round_index * 0.01),
                    market_path=_build_market_path(input_model, current_path, method),
                    notes=["采用同类市场对标换算策略"],
                )

        share = clamp(base_share, 0.01, 0.95)
        market_size = estimate_analogous_benchmark(max(latest_sales, 0.01), share)
        return EstimateResult(
            method=EstimationMethod.ANALOGOUS_BENCHMARK,
            market_size_latest_year=float(market_size),
            market_share_latest_year=float(clamp(latest_sales / market_size if market_size else 0.0, 0.0, 1.0)),
            confidence=0.35,
            market_path=_build_market_path(input_model, current_path, EstimationMethod.ANALOGOUS_BENCHMARK),
            notes=["未命中估算优先级，使用保守回退策略"],
        )

    def summarize_assumptions(self, input_model: InferenceInput) -> List[str]:
        notes = [
            f"市场范围按 {input_model.market_scope.value} 处理",
            f"最近财年按 {input_model.latest_sales_year} 处理",
            f"目标阈值为 {self.config.target_share_threshold:.2%}",
        ]
        if input_model.competitors:
            notes.append(f"竞争对手数量 {len(input_model.competitors)}，已用于保守修正")
        return notes
