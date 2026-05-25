from __future__ import annotations

from fastapi import APIRouter, HTTPException

from inference import InferenceConfig
from other_proof import (
    OtherProofError,
    OtherProofTimeoutError,
    generate_other_chapter1,
    generate_other_chapter1_section,
    lookup_other_companies,
)
from report_automation.schemas import Chapter1Request, Chapter1SectionRequest, CompanyLookupRequest
from report_automation.services.errors import other_proof_error_detail

router = APIRouter(prefix="/other-proof")


@router.post("/chapter1")
def generate_other_proof_chapter1_api(payload: Chapter1Request):
    try:
        return generate_other_chapter1(
            payload.product_name,
            InferenceConfig(),
            allow_partial=bool(payload.allow_partial),
        )
    except OtherProofTimeoutError as exc:
        raise HTTPException(status_code=504, detail=other_proof_error_detail(exc)) from exc
    except OtherProofError as exc:
        raise HTTPException(status_code=400, detail=other_proof_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/chapter1-section")
def generate_other_proof_chapter1_section_api(payload: Chapter1SectionRequest):
    try:
        return generate_other_chapter1_section(
            payload.product_name,
            payload.section_key,
            [item.model_dump() for item in payload.generated_sections],
            InferenceConfig(),
        )
    except OtherProofTimeoutError as exc:
        raise HTTPException(status_code=504, detail=other_proof_error_detail(exc)) from exc
    except OtherProofError as exc:
        raise HTTPException(status_code=400, detail=other_proof_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/company-lookup")
def lookup_other_proof_companies_api(payload: CompanyLookupRequest):
    try:
        return lookup_other_companies([item.model_dump() for item in payload.companies])
    except OtherProofError as exc:
        raise HTTPException(status_code=400, detail=other_proof_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
