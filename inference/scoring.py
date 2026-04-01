from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence

from .models import AttemptRecord, EvidenceRecord, InferenceInput


@dataclass(frozen=True)
class ScoreBreakdown:
    lexical: float
    numeric: float
    recency: float
    path: float
    total: float


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _tokenize(text: str) -> List[str]:
    cleaned = text.replace("，", " ").replace("、", " ").replace(",", " ").replace("；", " ")
    tokens = []
    for chunk in cleaned.split():
        token = chunk.strip().lower()
        if token:
            tokens.append(token)
    return tokens


def _overlap_score(left: Sequence[str], right: Sequence[str]) -> float:
    if not left or not right:
        return 0.0
    left_set = set(left)
    right_set = set(right)
    overlap = len(left_set & right_set)
    baseline = max(len(left_set), len(right_set), 1)
    return overlap / baseline


def score_evidence(input_model: InferenceInput, evidence: EvidenceRecord, round_index: int) -> ScoreBreakdown:
    query_tokens = _tokenize(
        " ".join(
            [
                input_model.company_name,
                input_model.product_name,
                input_model.product_code,
                input_model.product_category,
                input_model.product_intro,
                input_model.company_intro,
                " ".join(evidence.market_path),
            ]
        )
    )
    snippet_tokens = _tokenize(" ".join([evidence.title, evidence.snippet, evidence.url]))
    lexical = _overlap_score(query_tokens, snippet_tokens)

    numeric = 0.0
    if evidence.extracted_market_size is not None:
        numeric += 0.45
    if evidence.extracted_ratio is not None:
        numeric += 0.25
    if evidence.extracted_year is not None:
        numeric += 0.10
    numeric = clamp(numeric)

    latest_year = input_model.latest_sales_year
    recency = 0.0
    if evidence.extracted_year is not None:
        gap = max(0, latest_year - evidence.extracted_year)
        recency = clamp(1.0 - gap * 0.2)

    path = clamp(0.15 + 0.05 * len(evidence.market_path) + round_index * 0.02)
    total = clamp(lexical * 0.40 + numeric * 0.35 + recency * 0.15 + path * 0.10)
    return ScoreBreakdown(lexical=lexical, numeric=numeric, recency=recency, path=path, total=total)


def score_evidence_chain(evidence_chain: Iterable[EvidenceRecord]) -> float:
    chain = list(evidence_chain)
    if not chain:
        return 0.0
    total = 0.0
    weight = 0.0
    for index, item in enumerate(chain, start=1):
        current = clamp(item.confidence)
        current_weight = 1.0 + index * 0.15
        total += current * current_weight
        weight += current_weight
    return clamp(total / weight if weight else 0.0)


def evidence_count_bonus(evidence_chain: Sequence[EvidenceRecord]) -> float:
    if not evidence_chain:
        return 0.0
    return clamp(0.05 * len(evidence_chain), 0.0, 0.2)


def attempt_action_for_score(score: float, has_results: bool) -> str:
    if not has_results:
        return "fallback"
    if score >= 0.75:
        return "stop"
    if score >= 0.45:
        return "expand"
    return "explore"
