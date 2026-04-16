from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

import app as app_module


def build_payload() -> dict:
    return {
        "province": "浙江省",
        "company_name": "浙江达航数据技术有限公司",
        "product_name": "示例产品",
        "product_code": "P-001",
        "year": "2026",
        "month": "3",
        "day": "22",
        "intro": "企业介绍：示例\n\n产品介绍：示例",
        "sale_23": "100",
        "total_mkt_23": "1000",
        "pct_23": "10%",
        "rank_23": "第1",
        "sale_24": "120",
        "total_mkt_24": "1200",
        "pct_24": "10%",
        "rank_24": "第1",
        "sale_25": "150",
        "total_mkt_25": "1500",
        "pct_25": "10%",
        "rank_25": "第1",
        "sources": [],
        "competitors": [],
    }


def build_other_payload() -> dict:
    payload = build_payload()
    payload.update(
        {
            "template_type": "other",
            "company_intro_text": "这是一段企业介绍。",
            "proof_scope": "全国",
            "market_name": "高安全性自锁紧型电源连接系统",
            "sources": [
                {
                    "name": "来源1",
                    "url": "https://example.com/market-1",
                    "chart_title": "图表1",
                    "chart_2023": "444.48",
                    "chart_2024": "463.15",
                    "chart_2025": "482.6",
                    "analysis": "这一层市场规模按照行业研究资料测算。",
                },
                {
                    "name": "来源2",
                    "url": "https://example.com/market-2",
                    "chart_title": "图表2",
                    "chart_2023": "222.1",
                    "chart_2024": "240.8",
                    "chart_2025": "258.3",
                    "analysis": "这一层进一步缩小到目标产品的市场口径。",
                },
            ],
            "chapter2_layers": [
                {
                    "name": "连接器市场",
                },
                {
                    "name": "高安全电源连接系统市场",
                },
            ],
            "chapter1_sections": [
                {
                    "key": "background_overview",
                    "title": "背景与概述",
                    "paragraphs": ["第一章示例段落"],
                }
            ],
            "resolved_company_profiles": [
                {
                    "requested_name": "浙江达航数据技术有限公司",
                    "company_name": "浙江达航数据技术有限公司",
                    "company_url": "https://aiqicha.baidu.com/company_basic_1",
                    "registered_capital": "1000万人民币",
                    "established_date": "2020-01-01",
                    "legal_representative": "张三",
                    "company_address": "杭州市示例路1号",
                    "main_business": "连接系统研发",
                    "matched_exactly": True,
                }
            ],
        }
    )
    return payload


def test_generate_requires_template_type():
    client = TestClient(app_module.app)
    payload = build_payload()

    resp = client.post("/generate", json=payload)

    assert resp.status_code == 422
    detail = resp.json().get("detail", [])
    assert any(item.get("loc", [])[-1] == "template_type" for item in detail)


