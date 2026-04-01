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
                source_verified=True,
                quote_text="2025年纸袋机市场规模约为1000万元，占比约15%",
            )
        ]


class EmptyEvidenceProvider(FakeProvider):
    def search(self, query: str, max_results: int = 5, market_path=None):
        self.calls += 1
        return [
            ProviderHit(
                provider=self.name,
                query=query,
                title="仅AI摘要",
                url="https://example.com/ai-summary",
                snippet="这是摘要，没有原文可核验数据",
                captured_at=datetime.utcnow(),
                extracted_year=None,
                extracted_market_size=None,
                extracted_ratio=None,
                confidence=0.2,
                market_path=list(market_path or []),
                source_verified=False,
                quote_text=None,
            )
        ]


class FakePlan:
    def __init__(self):
        self.query = "LLM 优化后的查询"
        self.provider_queries = {"mitata": "秘塔查询", "yuanbao": "元宝查询"}
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
    def propose_market_paths(self, **kwargs):
        class Proposal:
            market_paths = [
                ["中国", "纸袋机", "全自动纸袋机", "可调式舌口全自动纸袋机"],
                ["中国", "包装设备", "纸袋设备", "全自动纸袋机"],
            ]
            confidence = 0.82
            reason = "多路径穷举以提升命中率"

        return Proposal()

    def plan_round(self, **kwargs):
        return FakePlan()

    def enrich_hit(self, **kwargs):
        return FakeExtraction()

    def is_available(self):
        return True

    def validate_market_fit(self, **kwargs):
        class Fit:
            is_aligned = True
            confidence = 0.9
            reason = "主导产品与细分市场一致"
        return Fit()


class FailingLlmOrchestrator:
    def plan_round(self, **kwargs):
        raise RuntimeError("llm unavailable")

    def enrich_hit(self, **kwargs):
        raise RuntimeError("llm unavailable")

    def is_available(self):
        return False

    def validate_market_fit(self, **kwargs):
        raise RuntimeError("llm unavailable")


class MisalignedLlmOrchestrator(FakeLlmOrchestrator):
    def validate_market_fit(self, **kwargs):
        class Fit:
            is_aligned = False
            confidence = 0.92
            reason = "证据显示市场定义偏向通用连接器，无法证明是自锁紧型细分市场"

        return Fit()


def _build_input() -> InferenceInput:
    return InferenceInput(
        company_name="Demo Co",
        product_name="Demo Product",
        product_code="3907039900",
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
    assert provider.calls >= 1
    assert result.final_market_path[-1] == "全自动纸袋机"
    assert result.evidence_chain[0].extracted_market_size == 1000.0
    assert result.evidence_chain[0].extracted_ratio == 0.15
    assert any("LLM" in item.reason or "收窄" in item.reason for item in result.attempt_log)
    assert any(item.provider == "llm-path-proposal" for item in result.attempt_log)


def test_engine_falls_back_when_llm_raises():
    config = InferenceConfig(max_search_rounds=1, evidence_min_sources=1, target_share_threshold=0.10)
    provider = FakeProvider()
    engine = MarketInferenceEngine(config=config, providers=[provider], llm_orchestrator=FailingLlmOrchestrator())

    result = engine.run("task-fallback", _build_input())

    assert result.status in {TaskStatus.REACHED, TaskStatus.NOT_REACHED}
    assert provider.calls >= 1
    assert result.evidence_chain


def test_engine_does_not_reach_when_llm_fit_check_rejects_market_path():
    config = InferenceConfig(max_search_rounds=1, evidence_min_sources=1, target_share_threshold=0.10, market_fit_required=True)
    provider = FakeProvider()
    engine = MarketInferenceEngine(config=config, providers=[provider], llm_orchestrator=MisalignedLlmOrchestrator())

    result = engine.run("task-fit-reject", _build_input())

    assert result.status == TaskStatus.NOT_REACHED
    assert result.market_share_latest_year is not None
    assert result.market_share_latest_year >= 0.10
    assert result.market_fit_passed is False
    assert "无法证明" in (result.market_fit_reason or "")


def test_engine_accepts_ai_only_evidence_but_marks_source_unverified():
    config = InferenceConfig(max_search_rounds=1, evidence_min_sources=1, target_share_threshold=0.10, market_fit_required=True)
    provider = EmptyEvidenceProvider()
    engine = MarketInferenceEngine(config=config, providers=[provider], llm_orchestrator=FakeLlmOrchestrator())

    result = engine.run("task-no-evidence", _build_input())

    assert result.status == TaskStatus.REACHED
    assert result.market_fit_passed is True
    assert result.evidence_chain
    assert any(item.source_verified is False for item in result.evidence_chain)
