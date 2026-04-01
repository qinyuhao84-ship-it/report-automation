from __future__ import annotations

import xml.etree.ElementTree as ET

import other_proof
from other_proof import (
    _build_chart_number_plan,
    _build_chapter1_prompt,
    _build_company_rows,
    _rewrite_dynamic_chart_references,
    _rewrite_other_header_titles,
    _set_paragraph_text,
    _validate_manual_company_profiles,
    _ensure_supply_chain_subsections,
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
    visible = [p for p in target["paragraphs"] if str(p).strip() and p != other_proof.PLACEHOLDER_TEXT][:6]
    assert len(visible) == 6
    assert visible[0] == "上游材料环节以铜材和工程塑料为主，供应稳定性直接影响交付周期。"
    assert all(str(item).strip() for item in visible[1:])


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


def test_build_chart_number_plan_uses_layer_count_as_offset():
    plan = _build_chart_number_plan(layer_count=2, company_count=3)

    assert plan["layer"] == [1, 2]
    assert plan["company"] == [3, 4, 5]
    assert plan["comparison"] == 6
    assert plan["share"] == 7
    assert plan["chapter5_source"] == 8


def test_rewrite_dynamic_chart_references_replaces_hardcoded_rank_and_chart_numbers():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def build_paragraph(text: str) -> ET.Element:
        return ET.fromstring(
            f"""
            <w:p xmlns:w="{ns}">
              <w:r><w:t>{text}</w:t></w:r>
            </w:p>
            """
        )

    paragraphs = [
        build_paragraph("应急用IEC电源连接器市场规模及各企业销售额如图表8所示。"),
        build_paragraph("图表9：2023-2025年宁波意缆可电器有限公司高安全性自锁紧型电源连接系统市场占有率"),
        build_paragraph("图表10 政府端数据来源"),
        build_paragraph("由以上分析可知，......“市场占有率全球第一”的市场地位结论成立。"),
    ]
    _rewrite_dynamic_chart_references(
        children=paragraphs,
        chart_plan={"comparison": 6, "share": 7, "chapter5_source": 8},
        self_row={
            "display_name": "宁波意缆可电器有限公司",
            "pct23": "17.73%",
            "pct24": "17.68%",
            "pct25": "19.10%",
        },
        rank_map={"2025": {"宁波意缆可电器有限公司": 2}},
        proof_scope="全国",
        product_name="高安全性自锁紧型电源连接系统",
    )

    rendered = [
        "".join(node.text or "" for node in p.findall(f".//{{{ns}}}t"))
        for p in paragraphs
    ]
    assert "图表6" in rendered[0]
    assert rendered[1].startswith("图表7：")
    assert rendered[2].startswith("图表8")
    assert "全国第二" in rendered[3]
    assert "全球第一" not in rendered[3]


def test_ensure_supply_chain_subsections_splits_combined_markers():
    paragraphs = [
        "（一）上游环节说明：A。（二）中游环节说明：B。（三）下游环节说明：C。",
    ]
    result = _ensure_supply_chain_subsections(paragraphs)

    assert len(result) >= 6
    assert "（二）" not in result[1]
    assert "（三）" not in result[2]
    assert "A" in result[1]
    assert "B" in result[2]


def test_rewrite_other_header_titles_updates_header_company_and_product():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    header_xml = f"""
    <w:hdr xmlns:w="{ns}">
      <w:p><w:r><w:t>宁波意缆可电器有限公司高安全性自锁紧型电源连接系统市场占有率证明报告</w:t></w:r></w:p>
      <w:p><w:r><w:t>不应修改</w:t></w:r></w:p>
    </w:hdr>
    """.strip().encode("utf-8")
    file_map = {"word/header2.xml": header_xml}

    _rewrite_other_header_titles(
        file_map=file_map,
        company_name="宏一集团有限公司",
        product_name="电源连接器系统",
    )

    root = ET.fromstring(file_map["word/header2.xml"])
    rendered = "".join(node.text or "" for node in root.findall(f".//{{{ns}}}t"))
    assert "宏一集团有限公司电源连接器系统市场占有率证明报告" in rendered
    assert "不应修改" in rendered
