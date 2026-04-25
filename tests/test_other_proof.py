from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import httpx
import other_proof
import pytest
from other_proof import (
    _build_chart_number_plan,
    _build_chapter1_prompt,
    _build_chapter1_section_prompt,
    _build_company_rows,
    _highlight_self_row_in_comparison_table,
    _rewrite_summary_market_research_phrase,
    _rewrite_dynamic_chart_references,
    _rewrite_other_header_titles,
    _extract_json_payload,
    _resolve_chapter1_model_name,
    _set_paragraph_text,
    _validate_manual_company_profiles,
    _ensure_supply_chain_subsections,
    generate_other_chapter1,
    generate_other_chapter1_section,
    lookup_other_companies,
    normalize_chapter1_sections,
)


def test_chapter1_prompt_uses_report_style_requirements():
    prompt = _build_chapter1_prompt("桥梁防撞主动预警系统以及多级消能防撞装置")

    assert "2200-3200 字" in prompt
    assert "行业研究报告" in prompt
    assert "禁止输出“总体工作原理”“机械自锁结构”这类孤立小标题或短语" in prompt
    assert "每个一级部分至少 2 段" in prompt
    assert "industry_supply_chain 必须包含“（一）到（五）”五个小分类" in prompt


def test_chapter1_section_prompt_has_consulting_style_constraints():
    spec = {"key": "industry_trends", "title": "行业发展趋势", "slot_count": 28}
    prompt = _build_chapter1_section_prompt(
        product_name="高安全性自锁紧型电源连接系统",
        spec=spec,
        generated_sections=[
            {"key": "definition", "title": "定义", "paragraphs": ["该产品面向高可靠连接场景。"]},
        ],
    )

    assert "咨询报告/研究报告风格" in prompt
    assert "不写具体数字、年份、金额、比例、增速、排名、市场份额" in prompt
    assert "数十毫秒" in prompt
    assert "围绕产品本身" in prompt
    assert "本产品、该产品" in prompt
    assert '"section"' in prompt


def test_resolve_chapter1_model_name_maps_deepseek_r1_to_chat():
    assert _resolve_chapter1_model_name("deepseek-r1", "deepseek-r1") == "deepseek-chat"
    assert _resolve_chapter1_model_name("deepseek-reasoner", "deepseek-reasoner") == "deepseek-chat"
    assert _resolve_chapter1_model_name("deepseek-chat", "deepseek-chat") == "deepseek-chat"


def test_extract_json_payload_handles_leading_braces_noise():
    raw = (
        "模型说明里可能出现占位符 {}，请忽略。\n"
        '{"section":{"key":"background_overview","title":"背景与概述","paragraphs":["段落一","段落二"]}}'
        "\n以上为正文。"
    )
    parsed = _extract_json_payload(raw)
    assert parsed["section"]["key"] == "background_overview"
    assert parsed["section"]["paragraphs"] == ["段落一", "段落二"]


