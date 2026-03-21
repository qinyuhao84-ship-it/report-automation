from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from inference.engine import MarketInferenceEngine
from inference.models import InferenceConfig, InferenceInput, TaskStatus
from inference.providers import ProviderHit


class FakeProvider:
    def __init__(self):
        self.name = "mitata"
        self.calls = 0
        self.config = SimpleNamespace(max_results=5)

    def search(self, query: str, max_results: int = 5, market_path=None):
        self.calls += 1
        return [
            ProviderHit(
                provider=self.name,
                query=query,
                title="纸袋机行业分析",
                url="https://example.com/report",
                snippet="2025年纸袋机市场规模约为1000万元，占比约15%",
                captured_at=datetime.utcnow(),
                extracted_year=None,
                extracted_market_size=None,
                extracted_ratio=None,
                confidence=0.2,
                market_path=list(market_path or []),
            )
        ]


class FakePlan:
    def __init__(self):
        self.query = "LLM 优化后的查询"
        self.market_path = ["中国", "纸袋机", "全自动纸袋机"]
        self.next_paths = [["中国", "纸袋机", "全自动纸袋机"]]
        self.should_stop = False
        self.confidence = 0.8
        self.reason = "收窄到可直接拿到市场规模的层级"


class FakeExtraction:
    def __init__(self):
        self.year = 2025
        self.market_size = 1000.0
        self.ratio = 0.15
        self.confidence = 0.95


class FakeLlmOrchestrator:
    def plan_round(self, **kwargs):
        return FakePlan()

    def enrich_hit(self, **kwargs):
        return FakeExtraction()

    def is_available(self):
        return True


class FailingLlmOrchestrator:
    def plan_round(self, **kwargs):
        raise RuntimeError("llm unavailable")

    def enrich_hit(self, **kwargs):
        raise RuntimeError("llm unavailable")

    def is_available(self):
        return False


def _build_input() -> InferenceInput:
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


def test_engine_uses_llm_plan_and_extraction():
    config = InferenceConfig(max_search_rounds=2, evidence_min_sources=1, target_share_threshold=0.10)
    provider = FakeProvider()
    engine = MarketInferenceEngine(config=config, providers=[provider], llm_orchestrator=FakeLlmOrchestrator())

    result = engine.run("task-llm", _build_input())

    assert result.status == TaskStatus.REACHED
    assert provider.calls == 1
    assert result.final_market_path[-1] == "全自动纸袋机"
    assert result.evidence_chain[0].extracted_market_size == 1000.0
    assert result.evidence_chain[0].extracted_ratio == 0.15
    assert any("LLM" in item.reason or "收窄" in item.reason for item in result.attempt_log)


def test_engine_falls_back_when_llm_raises():
    config = InferenceConfig(max_search_rounds=1, evidence_min_sources=1, target_share_threshold=0.10)
    provider = FakeProvider()
    engine = MarketInferenceEngine(config=config, providers=[provider], llm_orchestrator=FailingLlmOrchestrator())

    result = engine.run("task-fallback", _build_input())

    assert result.status in {TaskStatus.REACHED, TaskStatus.NOT_REACHED}
    assert provider.calls == 1
    assert result.evidence_chain
