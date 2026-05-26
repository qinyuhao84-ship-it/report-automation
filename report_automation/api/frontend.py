from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from report_automation.settings import FRONTEND_DIR

router = APIRouter()


@router.get("/")
def index():
    frontend_path = FRONTEND_DIR / "index.html"
    try:
        html = frontend_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="前端模板加载失败") from exc
    return HTMLResponse(content=html)


@router.get("/frontend/{file_path:path}")
def frontend_assets(file_path: str):
    frontend_dir = FRONTEND_DIR.resolve()
    target = (frontend_dir / file_path).resolve()
    if not target.is_relative_to(frontend_dir) or not target.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(target)
