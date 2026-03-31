from __future__ import annotations

import xml.etree.ElementTree as ET

import other_proof
from other_proof import (
    _build_chapter1_prompt,
    _build_company_rows,
    _set_paragraph_text,
    _validate_manual_company_profiles,
    lookup_other_companies,
    normalize_chapter1_sections,
)


def test_chapter1_prompt_uses_report_style_requirements():
    prompt = _build_chapter1_prompt("桥梁防撞主动预警系统以及多级消能防撞装置")

    assert "2800-3200 字" in prompt
    assert "行业研究报告" in prompt
    assert "禁止输出“总体工作原理”“机械自锁结构”这类孤立小标题或短语" in prompt
    assert "industry_supply_chain 必须包含“（一）到（五）”五个小分类" in prompt


def test_normalize_chapter1_sections_merges_heading_fragments():
    sections, warnings = normalize_chapter1_sections(
        [
            {
                "key": "working_principle",
                "title": "工作原理",
                "paragraphs": [
                    "总体工作原理",
                    "系统通过感知模块、决策模块和执行模块之间的实时联动，形成对桥梁通航风险的主动识别与预警闭环。",
                    "多级防护逻辑",
                    "在碰撞风险逐步提升时，装置会依次触发预警、减速引导和缓冲防护等响应机制。",
                ],
            }
        ]
    )

    target = next(item for item in sections if item["key"] == "working_principle")
    assert target["paragraphs"][0].startswith("总体工作原理：系统通过感知模块")
    assert target["paragraphs"][1].startswith("多级防护逻辑：在碰撞风险逐步提升时")
    assert any("工作原理" in item for item in warnings)


def test_normalize_chapter1_sections_supply_chain_always_has_five_subsections():
    sections, _warnings = normalize_chapter1_sections(
        [
            {
                "key": "industry_supply_chain",
                "title": "行业供应链",
                "paragraphs": ["上游材料环节以铜材和工程塑料为主，供应稳定性直接影响交付周期。"],
            }
        ]
    )
    target = next(item for item in sections if item["key"] == "industry_supply_chain")
    visible = [p for p in target["paragraphs"] if str(p).strip() and p != other_proof.PLACEHOLDER_TEXT][:5]
    assert len(visible) == 5
    assert visible[0].startswith("（一）")
    assert visible[1].startswith("（二）")
    assert visible[2].startswith("（三）")
    assert visible[3].startswith("（四）")
    assert visible[4].startswith("（五）")


def test_lookup_other_companies_uses_browser_qcc_result(monkeypatch):
    def fake_lookup(requested_name):
        assert requested_name == "宏一集团有限公司"
        return {
            "requested_name": requested_name,
            "company_name": requested_name,
            "company_url": "https://www.qcc.com/firm/example",
            "registered_capital": "5000万人民币",
            "established_date": "2001-02-03",
            "legal_representative": "张三",
            "company_address": "浙江省乐清市示例路1号",
            "main_business": "连接器制造",
            "matched_exactly": True,
        }

    monkeypatch.setattr(other_proof, "_lookup_company_profile_via_qcc_browser", fake_lookup)

    result = lookup_other_companies([{"company_name": "宏一集团有限公司"}])

    assert result["status"] == "resolved"
    assert result["pending"] == []
    assert result["resolved"][0]["company_name"] == "宏一集团有限公司"


def test_lookup_other_companies_raises_when_qcc_lookup_fails(monkeypatch):
    def fake_lookup(_requested_name):
        raise other_proof.OtherProofError("企查查没有找到“宏一集团有限公司”的精确结果，请确认公司全称，并保持 Chrome 已登录企查查。")

    monkeypatch.setattr(other_proof, "_lookup_company_profile_via_qcc_browser", fake_lookup)

    try:
        lookup_other_companies([{"company_name": "宏一集团有限公司"}])
    except other_proof.OtherProofError as exc:
        assert "企查查没有找到" in str(exc)
    else:
        raise AssertionError("expected OtherProofError")


def test_validate_manual_company_profiles_requires_all_fields():
    try:
        _validate_manual_company_profiles(
            company_name="宏一集团有限公司",
            competitors=[],
            profiles=[
                {
                    "requested_name": "宏一集团有限公司",
                    "company_name": "宏一集团有限公司",
                    "registered_capital": "5188万元",
                    "established_date": "2001-10-19",
                    "legal_representative": "沈对",
                    "company_address": "慈溪市观海卫镇师东村",
                    "main_business": "",
                }
            ],
        )
    except other_proof.OtherProofError as exc:
        assert str(exc) == "请先填写“宏一集团有限公司”的主营业务"
    else:
        raise AssertionError("expected OtherProofError")


def test_build_company_rows_competitor_sales_come_from_market_times_share():
    data = {
        "company_name": "申报公司",
        "sale_23": "100",
        "sale_24": "110",
        "sale_25": "120",
        "total_mkt_23": "1000",
        "total_mkt_24": "1100",
        "total_mkt_25": "1200",
        "pct_23": "10%",
        "pct_24": "10%",
        "pct_25": "10%",
    }
    profiles = [
        {
            "requested_name": "申报公司",
            "company_name": "申报公司",
            "company_url": "u-self",
            "registered_capital": "1亿",
            "established_date": "2000-01-01",
            "legal_representative": "甲",
            "company_address": "地址1",
            "main_business": "主营1",
        },
        {
            "requested_name": "竞品A",
            "company_name": "竞品A",
            "company_url": "u-a",
            "registered_capital": "2亿",
            "established_date": "2001-01-01",
            "legal_representative": "乙",
            "company_address": "地址2",
            "main_business": "主营2",
        },
    ]
    competitors = [{"name": "竞品A", "p23": "5%", "p24": "6%", "p25": "7%"}]
    warnings: list[str] = []
    rows = _build_company_rows(
        data=data,
        resolved_profiles=profiles,
        competitors=competitors,
        market_name="测试市场",
        proof_scope="全球",
        warnings=warnings,
    )
    rival = next(item for item in rows if item["display_name"] == "竞品A")
    assert rival["sale23"] == "50"
    assert rival["sale24"] == "66"
    assert rival["sale25"] == "84"


def test_set_paragraph_text_replaces_old_hyperlink_text():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paragraph = ET.fromstring(
        f"""
        <w:p xmlns:w="{ns}">
          <w:pPr/>
          <w:hyperlink>
            <w:r><w:t>https://old.example.com</w:t></w:r>
          </w:hyperlink>
        </w:p>
        """
    )
    _set_paragraph_text(paragraph, "https://new.example.com")
    rendered = "".join(node.text or "" for node in paragraph.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
    assert rendered == "https://new.example.com"
