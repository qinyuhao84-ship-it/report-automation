from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from threading import RLock
from typing import Callable, Dict, Optional
from uuid import uuid4

from .config import ConfigStore
from .engine import MarketInferenceEngine
from .models import CreateTaskResponse, InferenceInput, TaskResult, TaskStatus
from .storage import TaskStore


class InferenceTaskManager:
    def __init__(
        self,
        config_store: Optional[ConfigStore] = None,
        task_store: Optional[TaskStore] = None,
        engine_factory: Optional[Callable[[object], object]] = None,
        max_workers: int = 2,
    ) -> None:
        self.config_store = config_store or ConfigStore()
        self.task_store = task_store or TaskStore()
        self.engine_factory = engine_factory or (lambda cfg: MarketInferenceEngine(cfg))
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="infer-task")

        self._futures: Dict[str, Future] = {}
        self._lock = RLock()

    def submit(self, payload: InferenceInput) -> CreateTaskResponse:
        task_id = uuid4().hex
        pending = self._build_pending_result(task_id, payload)
        self.task_store.save(pending)

        with self._lock:
            self._futures[task_id] = self.executor.submit(self._run_task, task_id, payload)

        return CreateTaskResponse(task_id=task_id, status=TaskStatus.PENDING)

    def get_task(self, task_id: str) -> Optional[TaskResult]:
        return self.task_store.get(task_id)

    def get_config(self):
        return self.config_store.get()

    def update_config(self, patch):
        return self.config_store.update(patch)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False)

    def _run_task(self, task_id: str, payload: InferenceInput) -> None:
        running = self._build_running_result(task_id, payload)
        self.task_store.save(running)

        try:
            config = self.config_store.get()
            engine = self.engine_factory(config)
            result = engine.run(task_id, payload)
            self.task_store.save(result)
        except Exception as exc:
            failed = self._build_failed_result(task_id, payload, str(exc))
            self.task_store.save(failed)

    def _build_pending_result(self, task_id: str, payload: InferenceInput) -> TaskResult:
        now = datetime.utcnow()
        config = self.config_store.get()
        return TaskResult(
            task_id=task_id,
            status=TaskStatus.PENDING,
            started_at=now,
            finished_at=None,
            input=payload,
            target_scope=payload.market_scope,
            latest_year=payload.latest_sales_year,
            target_share_threshold=config.target_share_threshold,
            final_market_path=[],
            market_size_latest_year_wan_cny=None,
            market_share_latest_year=None,
            reached_target=False,
            evidence_score=0.0,
            evidence_chain=[],
            attempt_log=[],
            assumption_notes=[],
            error_message=None,
        )

    def _build_running_result(self, task_id: str, payload: InferenceInput) -> TaskResult:
        now = datetime.utcnow()
        config = self.config_store.get()
        return TaskResult(
            task_id=task_id,
            status=TaskStatus.RUNNING,
            started_at=now,
            finished_at=None,
            input=payload,
            target_scope=payload.market_scope,
            latest_year=payload.latest_sales_year,
            target_share_threshold=config.target_share_threshold,
            final_market_path=[],
            market_size_latest_year_wan_cny=None,
            market_share_latest_year=None,
            reached_target=False,
            evidence_score=0.0,
            evidence_chain=[],
            attempt_log=[],
            assumption_notes=["任务已进入执行队列"],
            error_message=None,
        )

    def _build_failed_result(self, task_id: str, payload: InferenceInput, error: str) -> TaskResult:
        now = datetime.utcnow()
        config = self.config_store.get()
        return TaskResult(
            task_id=task_id,
            status=TaskStatus.FAILED,
            started_at=now,
            finished_at=now,
            input=payload,
            target_scope=payload.market_scope,
            latest_year=payload.latest_sales_year,
            target_share_threshold=config.target_share_threshold,
            final_market_path=[],
            market_size_latest_year_wan_cny=None,
            market_share_latest_year=None,
            reached_target=False,
            evidence_score=0.0,
            evidence_chain=[],
            attempt_log=[],
            assumption_notes=["执行失败，请检查 error_message"],
            error_message=error,
        )