def test_other_chapter1_endpoint_returns_sections(monkeypatch):
    client = TestClient(app_module.app)

    def fake_generate_other_chapter1(product_name, _config, allow_partial=False):
        assert product_name == "示例产品"
        assert allow_partial is False
        return {
            "sections": [
                {"key": "background_overview", "title": "背景与概述", "paragraphs": ["段落1", "段落2"]},
            ],
            "warnings": ["第一章部分段落不足，已补齐占位内容"],
        }

    monkeypatch.setattr(app_module, "generate_other_chapter1", fake_generate_other_chapter1)

    resp = client.post("/other-proof/chapter1", json={"product_name": "示例产品"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["sections"][0]["key"] == "background_overview"
    assert body["warnings"] == ["第一章部分段落不足，已补齐占位内容"]


def test_other_chapter1_endpoint_returns_504_on_timeout(monkeypatch):
    client = TestClient(app_module.app)

    def fake_generate_other_chapter1(_product_name, _config, allow_partial=False):
        assert allow_partial is False
        raise app_module.OtherProofTimeoutError("第一章生成超时。你可以直接重试，或勾选“第一章失败后跳过继续生成”。")

    monkeypatch.setattr(app_module, "generate_other_chapter1", fake_generate_other_chapter1)

    resp = client.post("/other-proof/chapter1", json={"product_name": "示例产品"})

    assert resp.status_code == 504
    assert "第一章生成超时" in resp.json().get("detail", "")


def test_other_chapter1_endpoint_passes_allow_partial(monkeypatch):
    client = TestClient(app_module.app)

    def fake_generate_other_chapter1(product_name, _config, allow_partial=False):
        assert product_name == "示例产品"
        assert allow_partial is True
        return {"sections": [], "warnings": ["allow_partial=true"]}

    monkeypatch.setattr(app_module, "generate_other_chapter1", fake_generate_other_chapter1)

    resp = client.post("/other-proof/chapter1", json={"product_name": "示例产品", "allow_partial": True})

    assert resp.status_code == 200
    assert resp.json().get("warnings") == ["allow_partial=true"]


def test_other_chapter1_section_endpoint_returns_section(monkeypatch):
    client = TestClient(app_module.app)

    def fake_generate_other_chapter1_section(product_name, section_key, generated_sections, _config):
        assert product_name == "示例产品"
        assert section_key == "background_overview"
        assert isinstance(generated_sections, list)
        return {
            "section": {"key": "background_overview", "title": "背景与概述", "paragraphs": ["段落1"]},
            "warnings": [],
        }

    monkeypatch.setattr(app_module, "generate_other_chapter1_section", fake_generate_other_chapter1_section)

    resp = client.post(
        "/other-proof/chapter1-section",
        json={"product_name": "示例产品", "section_key": "background_overview", "generated_sections": []},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["section"]["key"] == "background_overview"
    assert body["warnings"] == []


def test_other_chapter1_section_endpoint_returns_504_on_timeout(monkeypatch):
    client = TestClient(app_module.app)

    def fake_generate_other_chapter1_section(_product_name, _section_key, _generated_sections, _config):
        raise app_module.OtherProofTimeoutError("第一章《背景与概述》生成失败，请重试。")

    monkeypatch.setattr(app_module, "generate_other_chapter1_section", fake_generate_other_chapter1_section)

    resp = client.post(
        "/other-proof/chapter1-section",
        json={"product_name": "示例产品", "section_key": "background_overview", "generated_sections": []},
    )

    assert resp.status_code == 504
    assert "第一章《背景与概述》生成失败" in resp.json().get("detail", "")


def test_rewrite_header_titles_updates_matching_header_paragraph():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    header_xml = f"""
    <w:hdr xmlns:w="{ns}">
      <w:p><w:r><w:t>旧公司旧产品市场占有率证明报告</w:t></w:r></w:p>
      <w:p><w:r><w:t>页码信息</w:t></w:r></w:p>
    </w:hdr>
    """.strip().encode("utf-8")
    file_map = {"word/header2.xml": header_xml}

    app_module.rewrite_header_titles(file_map, "新公司", "新产品")

    rendered = file_map["word/header2.xml"].decode("utf-8")
    assert "新公司新产品市场占有率证明报告" in rendered
    assert "页码信息" in rendered


def test_rewrite_summary_market_research_phrase_replaces_quoted_product():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    doc = ET.fromstring(
        f"""
        <w:document xmlns:w="{ns}">
          <w:body>
            <w:p><w:r><w:t>对“旧产品”细分市场进行拆分和规模测算。</w:t></w:r></w:p>
          </w:body>
        </w:document>
        """
    )
    app_module.rewrite_summary_market_research_phrase(doc, "新产品")
    rendered = "".join(node.text or "" for node in doc.findall(f".//{{{ns}}}t"))
    assert "对“新产品”细分市场进行拆分和规模测算" in rendered


def test_apply_body_plain_paragraph_justification_only_for_body_text():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    root = ET.fromstring(
        f"""
        <w:document xmlns:w="{ns}">
          <w:body>
            <w:p>
              <w:r>
                <w:rPr><w:rFonts w:eastAsia="宋体"/><w:sz w:val="24"/></w:rPr>
                <w:t>这是正文段落，应设置为两端对齐。</w:t>
              </w:r>
            </w:p>
            <w:p>
              <w:r>
                <w:rPr><w:rFonts w:eastAsia="宋体"/><w:sz w:val="24"/></w:rPr>
                <w:t>一、背景与概述</w:t>
              </w:r>
            </w:p>
            <w:tbl>
              <w:tr>
                <w:tc>
                  <w:p>
                    <w:r>
                      <w:rPr><w:rFonts w:eastAsia="宋体"/><w:sz w:val="24"/></w:rPr>
                      <w:t>表格内正文不改</w:t>
                    </w:r>
                  </w:p>
                </w:tc>
              </w:tr>
            </w:tbl>
          </w:body>
        </w:document>
        """
    )

    app_module.apply_body_plain_paragraph_justification(root)

    paragraphs = root.findall(f".//{{{ns}}}p")
    body_jc = paragraphs[0].find(f"./{{{ns}}}pPr/{{{ns}}}jc")
    heading_jc = paragraphs[1].find(f"./{{{ns}}}pPr/{{{ns}}}jc")
    table_jc = paragraphs[2].find(f"./{{{ns}}}pPr/{{{ns}}}jc")

    assert body_jc is not None
    assert body_jc.get(f"{{{ns}}}val") == "both"
    assert heading_jc is None
    assert table_jc is None


def test_other_company_lookup_endpoint_returns_resolved_profiles(monkeypatch):
    client = TestClient(app_module.app)

    def fake_lookup(items):
        assert items == [{"company_name": "浙江达航数据技术有限公司", "confirmed_url": None}]
        return {
            "status": "resolved",
            "resolved": [
                {
                    "requested_name": "浙江达航数据技术有限公司",
                    "company_name": "浙江达航数据技术有限公司",
                    "company_url": "https://www.qcc.com/firm/example",
                    "registered_capital": "500万人民币",
                    "established_date": "2019-02-03",
                    "legal_representative": "李四",
                    "company_address": "宁波市示例路2号",
                    "main_business": "电器制造",
                    "matched_exactly": True,
                }
            ],
            "pending": [],
        }

    monkeypatch.setattr(app_module, "lookup_other_companies", fake_lookup)

    resp = client.post(
        "/other-proof/company-lookup",
        json={"companies": [{"company_name": "浙江达航数据技术有限公司"}]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["pending"] == []
    assert body["resolved"][0]["company_name"] == "浙江达航数据技术有限公司"


def test_other_company_lookup_endpoint_returns_400_on_qcc_failure(monkeypatch):
    client = TestClient(app_module.app)

    def fake_lookup(_items):
        raise app_module.OtherProofError("企查查没有找到“浙江达航数据技术有限公司”的精确结果，请确认公司全称，并保持 Chrome 已登录企查查。")

    monkeypatch.setattr(app_module, "lookup_other_companies", fake_lookup)

    resp = client.post(
        "/other-proof/company-lookup",
        json={"companies": [{"company_name": "浙江达航数据技术有限公司"}]},
    )

    assert resp.status_code == 400
    assert "企查查没有找到" in resp.json()["detail"]


def test_generate_other_requires_confirmed_profiles(monkeypatch, tmp_path: Path):
    client = TestClient(app_module.app)
    payload = build_other_payload()
    payload["resolved_company_profiles"] = []

    monkeypatch.chdir(tmp_path)

    resp = client.post("/generate", json=payload)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "请先填写第三章企业基本信息"


def test_generate_self_requires_chart_data(monkeypatch, tmp_path: Path):
    client = TestClient(app_module.app)
    payload = build_payload()
    payload["template_type"] = "self"
    payload["sources"] = [
        {
            "name": "来源1",
            "url": "https://example.com/market-1",
            "chart_title": "图表1：测试",
            "chart_2023": "",
            "chart_2024": "463.15",
            "chart_2025": "482.6",
            "analysis": "测试",
        }
    ]

    resp = client.post("/generate", json=payload)

    assert resp.status_code == 400
    assert "缺少 2023 年市场规模" in resp.json()["detail"]


def test_generate_other_requires_complete_manual_company_profiles(monkeypatch, tmp_path: Path):
    client = TestClient(app_module.app)
    payload = build_other_payload()
    payload["resolved_company_profiles"][0]["main_business"] = ""

    monkeypatch.chdir(tmp_path)

    resp = client.post("/generate", json=payload)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "请先填写“浙江达航数据技术有限公司”的主营业务"


def test_generate_self_template_returns_docx(monkeypatch, tmp_path: Path):
    client = TestClient(app_module.app)
    payload = build_payload()
    payload["template_type"] = "self"

    def fake_generate(_data, _template_path, output_path):
        Path(output_path).write_bytes(b"PK\x03\x04fake-docx")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_module, "generate_docx_v4", fake_generate)

    resp = client.post("/generate", json=payload)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "output.docx" in resp.headers.get("content-disposition", "")


def test_generate_other_template_returns_docx(monkeypatch, tmp_path: Path):
    client = TestClient(app_module.app)
    payload = build_other_payload()

    def fake_generate(_data, _template_path, output_path):
        Path(output_path).write_bytes(b"PK\x03\x04fake-other-docx")
        return []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_module, "generate_other_docx", fake_generate)

    resp = client.post("/generate", json=payload)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "output.docx" in resp.headers.get("content-disposition", "")


def test_generate_other_template_returns_warning_header(monkeypatch, tmp_path: Path):
    client = TestClient(app_module.app)
    payload = build_other_payload()

    def fake_generate(_data, _template_path, output_path):
        Path(output_path).write_bytes(b"PK\x03\x04fake-other-docx")
        return ["第一章《行业发展趋势》未生成成功，已写入占位内容"]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_module, "generate_other_docx", fake_generate)

    resp = client.post("/generate", json=payload)

    assert resp.status_code == 200
    raw = resp.headers.get("X-Generate-Warnings")
    assert raw
    decoded = json.loads(urllib.parse.unquote(raw))
    assert decoded == ["第一章《行业发展趋势》未生成成功，已写入占位内容"]
