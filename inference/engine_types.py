from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class CandidatePath:
    path: List[str]
    parent_market_size: Optional[float]
    depth: int


@dataclass
class BestSnapshot:
    path: List[str]
    market_size: float
    market_share: float
    evidence_score: float
    verified_sources: int
    total_sources: int
    fit_passed: bool
    fit_reason: str
    fit_confidence: float
