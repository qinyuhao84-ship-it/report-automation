from __future__ import annotations

import json
import tempfile
import urllib.parse
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from report_automation.docx.self_proof import generate_docx_v4
from report_automation.other_proof import OtherProofError, generate_other_docx
from report_automation.schemas import DataModel
from report_automation.services.errors import other_proof_error_detail
from report_automation.settings import DOCX_MEDIA_TYPE, OTHER_TEMPLATE_PATH, SELF_TEMPLATE_PATH


def generate_report_file_response(data: DataModel) -> FileResponse:
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="report-", suffix=".docx", delete=False) as tmp:
            output_path = Path(tmp.name)

        headers = {}
        if data.template_type == "self":
            _require_template_exists(SELF_TEMPLATE_PATH)
            generate_docx_v4(data.model_dump(), SELF_TEMPLATE_PATH, output_path)
        elif data.template_type == "other":
            _require_template_exists(OTHER_TEMPLATE_PATH)
            warnings = generate_other_docx(data.model_dump(), OTHER_TEMPLATE_PATH, output_path)
            if warnings:
                headers["X-Generate-Warnings"] = urllib.parse.quote(json.dumps(warnings, ensure_ascii=False))
            if data.chapter1_replay_file_path:
                headers["X-Chapter1-Replay-File-Path"] = urllib.parse.quote(data.chapter1_replay_file_path)
        else:
            raise HTTPException(status_code=400, detail="不支持的模板类型")

        return FileResponse(
            output_path,
            media_type=DOCX_MEDIA_TYPE,
            filename="output.docx",
            headers=headers,
            background=_unlink_after_response(output_path),
        )
    except OtherProofError as exc:
        _cleanup_failed_output(output_path)
        raise HTTPException(status_code=400, detail=other_proof_error_detail(exc)) from exc
    except HTTPException:
        _cleanup_failed_output(output_path)
        raise
    except Exception as exc:
        _cleanup_failed_output(output_path)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _require_template_exists(path: Path) -> None:
    if not path.is_file():
        raise HTTPException(status_code=500, detail=f"模板文件不存在：{path.name}")


def _cleanup_failed_output(path: Path | None) -> None:
    if path is not None and path.exists():
        path.unlink()


def _unlink_after_response(path: Path):
    from starlette.background import BackgroundTask

    return BackgroundTask(lambda: path.exists() and path.unlink())
