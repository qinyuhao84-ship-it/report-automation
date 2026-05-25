from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from report_automation.docx.self_proof import extract_self_docx_fields

router = APIRouter()


@router.post("/api/extract-final-docx")
async def extract_final_docx(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix.lower() if file.filename else ""
    if suffix != ".docx":
        raise HTTPException(status_code=400, detail="仅支持 .docx 文件")

    tmp_path = None
    try:
        contents = await file.read()
        with tempfile.NamedTemporaryFile(prefix="upload-", suffix=".docx", delete=False) as tmp:
            tmp.write(contents)
            tmp_path = Path(tmp.name)

        result = extract_self_docx_fields(str(tmp_path))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="文件损坏，无法作为 .docx 打开") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"解析失败：{exc}") from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()

    return JSONResponse(content=result)
