from __future__ import annotations

import json
from pathlib import Path
from threading import RLock

from .models import InferConfigPatch, InferenceConfig


def _model_dump(model):
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _model_validate(model_cls, payload):
    if hasattr(model_cls, "model_validate"):
        return model_cls.model_validate(payload)
    return model_cls.parse_obj(payload)


class ConfigStore:
    def __init__(self, path: str = "data/inference/config/inference_config.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._config = self._load()

    def _load(self) -> InferenceConfig:
        if not self.path.exists():
            config = InferenceConfig()
            self._write(config)
            return config

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return _model_validate(InferenceConfig, raw)

    def _write(self, config: InferenceConfig) -> None:
        self.path.write_text(
            json.dumps(_model_dump(config), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def get(self) -> InferenceConfig:
        with self._lock:
            return _model_validate(InferenceConfig, _model_dump(self._config))

    def update(self, patch: InferConfigPatch) -> InferenceConfig:
        with self._lock:
            current = _model_dump(self._config)
            updates = _model_dump(patch)
            updates = {k: v for k, v in updates.items() if v is not None}
            current.update(updates)
            self._config = _model_validate(InferenceConfig, current)
            self._write(self._config)
            return _model_validate(InferenceConfig, _model_dump(self._config))
