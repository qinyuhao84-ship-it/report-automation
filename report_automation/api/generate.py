from __future__ import annotations

from fastapi import APIRouter

from report_automation.schemas import DataModel
from report_automation.services.report_generation import generate_report_file_response

router = APIRouter()


@router.post("/generate")
def generate_api(data: DataModel):
    return generate_report_file_response(data)
