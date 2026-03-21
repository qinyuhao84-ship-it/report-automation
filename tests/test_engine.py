from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import List

import pytest

from inference.engine import MarketInferenceEngine
from inference.models import InferenceConfig, InferenceInput, TaskStatus, SearchAction
from inference.providers import ProviderHit


class FakeProvider:
    def __init__(self, name: str, outcomes: List[object]):
        self.name = name
        self.outcomes = outcomes
        self.calls = 0
        self.config = SimpleNamespace(max_results=5)

    def search(self, query: str, max_results: int = 5, market_path=None):
        self.calls += 1
        idx = min(self.calls - 1, len(self.outcomes) - 1)
        outcome = self.outcomes[idx]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def build_input() -> InferenceInput:
    return InferenceInput(
        company_name="Demo Co",
        product_name="Demo Product",
        product_intro="企业产品介绍",
        product_category="工业设备",
        company_intro="企业介绍",
        competitors=["Alpha", "Beta"],
        sale_23=100.0,
        sale_24=120.0,
        sale_25=150.0,
        target_scope="CN",
    )


def make_hit(provider: str, ratio: float, market_size: float) -> ProviderHit:
    return ProviderHit(
        provider=provider,
        query="demo query",
        title="demo title",
        url=f"https://example.com/{provider}",
        snippet="demo snippet",
        captured_at=datetime.utcnow(),
        extracted_year=2025,
        extracted_market_size=market_size,
        extracted_ratio=ratio,
        confidence=0.9,
        market_path=["CN", "工业设备"],
    )


def test_engine_reaches_target_and_stops_early():
    config = InferenceConfig(target_share_threshold=0.10, max_search_rounds=3, evidence_min_sources=1)
    p1 = FakeProvider("mitata", [[make_hit("mitata", ratio=0.18, market_size=830.0)]])
    p2 = FakeProvider("doubao", [[make_hit("doubao", ratio=0.08, market_size=2000.0)]])

    engine = MarketInferenceEngine(config=config, providers=[p1, p2])
    result = engine.run("task-1", build_input())

    assert result.status == TaskStatus.REACHED
    assert result.reached_target is True
    assert result.market_share_latest_year is not None
    assert result.market_share_latest_year >= 0.10
    assert p1.calls == 1
    assert p2.calls == 0
    assert any(item.action == SearchAction.STOP for item in result.attempt_log)


def test_engine_not_reached_after_max_rounds():
    config = InferenceConfig(target_share_threshold=0.30, max_search_rounds=2, evidence_min_sources=1)
    p1 = FakeProvider("mitata", [[make_hit("mitata", ratio=0.03, market_size=5000.0)]])

    engine = MarketInferenceEngine(config=config, providers=[p1])
    result = engine.run("task-2", build_input())

    assert result.status == TaskStatus.NOT_REACHED
    assert result.reached_target is False
    assert result.market_share_latest_year is not None
    assert result.market_share_latest_year < 0.30
    assert result.attempt_log[-1].action == SearchAction.STOP


def test_engine_fallback_when_all_channels_fail():
    config = InferenceConfig(target_share_threshold=0.20, max_search_rounds=1, evidence_min_sources=1)
    p1 = FakeProvider("mitata", [RuntimeError("m1 down")])
    p2 = FakeProvider("doubao", [RuntimeError("m2 down")])

    engine = MarketInferenceEngine(config=config, providers=[p1, p2])
    result = engine.run("task-3", build_input())

    assert result.status == TaskStatus.NOT_REACHED
    assert any(item.action == SearchAction.FALLBACK for item in result.attempt_log)
    assert result.market_size_latest_year_wan_cny is not None


def test_engine_degrades_to_next_channel_after_failure():
    config = InferenceConfig(target_share_threshold=0.10, max_search_rounds=2, evidence_min_sources=1)
    p1 = FakeProvider("mitata", [RuntimeError("temporary outage")])
    p2 = FakeProvider("doubao", [[make_hit("doubao", ratio=0.15, market_size=1000.0)]])

    engine = MarketInferenceEngine(config=config, providers=[p1, p2])
    result = engine.run("task-4", build_input())

    assert result.status == TaskStatus.REACHED
    assert p1.calls == 1
    assert p2.calls == 1
    assert result.final_market_path
    assert any(item.provider == "mitata" and item.action == SearchAction.SKIP for item in result.attempt_log)
    assert any(item.provider == "doubao" and item.action == SearchAction.EXPLORE for item in result.attempt_log)
