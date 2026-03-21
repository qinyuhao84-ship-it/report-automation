from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Dict, Optional

from .models import TaskResult


def _model_dump(model):
    if hasattr(model, "model_dump"):
        return model.model_dump(by_alias=True)
    return model.dict()


def _model_validate(model_cls, payload):
    if hasattr(model_cls, "model_validate"):
        return model_cls.model_validate(payload)
    return model_cls.parse_obj(payload)


class TaskStore:
    def __init__(self, base_dir: str = "data/inference/tasks") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._cache: Dict[str, TaskResult] = {}
        self._warmup()

    def _warmup(self) -> None:
        for fp in self.base_dir.glob("*.json"):
            try:
                raw = json.loads(fp.read_text(encoding="utf-8"))
                task = _model_validate(TaskResult, raw)
                self._cache[task.task_id] = task
            except Exception:
                # Skip corrupted records to keep service alive
                continue

    def save(self, task: TaskResult) -> None:
        with self._lock:
            self._cache[task.task_id] = task
            fp = self.base_dir / f"{task.task_id}.json"
            fp.write_text(
                json.dumps(_model_dump(task), ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

    def get(self, task_id: str) -> Optional[TaskResult]:
        with self._lock:
            task = self._cache.get(task_id)
            if task is None:
                return None
            if hasattr(task, "model_copy"):
                return task.model_copy(deep=True)
            return _model_validate(TaskResult, _model_dump(task))
