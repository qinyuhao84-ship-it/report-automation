from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class SourceBlock(BaseModel):
    name: str
    url: str
    names: List[str] = Field(default_factory=list)
    urls: List[str] = Field(default_factory=list)
    chart_title: str
    analysis: str
    chart_2023: str = ""
    chart_2024: str = ""
    chart_2025: str = ""


class Competitor(BaseModel):
    name: str
    p23: str
    p24: str
    p25: str


class OtherProofLayer(BaseModel):
    name: str
    analysis: str = ""
    url: str = ""
    urls: List[str] = Field(default_factory=list)


class Chapter1Section(BaseModel):
    key: str
    title: str
    paragraphs: List[str]


class ResolvedCompanyProfile(BaseModel):
    requested_name: str
    company_name: str
    company_url: str
    registered_capital: str = ""
    established_date: str = ""
    legal_representative: str = ""
    company_address: str = ""
    main_business: str = ""
    matched_exactly: bool = False


class CompanyLookupItem(BaseModel):
    company_name: str
    confirmed_url: Optional[str] = None


class Chapter1Request(BaseModel):
    product_name: str
    allow_partial: bool = False


class Chapter1SectionRequest(BaseModel):
    product_name: str
    section_key: str
    generated_sections: List[Chapter1Section] = Field(default_factory=list)


class CompanyLookupRequest(BaseModel):
    companies: List[CompanyLookupItem]


class DataModel(BaseModel):
    template_type: Literal["self", "other"]
    province: str
    company_name: str
    product_name: str
    product_code: str
    year: str
    month: str
    day: str
    intro: str
    sale_23: str
    total_mkt_23: str
    pct_23: str
    rank_23: str
    sale_24: str
    total_mkt_24: str
    pct_24: str
    rank_24: str
    sale_25: str
    total_mkt_25: str
    pct_25: str
    rank_25: str
    sources: List[SourceBlock]
    competitors: List[Competitor]
    company_intro_text: Optional[str] = None
    proof_scope: Optional[str] = None
    market_name: Optional[str] = None
    chapter2_layers: List[OtherProofLayer] = Field(default_factory=list)
    chapter1_sections: List[Chapter1Section] = Field(default_factory=list)
    chapter1_replay_file_path: Optional[str] = None
    skip_chapter1: bool = False
    resolved_company_profiles: List[ResolvedCompanyProfile] = Field(default_factory=list)
