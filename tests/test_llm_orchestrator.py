from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from inference import llm_orchestrator as llm_module
from inference.llm_orchestrator import LLMOrchestrator, OpenAICompatibleClient
from inference.models import InferenceConfig, InferenceInput
from inference.providers import ProviderHit


class MockTransport:
    def __init__(self):
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.requests.append({"url": str(request.url), "body": body})
        prompt = json.dumps(body.get("messages", []), ensure_ascii=False)
        if "EXTRACT" in prompt:
            payload = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "year": 2025,
                                    "market_size": 1234.5,
                                    "ratio": 0.12,
                                    "confidence": 0.93,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
        elif "PATH_PROPOSAL" in prompt:
            payload = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "market_paths": [
                                        ["中国", "纸袋机", "全自动纸袋机", "可调式舌口全自动纸袋机"],
                                        ["中国", "包装设备", "纸袋设备", "全自动纸袋机"],
                                        ["全球", "纸袋机", "工业包装机械"],
                                    ],
                                    "confidence": 0.88,
                                    "reason": "优先给出可检索到公开市场规模的多维细分链",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
        else:
            payload = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "query": "纸袋机 全自动 2025 市场规模",
                                    "provider_queries": {
                                        "mitata": "秘塔：纸袋机 全自动 2025 市场规模",
                                        "yuanbao": "元宝：纸袋机 全自动 2025 市场规模"
                                    },
                                    "market_path": ["中国", "纸袋机", "全自动纸袋机"],
                                    "next_paths": [
                                        ["中国", "纸袋机", "全自动纸袋机"],
                                        ["中国", "纸袋机", "自动化包装设备"],
                                    ],
                                    "should_stop": False,
                                    "confidence": 0.86,
                                    "reason": "优先收窄到可直接找到市场规模数据的层级",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
        return httpx.Response(200, json=payload, request=request)


@pytest.fixture()
def sample_input() -> InferenceInput:
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


def _sample_hit() -> ProviderHit:
    return ProviderHit(
        provider="mitata",
        query="placeholder",
        title="纸袋机行业分析",
        url="https://example.com/report",
        snippet="2025年纸袋机市场规模约为1200万元，占比约12%。",
        captured_at=datetime.utcnow(),
        extracted_year=None,
        extracted_market_size=None,
        extracted_ratio=None,
        confidence=0.1,
        market_path=["中国", "纸袋机"],
    )


def test_llm_config_reads_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_BASE", "https://proxy.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "gpt-5.1-codex")

    config = InferenceConfig()

    assert config.llm_enabled is True
    assert config.llm_api_base == "https://proxy.example.com/v1"
    assert config.llm_api_key_env == "OPENAI_API_KEY"
    assert config.llm_model == "gpt-5.1-codex"


def test_resolve_model_maps_deepseek_r1_alias():
    assert llm_module._resolve_model("deepseek-r1", "fallback-model") == "deepseek-reasoner"
    assert llm_module._resolve_model("DeepSeek-R1", "fallback-model") == "deepseek-reasoner"
    assert llm_module._resolve_model("deepseek-chat", "fallback-model") == "deepseek-chat"


def test_llm_orchestrator_plan_and_extract(sample_input):
    transport = MockTransport()
    client = OpenAICompatibleClient(
        api_base="https://proxy.example.com/v1",
        api_key="sk-test",
        model="gpt-5.1-codex",
        timeout_seconds=10,
        transport=transport,
    )
    orchestrator = LLMOrchestrator(client=client)

    proposal = orchestrator.propose_market_paths(
        input_model=sample_input,
        latest_year=2025,
        max_paths=8,
    )

    assert proposal is not None
    assert len(proposal.market_paths) >= 2
    assert proposal.market_paths[0][-1] == "可调式舌口全自动纸袋机"

    plan = orchestrator.plan_round(
        input_model=sample_input,
        current_path=["中国", "纸袋机"],
        latest_year=2025,
        round_index=1,
        evidence_summary=[],
        fallback_query="fallback query",
    )

    assert plan is not None
    assert plan.query == "纸袋机 全自动 2025 市场规模"
    assert plan.provider_queries.get("mitata", "").startswith("秘塔")
    assert plan.market_path[-1] == "全自动纸袋机"
    assert plan.next_paths

    enriched = orchestrator.enrich_hit(
        input_model=sample_input,
        hit=_sample_hit(),
        current_path=["中国", "纸袋机"],
        round_index=1,
    )

    assert enriched.extracted_year == 2025
    assert enriched.extracted_market_size == pytest.approx(1234.5)
    assert enriched.extracted_ratio == pytest.approx(0.12)
    assert enriched.confidence == pytest.approx(0.93)
    assert len(transport.requests) >= 2


def test_llm_orchestrator_disabled_without_credentials(sample_input):
    config = InferenceConfig(llm_enabled=False, llm_api_base=None, llm_model="gpt-5.1-codex")
    orchestrator = LLMOrchestrator.from_config(config)

    assert orchestrator.is_available() is False
    assert orchestrator.plan_round(
        input_model=sample_input,
        current_path=["中国", "纸袋机"],
        latest_year=2025,
        round_index=1,
        evidence_summary=[],
        fallback_query="fallback query",
    ) is None


def test_openai_client_retries_on_429_then_succeeds(monkeypatch):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, json={"error": "rate limit"}, request=request, headers={"Retry-After": "1"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
            request=request,
        )

    sleep_values = []
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: sleep_values.append(seconds))

    client = OpenAICompatibleClient(
        api_base="https://proxy.example.com/v1",
        api_key="sk-test",
        model="gpt-5.1-codex",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_max_attempts=3,
        retry_base_delay_ms=800,
        retry_max_delay_ms=8000,
    )

    content = client.complete([{"role": "user", "content": "hi"}], section_key="background_overview")
    assert calls["count"] == 2
    assert sleep_values == [1.0]
    assert '"ok": true' in content


def test_openai_client_retries_on_empty_content(monkeypatch):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": ""}}]},
                request=request,
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
            request=request,
        )

    sleep_values = []
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: sleep_values.append(seconds))
    monkeypatch.setattr(llm_module.random, "uniform", lambda _a, _b: 0)

    client = OpenAICompatibleClient(
        api_base="https://proxy.example.com/v1",
        api_key="sk-test",
        model="gpt-5.1-codex",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_max_attempts=3,
        retry_base_delay_ms=800,
        retry_max_delay_ms=8000,
    )

    content = client.complete([{"role": "user", "content": "hi"}], section_key="background_overview")
    assert calls["count"] == 2
    assert sleep_values == [0.0]
    assert '"ok": true' in content


def test_openai_client_does_not_retry_on_400():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(400, json={"error": "bad request"}, request=request)

    client = OpenAICompatibleClient(
        api_base="https://proxy.example.com/v1",
        api_key="sk-test",
        model="gpt-5.1-codex",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_max_attempts=3,
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.complete([{"role": "user", "content": "hi"}], section_key="definition")
    assert calls["count"] == 1
