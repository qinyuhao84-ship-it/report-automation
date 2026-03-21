from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
from inference.config import ConfigStore
from inference.models import InferenceInput, TaskResult, TaskStatus
from inference.task_manager import InferenceTaskManager
from inference.storage import TaskStore


class FakeEngine:
    def __init__(self, config) -> None:
        self.config = config

    def run(self, task_id: str, payload: InferenceInput) -> TaskResult:
        now = datetime.utcnow()
        return TaskResult(
            task_id=task_id,
            status=TaskStatus.REACHED,
            started_at=now,
            finished_at=now,
            input=payload,
            target_scope=payload.market_scope,
            latest_year=payload.latest_sales_year,
            target_share_threshold=self.config.target_share_threshold,
            final_market_path=[payload.market_scope.value, payload.product_name],
            market_size_latest_year_wan_cny=1000.0,
            market_share_latest_year=0.15,
            reached_target=True,
            evidence_score=0.8,
            evidence_chain=[],
            attempt_log=[],
            assumption_notes=["fake engine for API test"],
            error_message=None,
        )


@pytest.fixture()
def client_with_fake_manager(tmp_path: Path):
    config_store = ConfigStore(path=str(tmp_path / "config.json"))
    task_store = TaskStore(base_dir=str(tmp_path / "tasks"))
    manager = InferenceTaskManager(
        config_store=config_store,
        task_store=task_store,
        engine_factory=lambda cfg: FakeEngine(cfg),
        max_workers=1,
    )

    original = app_module.inference_task_manager
    app_module.inference_task_manager = manager

    client = TestClient(app_module.app)
    try:
        yield client
    finally:
        app_module.inference_task_manager = original
        manager.shutdown()


def payload() -> dict:
    return {
        "company_name": "Demo Co",
        "product_name": "Demo Product",
        "product_intro": "intro",
        "product_category": "category",
        "company_intro": "company",
        "competitors": ["A", "B"],
        "sale_23": 100.0,
        "sale_24": 120.0,
        "sale_25": 150.0,
        "target_scope": "CN",
    }


def _poll_result(client: TestClient, task_id: str, timeout_sec: float = 2.0):
    deadline = time.time() + timeout_sec
    last = None
    while time.time() < deadline:
        resp = client.get(f"/infer-market-share/{task_id}")
        assert resp.status_code == 200
        last = resp.json()
        if last["status"] in {"REACHED", "NOT_REACHED", "FAILED"}:
            return last
        time.sleep(0.05)
    return last


def test_submit_and_query_flow(client_with_fake_manager: TestClient):
    submit_resp = client_with_fake_manager.post("/infer-market-share", json=payload())
    assert submit_resp.status_code == 202
    body = submit_resp.json()
    assert body["status"] == "PENDING"
    task_id = body["task_id"]

    result = _poll_result(client_with_fake_manager, task_id)
    assert result is not None
    assert result["status"] == "REACHED"
    assert result["reached_target"] is True
    assert result["market_share_latest_year"] == pytest.approx(0.15)


def test_query_404_for_unknown_task(client_with_fake_manager: TestClient):
    resp = client_with_fake_manager.get("/infer-market-share/unknown-task")
    assert resp.status_code == 404


def test_config_endpoints(client_with_fake_manager: TestClient):
    get_resp = client_with_fake_manager.get("/infer-config")
    assert get_resp.status_code == 200
    assert get_resp.json()["max_search_rounds"] == 10

    update_resp = client_with_fake_manager.put(
        "/infer-config",
        json={"max_search_rounds": 6, "target_share_threshold": 0.12},
    )
    assert update_resp.status_code == 200
    data = update_resp.json()
    assert data["max_search_rounds"] == 6
    assert data["target_share_threshold"] == pytest.approx(0.12)


def test_config_priority_order_persists(client_with_fake_manager: TestClient):
    update_resp = client_with_fake_manager.put(
        "/infer-config",
        json={
            "provider_priority": ["doubao", "yuanbao", "mitata"],
            "estimation_priority": ["cagr_projection", "share_x_parent", "analogous_benchmark"],
        },
    )
    assert update_resp.status_code == 200
    data = update_resp.json()
    assert data["provider_priority"] == ["doubao", "yuanbao", "mitata"]
    assert data["estimation_priority"] == ["cagr_projection", "share_x_parent", "analogous_benchmark"]