def test_generate_other_chapter1_caps_request_budget(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def complete(self, messages, **kwargs):
            self.calls.append({"messages": messages, "kwargs": kwargs})
            raise AssertionError("test setup should replace complete() before invocation")

    class FakeOrchestrator:
        def __init__(self, client):
            self.client = client

        def is_available(self):
            return True

    def _payload():
        sections = []
        for spec in other_proof.CHAPTER1_SECTION_SPECS:
            key = spec["key"]
            if key == "industry_supply_chain":
                paragraphs = [
                    "行业供应链围绕上游部件、中游集成、下游交付和长期协同形成完整体系。",
                    *[
                        f"上游供应链段落 {idx} 聚焦芯片、光学模组、传感器和核心零部件的稳定供应。"
                        for idx in range(4)
                    ],
                    *[
                        f"中游制造与集成段落 {idx} 聚焦设计、组装、系统集成和质量控制。"
                        for idx in range(3)
                    ],
                    *[
                        f"下游应用与分销段落 {idx} 聚焦应用行业、客户结构、渠道分销和交付服务。"
                        for idx in range(3)
                    ],
                    *[
                        f"行业供应链核心特征与挑战段落 {idx} 聚焦协同复杂度、成本压力和质量一致性。"
                        for idx in range(2)
                    ],
                    *[
                        f"行业供应链发展方向段落 {idx} 聚焦模块化设计、标准化接口和生态协同。"
                        for idx in range(5)
                    ],
                ]
            else:
                paragraphs = [
                    f"{key} 段落 {idx} 围绕产品定位、技术特征、应用场景和产业链位置展开，形成完整正文。"
                    for idx in range(spec["slot_count"])
                ]
            sections.append(
                {
                    "key": key,
                    "title": other_proof.CHAPTER1_SPEC_MAP[key]["title"],
                    "paragraphs": paragraphs,
                }
            )
        return json.dumps(
            {
                "sections": sections
            },
            ensure_ascii=False,
        )

    fake_client = FakeClient()

    def fake_from_config(_config):
        return FakeOrchestrator(fake_client)

    monkeypatch.setattr(other_proof.LLMOrchestrator, "from_config", staticmethod(fake_from_config))

    config = other_proof.InferenceConfig(
        llm_timeout_seconds=300,
        llm_max_output_tokens=8192,
    )
    def complete_once(messages, **kwargs):
        fake_client.calls.append({"messages": messages, "kwargs": kwargs})
        return _payload()

    fake_client.complete = complete_once
    result = generate_other_chapter1("高安全性自锁紧型电源连接系统", config, allow_partial=False)

    assert len(result["sections"]) == 9
    assert len(fake_client.calls) == 5
    kwargs = fake_client.calls[0]["kwargs"]
    assert kwargs["timeout_seconds"] == 0
    assert kwargs["max_output_tokens"] == 5200
    assert kwargs["retry_max_attempts"] == 1
    assert "section_key" not in kwargs


def test_generate_other_chapter1_wraps_transport_errors_as_timeout(monkeypatch):
    class FakeClient:
        def complete(self, *_args, **_kwargs):
            raise httpx.RemoteProtocolError("peer closed connection")

    class FakeOrchestrator:
        def __init__(self):
            self.client = FakeClient()

        def is_available(self):
            return True

    monkeypatch.setattr(
        other_proof.LLMOrchestrator,
        "from_config",
        staticmethod(lambda _config: FakeOrchestrator()),
    )

    try:
        generate_other_chapter1("高安全性自锁紧型电源连接系统", other_proof.InferenceConfig())
    except other_proof.OtherProofTimeoutError as exc:
        assert "暂未生成完成" in str(exc)
    else:
        raise AssertionError("expected OtherProofTimeoutError")


def test_generate_other_chapter1_allow_partial_writes_placeholders(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def complete(self, messages, **kwargs):
            self.calls.append({"messages": messages, "kwargs": kwargs})
            return json.dumps(
                {
                    "sections": [
                        {
                            "key": "industry_trends",
                            "title": other_proof.CHAPTER1_SPEC_MAP["industry_trends"]["title"],
                            "paragraphs": ["行业发展趋势段落 1", "行业发展趋势段落 2"],
                        }
                    ]
                },
                ensure_ascii=False,
            )

    class FakeOrchestrator:
        def __init__(self, client):
            self.client = client

        def is_available(self):
            return True

    fake_client = FakeClient()
    monkeypatch.setattr(
        other_proof.LLMOrchestrator,
        "from_config",
        staticmethod(lambda _config: FakeOrchestrator(fake_client)),
    )

    result = generate_other_chapter1("高安全性自锁紧型电源连接系统", other_proof.InferenceConfig(), allow_partial=True)

    assert len(result["sections"]) == 9
    assert any("背景与概述" in item and "未生成成功" in item for item in result["warnings"])
    assert any("定义" in item and "未生成成功" in item for item in result["warnings"])
    background = next(item for item in result["sections"] if item["key"] == "background_overview")
    assert all(p == other_proof.PLACEHOLDER_TEXT for p in background["paragraphs"])


def test_generate_other_chapter1_allow_partial_all_failed_raises_timeout(monkeypatch):
    class FakeClient:
        def complete(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("timed out")

    class FakeOrchestrator:
        def __init__(self):
            self.client = FakeClient()

        def is_available(self):
            return True

    monkeypatch.setattr(
        other_proof.LLMOrchestrator,
        "from_config",
        staticmethod(lambda _config: FakeOrchestrator()),
    )

    with pytest.raises(other_proof.OtherProofTimeoutError) as exc_info:
        generate_other_chapter1("高安全性自锁紧型电源连接系统", other_proof.InferenceConfig(), allow_partial=True)
    assert "暂未生成完成" in str(exc_info.value)


def test_generate_other_chapter1_repairs_incomplete_section_individually(monkeypatch):
    def long_topic(prefix: str, count: int) -> str:
        return "".join(f"{prefix}第{idx}句围绕供应链协同、质量控制和交付稳定性展开。" for idx in range(1, count + 1))

    def batch_payload() -> str:
        sections = []
        for spec in other_proof.CHAPTER1_SECTION_SPECS:
            if spec["key"] == "industry_supply_chain":
                paragraphs = ["行业供应链围绕关键零部件、整机集成和场景交付形成协同体系。"]
            else:
                paragraphs = [
                    f"{spec['title']}第{idx}段围绕产品定位、技术特征、应用场景和产业链位置展开，形成完整正文。"
                    for idx in range(spec["slot_count"])
                ]
            sections.append({"key": spec["key"], "title": spec["title"], "paragraphs": paragraphs})
        return json.dumps({"sections": sections}, ensure_ascii=False)

    repaired_supply_chain = {
        "section": {
            "key": "industry_supply_chain",
            "title": other_proof.CHAPTER1_SPEC_MAP["industry_supply_chain"]["title"],
            "paragraphs": [
                "行业供应链围绕上游部件、中游集成、下游交付和长期服务形成协同体系。",
                long_topic("上游供应链", 4),
                long_topic("中游制造与集成", 3),
                long_topic("下游应用与分销", 3),
                long_topic("行业供应链核心特征与挑战", 2),
                long_topic("行业供应链发展方向", 5),
            ],
        }
    }

    class FakeClient:
        def __init__(self):
            self.prompts = []

        def complete(self, messages, **_kwargs):
            prompt = messages[-1]["content"]
            self.prompts.append(prompt)
            if "以下章节在第一轮生成中缺失" in prompt:
                return json.dumps({"sections": []}, ensure_ascii=False)
            if "请仅生成第一章中的一个小节：行业供应链" in prompt:
                return json.dumps(repaired_supply_chain, ensure_ascii=False)
            return batch_payload()

    class FakeOrchestrator:
        def __init__(self, client):
            self.client = client

        def is_available(self):
            return True

    fake_client = FakeClient()
    monkeypatch.setattr(
        other_proof.LLMOrchestrator,
        "from_config",
        staticmethod(lambda _config: FakeOrchestrator(fake_client)),
    )

    result = generate_other_chapter1("AI+XR穿戴设备", other_proof.InferenceConfig(), allow_partial=False)

    supply_chain = next(item for item in result["sections"] if item["key"] == "industry_supply_chain")
    assert len(supply_chain["paragraphs"]) == other_proof.CHAPTER1_SPEC_MAP["industry_supply_chain"]["slot_count"]
    assert all(paragraph != other_proof.PLACEHOLDER_TEXT for paragraph in supply_chain["paragraphs"])
    assert any("第一章已逐节补全：行业供应链" in warning for warning in result["warnings"])
    assert any("请仅生成第一章中的一个小节：行业供应链" in prompt for prompt in fake_client.prompts)


def test_generate_other_chapter1_accepts_plain_text_and_maps_sections(monkeypatch):
    plain_text = """
一、背景与概述
高安全性自锁紧型电源连接系统处于高可靠连接赛道，核心价值是提升复杂工况下供电连接稳定性。

二、定义
该产品定义为面向高可靠连接场景的关键部件体系，强调稳定传输与安全冗余能力。

九、行业供应链
行业需求由新能源、电力装备、轨道交通等场景共同拉动，市场关注点持续转向安全冗余和维护便利性。
""".strip()

    class FakeClient:
        def complete(self, *_args, **_kwargs):
            return plain_text

    class FakeOrchestrator:
        def __init__(self):
            self.client = FakeClient()

        def is_available(self):
            return True

    monkeypatch.setattr(
        other_proof.LLMOrchestrator,
        "from_config",
        staticmethod(lambda _config: FakeOrchestrator()),
    )

    result = generate_other_chapter1("高安全性自锁紧型电源连接系统", other_proof.InferenceConfig(), allow_partial=True)
    assert len(result["sections"]) == 9
    assert any("非 JSON 文本" in item for item in result["warnings"])
    section_map = {item["key"]: item for item in result["sections"]}
    assert "关键部件体系" in section_map["definition"]["paragraphs"][0]
    assert any("行业需求" in p for p in section_map["industry_supply_chain"]["paragraphs"])


def test_extract_sections_from_json_like_text_does_not_leak_title_tokens():
    raw = (
        '"key":"industry_environment","title":"行业发展环境","paragraphs":["段落A"],'
        '"key":"industry_trends","title":"行业发展趋势","paragraphs":["段落B"]'
    )
    sections = other_proof._extract_sections_from_json_like_text(raw)
    env = next(item for item in sections if item["key"] == "industry_environment")
    trends = next(item for item in sections if item["key"] == "industry_trends")
    assert env["paragraphs"] == ["段落A"]
    assert trends["paragraphs"] == ["段落B"]


def test_generate_other_chapter1_section_returns_requested_section(monkeypatch):
    requested_key = "definition"

    class FakeClient:
        def complete(self, *_args, **_kwargs):
            return json.dumps(
                {
                    "section": {
                        "key": requested_key,
                        "title": other_proof.CHAPTER1_SPEC_MAP[requested_key]["title"],
                        "paragraphs": [
                            "该产品是面向高可靠连接场景的关键部件体系，强调稳定传输和安全冗余。",
                            "其定义边界聚焦于连接功能与防护能力，不延展到无关业务领域。",
                        ],
                    }
                },
                ensure_ascii=False,
            )

    class FakeOrchestrator:
        def __init__(self):
            self.client = FakeClient()

        def is_available(self):
            return True

    monkeypatch.setattr(
        other_proof.LLMOrchestrator,
        "from_config",
        staticmethod(lambda _config: FakeOrchestrator()),
    )

    result = generate_other_chapter1_section(
        "高安全性自锁紧型电源连接系统",
        requested_key,
        [],
        other_proof.InferenceConfig(),
    )

    section = result["section"]
    assert section["key"] == requested_key
    assert section["title"] == other_proof.CHAPTER1_SPEC_MAP[requested_key]["title"]
    assert any(p != other_proof.PLACEHOLDER_TEXT for p in section["paragraphs"])
    assert section["paragraphs"][0].startswith("该产品是面向高可靠连接场景")


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
    assert any(str(item).startswith("多级防护逻辑：在碰撞风险逐步提升时") for item in target["paragraphs"])
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
    assert target["paragraphs"][0] == "上游材料环节以铜材和工程塑料为主，供应稳定性直接影响交付周期。"
    assert len(target["paragraphs"]) == other_proof.CHAPTER1_SPEC_MAP["industry_supply_chain"]["slot_count"]
    assert all(paragraph == other_proof.PLACEHOLDER_TEXT for paragraph in target["paragraphs"][1:])


def test_normalize_chapter1_sections_cleans_leading_punctuation_and_struct_tokens():
    sections, _warnings = normalize_chapter1_sections(
        [
            {
                "key": "definition",
                "title": "定义",
                "paragraphs": [
                    ",“title 定义 paragraphs: 该产品是面向空间计算场景的智能穿戴终端。”",
                    "。paragraphs：其核心价值在于把感知、显示和智能交互能力集成到可佩戴设备中。",
                    "sections",
                ],
            }
        ]
    )
    target = next(item for item in sections if item["key"] == "definition")
    assert target["paragraphs"][0].startswith("定义：该产品是面向空间计算场景")
    assert target["paragraphs"][1].startswith("其核心价值在于")
    assert all(not paragraph.startswith(("，", ",", "。")) for paragraph in target["paragraphs"])


def test_normalize_chapter1_sections_supply_chain_remaps_body_to_matching_subtitle():
    sections, _warnings = normalize_chapter1_sections(
        [
            {
                "key": "industry_supply_chain",
                "title": "行业供应链",
                "paragraphs": [
                    "该产品供应链围绕核心零部件、整机集成和场景交付形成协同体系。",
                    "下游应用与分销环节主要面向工业运维、医疗培训和教育模拟等客户，渠道需要承担体验、交付和售后服务。",
                    "上游供应链依赖芯片、光学模组、传感器和轻量化结构件等关键部件，供应稳定性直接影响产品迭代。",
                    "中游制造与集成环节需要把算法、光学、结构和操作系统进行协同设计，质量控制贯穿样机验证和量产爬坡。",
                    "行业供应链的核心特征与面临的挑战在于跨学科协同复杂，关键零部件认证周期长，成本与一致性管理压力较高。",
                    "行业供应链的发展方向将围绕模块化设计、标准化接口和生态协同展开，以提升交付效率和供应韧性。",
                ],
            }
        ]
    )
    target = next(item for item in sections if item["key"] == "industry_supply_chain")
    assert "依赖芯片" in target["paragraphs"][1]
    assert "算法、光学" in target["paragraphs"][5]
    assert "工业运维" in target["paragraphs"][8]
    assert "跨学科协同复杂" in target["paragraphs"][11]
    assert "模块化设计" in target["paragraphs"][13]


def test_supply_chain_batch_prompt_uses_six_complete_topic_paragraphs():
    prompt = other_proof._build_chapter1_batch_prompt(
        product_name="AI+XR穿戴设备",
        batch_specs=[other_proof.CHAPTER1_SPEC_MAP["industry_supply_chain"]],
        generated_sections=[],
    )

    assert "输出 6 段完整正文" in prompt
    assert "第 2 段只写上游供应链" in prompt
    assert "第 3 段只写中游制造与集成" in prompt
    assert "第 4 段只写下游应用与分销" in prompt
    assert "不要在正文中写（一）（二）或小标题" in prompt


def test_remove_chapter1_when_user_skips_does_not_leave_placeholders_or_old_body():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def p(text: str) -> str:
        return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"

    root = ET.fromstring(
        f"""
        <w:document xmlns:w="{ns}">
          <w:body>
            {p("目录")}
            {p("第一章 高安全性自锁紧型电源连接系统产品概况8")}
            {p("一、背景与概述8")}
            {p("五、行业供应链25")}
            {p("第二章 主导产品市场销售规模情况31")}
            {p("摘 要")}
            {p("第一章 AI+XR穿戴设备产品概况")}
            {p("一、背景与概述")}
            {p("该部分生成失败，请人工补充。")}
            {p("第二章 主导产品市场销售规模情况")}
            {p("第二章正文")}
          </w:body>
        </w:document>
        """
    )

    other_proof._remove_chapter1_body_and_toc(root)

    texts = [_text for _text in (_get_text(p) for p in root.findall(".//w:p", {"w": ns})) if _text]
    assert "第一章 AI+XR穿戴设备产品概况" not in texts
    assert "第一章 高安全性自锁紧型电源连接系统产品概况8" not in texts
    assert "该部分生成失败，请人工补充。" not in texts
    assert "第二章 主导产品市场销售规模情况31" in texts
    assert "第二章 主导产品市场销售规模情况" in texts


def test_rewrite_other_toc_titles_replaces_template_product_and_company_names():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def p(text: str) -> str:
        return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"

    root = ET.fromstring(
        f"""
        <w:document xmlns:w="{ns}">
          <w:body>
            {p("目录")}
            {p("第一章 高安全性自锁紧型电源连接系统产品概况8")}
            {p("第二章 主导产品市场销售规模情况31")}
            {p("一、全球电源连接器市场情况分析31")}
            {p("第三章 主导产品头部企业分析34")}
            {p("一、主导产品企业分析——旧公司34")}
            {p("第四章 旧公司旧产品市场占有率证明37")}
            {p("第五章 数据来源39")}
            {p("摘 要")}
          </w:body>
        </w:document>
        """
    )

    other_proof._rewrite_other_toc_titles(
        root=root,
        product_name="AI+XR穿戴设备",
        chapter2_layers=[{"name": "全球AI+XR穿戴设备"}],
        sorted_rows=[{"display_name": "深圳市亿境虚拟现实技术有限公司"}],
        self_company_name="深圳市亿境虚拟现实技术有限公司",
    )

    texts = [_get_text(p) for p in root.findall(".//w:p", {"w": ns})]
    assert "第一章 AI+XR穿戴设备产品概况8" in texts
    assert "一、全球AI+XR穿戴设备市场情况分析31" in texts
    assert "一、主导产品企业分析——深圳市亿境虚拟现实技术有限公司34" in texts
    assert "第四章 深圳市亿境虚拟现实技术有限公司AI+XR穿戴设备市场占有率证明37" in texts


def _get_text(paragraph: ET.Element) -> str:
    return "".join(node.text or "" for node in paragraph.findall(".//w:t", {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}))


def test_other_proof_body_plain_paragraph_justification_only_for_body_text():
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
                <w:t>图表1：示例标题</w:t>
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

    other_proof._apply_body_plain_paragraph_justification(root)

    paragraphs = root.findall(f".//{{{ns}}}p")
    body_jc = paragraphs[0].find(f"./{{{ns}}}pPr/{{{ns}}}jc")
    heading_jc = paragraphs[1].find(f"./{{{ns}}}pPr/{{{ns}}}jc")
    table_jc = paragraphs[2].find(f"./{{{ns}}}pPr/{{{ns}}}jc")

    assert body_jc is not None
    assert body_jc.get(f"{{{ns}}}val") == "both"
    assert heading_jc is None
    assert table_jc is None


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

    assert len(result) == 4
    assert result[0] == other_proof.PLACEHOLDER_TEXT
    assert "（二）" not in result[1]
    assert "（三）" not in result[2]
    assert "A" in result[1]
    assert "B" in result[2]


def test_normalize_industry_environment_strips_synthetic_topic_headings():
    sections, _warnings = normalize_chapter1_sections(
        [
            {
                "key": "industry_environment",
                "title": "行业发展环境",
                "paragraphs": [
                    "（一）行业发展环境 基于产品特性，行业发展受到政策与技术双重驱动。",
                    "1. 政策环境 政策端持续强化安全与质量约束。",
                    "2. 经济环境 经济端投资结构向高端制造倾斜。",
                    "3. 技术环境 技术端关键部件持续迭代升级。",
                    "4. 社会环境 社会端对高可靠供电安全要求持续提升。",
                ],
            }
        ]
    )
    target = next(item for item in sections if item["key"] == "industry_environment")
    assert all(not paragraph.startswith("（一）行业发展环境") for paragraph in target["paragraphs"])
    assert all(not paragraph.startswith("1. 政策环境") for paragraph in target["paragraphs"])
    assert any("政策端持续强化安全与质量约束" in paragraph for paragraph in target["paragraphs"])


def test_mark_footer_page_fields_dirty_sets_begin_fields_dirty():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    footer_xml = f"""
    <w:ftr xmlns:w="{ns}">
      <w:p>
        <w:r><w:fldChar w:fldCharType="begin"/></w:r>
        <w:r><w:instrText> PAGE  \\* MERGEFORMAT </w:instrText></w:r>
        <w:r><w:fldChar w:fldCharType="separate"/></w:r>
        <w:r><w:t>1</w:t></w:r>
        <w:r><w:fldChar w:fldCharType="end"/></w:r>
      </w:p>
    </w:ftr>
    """.strip().encode("utf-8")
    file_map = {"word/footer1.xml": footer_xml}
    other_proof._mark_footer_page_fields_dirty(file_map)
    root = ET.fromstring(file_map["word/footer1.xml"])
    begin = root.find(f".//{{{ns}}}fldChar[@{{{ns}}}fldCharType='begin']")
    assert begin is not None
    assert begin.get(f"{{{ns}}}dirty") == "true"


def test_split_paragraph_for_template_does_not_split_on_comma():
    text = "该段只有逗号，没有句号，所以不应在逗号处拆分，并保持同一段落结构"
    left, right = other_proof._split_paragraph_for_template(text)
    assert left == text
    assert right == ""


def test_remove_section_page_number_restart_removes_pg_num_type():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    root = ET.fromstring(
        f"""
        <w:document xmlns:w="{ns}">
          <w:body>
            <w:p><w:r><w:t>正文</w:t></w:r></w:p>
            <w:sectPr><w:pgNumType w:start="1"/></w:sectPr>
          </w:body>
        </w:document>
        """
    )
    other_proof._remove_section_page_number_restart(root)
    assert root.find(f".//{{{ns}}}pgNumType") is None


def test_set_signature_block_right_alignment_sets_signature_to_right():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    root = ET.fromstring(
        f"""
        <w:document xmlns:w="{ns}">
          <w:body>
            <w:p><w:r><w:t>北京算路科技有限公司（盖章）</w:t></w:r></w:p>
            <w:p><w:r><w:t>2026 年 4 月 16 日</w:t></w:r></w:p>
          </w:body>
        </w:document>
        """
    )
    other_proof._set_signature_block_right_alignment(root)
    paragraphs = root.findall(f".//{{{ns}}}p")
    for paragraph in paragraphs:
        jc = paragraph.find(f"./{{{ns}}}pPr/{{{ns}}}jc")
        assert jc is not None
        assert jc.get(f"{{{ns}}}val") == "right"


def test_enable_word_update_fields_on_open_sets_settings_flag():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    settings_xml = f"""
    <w:settings xmlns:w="{ns}">
      <w:zoom w:percent="120"/>
    </w:settings>
    """.strip().encode("utf-8")
    file_map = {"word/settings.xml": settings_xml}
    other_proof._enable_word_update_fields_on_open(file_map)
    root = ET.fromstring(file_map["word/settings.xml"])
    node = root.find(f"./{{{ns}}}updateFields")
    assert node is not None
    assert node.get(f"{{{ns}}}val") == "true"


def test_compress_chapter1_visual_paragraphs_compresses_environment_and_trends_only():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    root = ET.fromstring(f'<w:document xmlns:w="{ns}"><w:body/></w:document>')
    body = root.find(f".//{{{ns}}}body")
    assert body is not None
    field_paragraphs = []
    total_slots = 20 + sum(item["slot_count"] for item in other_proof.CHAPTER1_SECTION_SPECS)
    for idx in range(total_slots):
        p = ET.Element(f"{{{ns}}}p")
        r = ET.SubElement(p, f"{{{ns}}}r")
        t = ET.SubElement(r, f"{{{ns}}}t")
        t.text = f"slot-{idx}"
        body.append(p)
        field_paragraphs.append(p)

    cursor = 20
    for spec in other_proof.CHAPTER1_SECTION_SPECS:
        if spec["key"] in {"industry_environment", "industry_trends", "industry_supply_chain"}:
            for i in range(spec["slot_count"]):
                other_proof._set_paragraph_text(field_paragraphs[cursor + i], f"{spec['key']}-{i}")
        cursor += spec["slot_count"]

    other_proof._compress_chapter1_visual_paragraphs(root, field_paragraphs)
    body_texts = []
    for paragraph in body.findall(f"./{{{ns}}}p"):
        text = "".join(node.text or "" for node in paragraph.findall(f".//{{{ns}}}t")).strip()
        if text:
            body_texts.append(text)

    env_visible = [text for text in body_texts if text.startswith("industry_environment-")]
    trend_visible = [text for text in body_texts if text.startswith("industry_trends-")]
    supply_visible = [text for text in body_texts if text.startswith("industry_supply_chain-")]

    assert len(env_visible) <= other_proof.CHAPTER1_VISIBLE_PARAGRAPH_COUNTS["industry_environment"]
    assert len(trend_visible) <= other_proof.CHAPTER1_VISIBLE_PARAGRAPH_COUNTS["industry_trends"]
    assert len(supply_visible) == other_proof.CHAPTER1_SPEC_MAP["industry_supply_chain"]["slot_count"]


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


def test_rewrite_summary_market_research_phrase_uses_product_name():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    root = ET.fromstring(
        f"""
        <w:document xmlns:w="{ns}">
          <w:body>
            <w:p><w:r><w:t>深入了解行业情况，对“旧产品名称”细分市场进行拆分和规模测算。</w:t></w:r></w:p>
          </w:body>
        </w:document>
        """
    )
    _rewrite_summary_market_research_phrase(root, "新主导产品")
    rendered = "".join(node.text or "" for node in root.findall(f".//{{{ns}}}t"))
    assert "对“新主导产品”细分市场进行拆分和规模测算" in rendered


def test_highlight_self_row_in_comparison_table_only_bolds_self_row():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    root = ET.fromstring(
        f"""
        <w:document xmlns:w="{ns}">
          <w:body>
            <w:tbl>
              <w:tr><w:tc><w:p><w:r><w:t>头1</w:t></w:r></w:p></w:tc></w:tr>
              <w:tr><w:tc><w:p><w:r><w:t>头2</w:t></w:r></w:p></w:tc></w:tr>
              <w:tr><w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>企业A</w:t></w:r></w:p></w:tc></w:tr>
              <w:tr><w:tc><w:p><w:r><w:t>我司</w:t></w:r></w:p></w:tc></w:tr>
              <w:tr><w:tc><w:p><w:r><w:t>企业C</w:t></w:r></w:p></w:tc></w:tr>
            </w:tbl>
          </w:body>
        </w:document>
        """
    )
    body = root.find(f".//{{{ns}}}body")
    assert body is not None
    _highlight_self_row_in_comparison_table(body=body, table_index=0, self_company_name="我司")

    rows = root.findall(f".//{{{ns}}}tbl/{{{ns}}}tr")
    data_rows = rows[2:]
    # 企业A 行应去掉加粗
    a_bold = data_rows[0].find(f".//{{{ns}}}rPr/{{{ns}}}b")
    assert a_bold is None
    # 我司行应加粗
    self_bold = data_rows[1].find(f".//{{{ns}}}rPr/{{{ns}}}b")
    assert self_bold is not None
    # 企业C 不加粗
    c_bold = data_rows[2].find(f".//{{{ns}}}rPr/{{{ns}}}b")
    assert c_bold is None
