from __future__ import annotations

import inspect
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from threading import Event, RLock
from typing import Callable, Dict, Optional
from uuid import uuid4

from .config import ConfigStore
from .engine import MarketInferenceEngine
from .models import CancelTaskResponse, CreateTaskResponse, InferenceInput, TaskResult, TaskStatus
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
        self._cancel_events: Dict[str, Event] = {}
        self._lock = RLock()

    def submit(self, payload: InferenceInput) -> CreateTaskResponse:
        task_id = uuid4().hex
        pending = self._build_pending_result(task_id, payload)
        self.task_store.save(pending)

        with self._lock:
            self._cancel_events[task_id] = Event()
            self._futures[task_id] = self.executor.submit(self._run_task, task_id, payload)

        return CreateTaskResponse(task_id=task_id, status=TaskStatus.PENDING)

    def cancel(self, task_id: str) -> CancelTaskResponse:
        task = self.task_store.get(task_id)
        if task is None:
            return CancelTaskResponse(task_id=task_id, status=TaskStatus.FAILED, accepted=False, message="任务不存在")
        if self._is_terminal(task.status):
            return CancelTaskResponse(task_id=task_id, status=task.status, accepted=False, message="任务已结束，无法取消")

        with self._lock:
            cancel_event = self._cancel_events.get(task_id)
            future = self._futures.get(task_id)
            if cancel_event is not None:
                cancel_event.set()
            cancelled_in_queue = bool(future.cancel()) if future is not None and not future.running() else False

        if cancelled_in_queue:
            cancelled = self._build_cancelled_result(task_id, task.input, "任务在排队阶段已取消")
            self.task_store.save(cancelled)
            return CancelTaskResponse(task_id=task_id, status=TaskStatus.CANCELLED, accepted=True, message="任务已取消")

        return CancelTaskResponse(
            task_id=task_id,
            status=TaskStatus.RUNNING,
            accepted=True,
            message="已收到停止请求，当前轮次完成后将尽快中断",
        )

    def get_task(self, task_id: str) -> Optional[TaskResult]:
        return self.task_store.get(task_id)

    def get_config(self):
        return self.config_store.get()

    def update_config(self, patch):
        return self.config_store.update(patch)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False)

    def _run_task(self, task_id: str, payload: InferenceInput) -> None:
        if self._is_cancel_requested(task_id):
            self.task_store.save(self._build_cancelled_result(task_id, payload, "任务在启动前已取消"))
            self._cleanup_task(task_id)
            return

        running = self._build_running_result(task_id, payload)
        self.task_store.save(running)

        try:
            config = self.config_store.get()
            engine = self.engine_factory(config)
            result = self._run_engine(engine, task_id, payload)
            self.task_store.save(result)
        except Exception as exc:
            failed = self._build_failed_result(task_id, payload, str(exc))
            self.task_store.save(failed)
        finally:
            self._cleanup_task(task_id)

    def _run_engine(self, engine: object, task_id: str, payload: InferenceInput) -> TaskResult:
        run_callable = getattr(engine, "run")
        try:
            params = inspect.signature(run_callable).parameters
            if "should_stop" in params:
                return run_callable(task_id, payload, should_stop=lambda: self._is_cancel_requested(task_id))
        except Exception:
            pass
        return run_callable(task_id, payload)

    def _is_cancel_requested(self, task_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(task_id)
        return bool(event and event.is_set())

    def _cleanup_task(self, task_id: str) -> None:
        with self._lock:
            self._futures.pop(task_id, None)
            self._cancel_events.pop(task_id, None)

    @staticmethod
    def _is_terminal(status: TaskStatus) -> bool:
        return status in {TaskStatus.REACHED, TaskStatus.NOT_REACHED, TaskStatus.CANCELLED, TaskStatus.FAILED}

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

    def _build_cancelled_result(self, task_id: str, payload: InferenceInput, reason: str) -> TaskResult:
        now = datetime.utcnow()
        config = self.config_store.get()
        return TaskResult(
            task_id=task_id,
            status=TaskStatus.CANCELLED,
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
            assumption_notes=[reason],
            error_message=None,
        )
