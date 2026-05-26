from __future__ import annotations

import io
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

import app as app_module
import chart_docx
import other_proof


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}


def _build_self_payload(source_count: int = 2) -> dict:
    sources = []
    for i in range(source_count):
        base = 440 + i * 30
        sources.append(
            {
                "name": f"来源{i + 1}",
                "url": f"https://example.com/{i + 1}",
                "chart_title": f"图表{i + 1}：2023-2025年示例市场规模",
                "chart_2023": f"{base:.2f}",
                "chart_2024": f"{(base + 18.6):.2f}",
                "chart_2025": f"{(base + 37.2):.2f}",
                "analysis": "测试来源正文",
            }
        )

    return {
        "province": "浙江省",
        "company_name": "浙江达航数据技术有限公司",
        "product_name": "示例产品",
        "product_code": "P-001",
        "year": "2026",
        "month": "3",
        "day": "22",
        "intro": "企业介绍：示例\\n\\n产品介绍：示例",
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
        "sources": sources,
        "competitors": [],
    }


def _build_complete_chapter1_sections() -> list[dict]:
    sections = []
    for spec in other_proof.CHAPTER1_SECTION_SPECS:
        if spec["key"] == "industry_supply_chain":
            paragraphs = ["行业供应链围绕核心零部件、整机集成、渠道交付和生态协同形成完整体系。"]
            paragraphs += ["上游供应链聚焦芯片、光学模组、传感器、结构件和能源部件的稳定供给。"]
            paragraphs += ["中游制造与集成聚焦工业设计、软硬件协同、整机组装、测试验证和质量控制。"]
            paragraphs += ["下游应用与分销聚焦企业级场景、消费场景、渠道交付和售后服务。"]
            paragraphs += ["行业供应链的核心特征与面临的挑战体现在跨学科协同、质量一致性和成本控制。"]
            paragraphs += ["行业供应链的发展方向聚焦模块化设计、标准化接口、生态协同和区域化交付。"]
        else:
            paragraphs = [
                f"{spec['title']}正文围绕示例产品的产品定位、技术特征、应用场景和产业链位置展开，形成完整咨询报告段落。"
                for _ in range(spec["slot_count"])
            ]
        sections.append({"key": spec["key"], "title": spec["title"], "paragraphs": paragraphs})
    return sections


def _build_other_payload_three_layers() -> dict:
    return {
        "template_type": "other",
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
                "chart_2023": "240.1",
                "chart_2024": "268.9",
                "chart_2025": "296.3",
                "analysis": "这一层进一步缩小到目标产品的市场口径。",
            },
            {
                "name": "来源3",
                "url": "https://example.com/market-3",
                "chart_title": "图表3",
                "chart_2023": "120.4",
                "chart_2024": "138.8",
                "chart_2025": "159.7",
                "analysis": "这一层再次收窄到可核验口径。",
            },
        ],
        "chapter2_layers": [
            {"name": "连接器市场"},
            {"name": "高安全电源连接系统市场"},
            {"name": "高安全自锁紧型电源连接系统市场"},
        ],
        "chapter1_sections": _build_complete_chapter1_sections(),
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
        "competitors": [],
    }


def _find_neighbor_text(children: list[ET.Element], index: int, step: int) -> str:
    cursor = index + step
    while 0 <= cursor < len(children):
        node = children[cursor]
        if node.tag == f"{{{NS['w']}}}p":
            text = "".join(t.text or "" for t in node.findall(".//w:t", NS)).strip()
            if text:
                return text
        cursor += step
    return ""


def _extract_chart_slots(docx_path: Path) -> list[dict[str, str | int]]:
    with zipfile.ZipFile(docx_path, "r") as archive:
        rel_root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        rid_to_target = {
            rel.attrib.get("Id", ""): rel.attrib.get("Target", "")
            for rel in rel_root.findall("rel:Relationship", NS)
        }
        root = ET.fromstring(archive.read("word/document.xml"))

    body = root.find(".//w:body", NS)
    assert body is not None
    children = list(body)

    slots: list[dict[str, str | int]] = []
    for idx, child in enumerate(children):
        if child.tag != f"{{{NS['w']}}}p":
            continue
        drawing = child.find(".//w:drawing", NS)
        if drawing is None:
            continue

        prev_text = _find_neighbor_text(children, idx, -1)
        next_text = _find_neighbor_text(children, idx, +1)
        if not prev_text.startswith("图表") or not next_text.startswith("数据来源"):
            continue

        blip = drawing.find(".//a:blip", NS)
        rid = blip.attrib.get(f"{{{NS['r']}}}embed", "") if blip is not None else ""
        extent = drawing.find(".//wp:extent", NS)
        cx = extent.attrib.get("cx", "") if extent is not None else ""
        cy = extent.attrib.get("cy", "") if extent is not None else ""
        slots.append(
            {
                "index": idx,
                "cx": cx,
                "cy": cy,
                "rid": rid,
                "target": rid_to_target.get(rid, ""),
            }
        )
    return slots


def _extract_paragraph_texts(docx_path: Path) -> list[str]:
    with zipfile.ZipFile(docx_path, "r") as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    texts: list[str] = []
    for p in root.findall(".//w:p", NS):
        text = "".join(t.text or "" for t in p.findall(".//w:t", NS)).strip()
        if text:
            texts.append(text)
    return texts


def _extract_self_sales_table_row_count(docx_path: Path) -> int:
    with zipfile.ZipFile(docx_path, "r") as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(".//w:body", NS)
    assert body is not None
    for table in body.findall("./w:tbl", NS):
        rows = table.findall("./w:tr", NS)
        if len(rows) < 3:
            continue
        first_row_text = "".join(t.text or "" for t in rows[0].findall(".//w:t", NS))
        second_row_text = "".join(t.text or "" for t in rows[1].findall(".//w:t", NS))
        if "企业名称" in first_row_text and "2023年" in first_row_text and "销售额（万元）" in second_row_text:
            return len(rows)
    raise AssertionError("self sales table not found")


def test_self_chart_layout_unchanged_when_source_count_matches_template(tmp_path: Path):
    template_path = Path("0315-浙江达航数据技术有限公司-自证-初版.docx")
    baseline_slots = _extract_chart_slots(template_path)
    assert len(baseline_slots) == 2

    output_path = tmp_path / "output.docx"
    payload = _build_self_payload(source_count=2)
    app_module.generate_docx_v4(payload, template_path, output_path)

    generated_slots = _extract_chart_slots(output_path)
    assert len(generated_slots) == 2

    for before, after in zip(baseline_slots, generated_slots):
        assert after["index"] == before["index"]
        assert after["cx"] == before["cx"]
        assert after["cy"] == before["cy"]
        assert str(after["target"]).endswith(".png")


def test_self_chart_slots_expand_with_source_layers(tmp_path: Path):
    template_path = Path("0315-浙江达航数据技术有限公司-自证-初版.docx")
    output_path = tmp_path / "output-expanded.docx"
    payload = _build_self_payload(source_count=5)
    app_module.generate_docx_v4(payload, template_path, output_path)

    slots = _extract_chart_slots(output_path)
    assert len(slots) == 5

    indices = [int(item["index"]) for item in slots]
    assert indices == sorted(indices)

    sizes = {(str(item["cx"]), str(item["cy"])) for item in slots}
    assert len(sizes) == 1

    targets = [str(item["target"]) for item in slots]
    assert len(set(targets)) == 5
    assert all(target.endswith(".png") for target in targets)


def test_self_chart_reference_numbering_and_sales_table_rows_follow_source_and_company_counts(tmp_path: Path):
    template_path = Path("0315-浙江达航数据技术有限公司-自证-初版.docx")
    output_path = tmp_path / "self-numbering.docx"
    payload = _build_self_payload(source_count=5)
    payload["competitors"] = [
        {"name": "竞品A", "p23": "5%", "p24": "5.5%", "p25": "6%"},
    ]
    app_module.generate_docx_v4(payload, template_path, output_path)

    texts = _extract_paragraph_texts(output_path)
    joined = "\n".join(texts)
    assert "市场规模以及各企业2023年-2025年产品销售额如图表6所示。2023-2025 年浙江达航数据技术有限公司的产品市场规模占有率如图表7所示。" in joined
    assert "图表 6：2023-2025年主要企业全国销售额情况" in joined
    assert "图表 7：2023-2025 年浙江达航数据技术有限公司占有率情况" in joined

    row_count = _extract_self_sales_table_row_count(output_path)
    # 2 行表头 + 我司 1 行 + 竞品 1 行
    assert row_count == 4


def test_other_chart_layout_unchanged_when_layer_count_matches_template(tmp_path: Path):
    template_path = Path("0323-高安全性自锁紧型电源连接系统市场占有率证明报告-初版.docx")
    baseline_slots = _extract_chart_slots(template_path)
    assert len(baseline_slots) == 3

    payload = _build_other_payload_three_layers()
    output_path = tmp_path / "other-output.docx"
    other_proof.generate_other_docx(payload, template_path, output_path)

    generated_slots = _extract_chart_slots(output_path)
    assert len(generated_slots) == 3
    baseline_gaps = [int(baseline_slots[i + 1]["index"]) - int(baseline_slots[i]["index"]) for i in range(2)]
    generated_gaps = [int(generated_slots[i + 1]["index"]) - int(generated_slots[i]["index"]) for i in range(2)]
    assert generated_gaps == baseline_gaps

    for before, after in zip(baseline_slots, generated_slots):
        assert after["cx"] == before["cx"]
        assert after["cy"] == before["cy"]
        assert str(after["target"]).endswith(".png")


def test_other_docx_keeps_chapter1_with_placeholders_when_chapter1_empty(tmp_path: Path):
    template_path = Path("0323-高安全性自锁紧型电源连接系统市场占有率证明报告-初版.docx")
    payload = _build_other_payload_three_layers()
    payload["chapter1_sections"] = []
    payload["skip_chapter1"] = True
    output_path = tmp_path / "other-placeholder-chapter1.docx"

    warnings = other_proof.generate_other_docx(payload, template_path, output_path)

    texts = _extract_paragraph_texts(output_path)
    assert any(text.startswith("第一章 ") and "产品概况" in text for text in texts)
    assert other_proof.PLACEHOLDER_TEXT in texts
    assert any("跳过第一章标记" in item for item in warnings)
    assert any("第一章存在未完成内容" in item for item in warnings)


def test_chart_axis_uses_integer_hundreds_for_large_values():
    low, high, step = chart_docx._compute_y_axis([444.48, 463.15, 482.6])
    assert low == 0.0
    assert step == 100.0
    assert high == 500.0
    assert int(high / step) + 1 <= 10


def test_chart_axis_uses_small_integer_step_for_small_values():
    low, high, step = chart_docx._compute_y_axis([18, 23, 21])
    assert low == 0.0
    assert step == 5.0
    assert high == 25.0
    assert int(high / step) + 1 <= 10


def test_chart_axis_uses_step_two_for_decimal_values_near_ten():
    low, high, step = chart_docx._compute_y_axis([8.42, 9.32, 10.32])
    assert low == 0.0
    assert step == 2.0
    assert high == 12.0
    assert int(high / step) + 1 <= 10


def test_chart_axis_uses_step_fifty_for_large_spread_values():
    low, high, step = chart_docx._compute_y_axis([12, 197, 50])
    assert low == 0.0
    assert step == 50.0
    assert high == 200.0
    assert int(high / step) + 1 <= 10


def test_rendered_chart_background_is_white_and_has_expected_bar_color():
    try:
        from PIL import Image
    except ImportError:
        raise AssertionError("Pillow is required for chart rendering test")

    image_bytes = chart_docx.render_market_chart_png(
        chart_docx.ChartSeries(
            values=(12.0, 197.0, 50.0),
            labels=("12", "197", "50"),
        )
    )
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    # Ensure background remains white.
    assert image.getpixel((0, 0)) == (255, 255, 255)
    assert image.getpixel((40, 40)) == (255, 255, 255)

    # Ensure at least one pixel uses the configured bar color.
    bar_color = (29, 100, 133)  # #1D6485
    assert bar_color in image.getdata()


def test_rendered_chart_uses_template_like_bar_width_spacing_and_value_scale():
    try:
        from PIL import Image
    except ImportError:
        raise AssertionError("Pillow is required for chart rendering test")

    values = (18.3, 20.0, 22.83)
    image_bytes = chart_docx.render_market_chart_png(
        chart_docx.ChartSeries(
            values=values,
            labels=("18.3", "20", "22.83"),
        ),
        canvas_size=(1600, 980),
    )
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    bar_color = (29, 100, 133)

    columns_with_bar = [
        x
        for x in range(image.width)
        if any(image.getpixel((x, y)) == bar_color for y in range(image.height))
    ]
    runs: list[tuple[int, int]] = []
    run_start = columns_with_bar[0]
    previous = columns_with_bar[0]
    for x in columns_with_bar[1:]:
        if x == previous + 1:
            previous = x
            continue
        runs.append((run_start, previous))
        run_start = previous = x
    runs.append((run_start, previous))

    assert len(runs) == 3
    widths = [end - start + 1 for start, end in runs]
    centers = [(start + end) / 2 for start, end in runs]

    assert all(140 <= width <= 150 for width in widths)
    assert centers == pytest.approx([372.5, 842.5, 1312.5], abs=1.0)

    low, high, _step = chart_docx._compute_y_axis(values)
    for (start, end), value in zip(runs, values):
        bar_pixels = [
            y
            for y in range(image.height)
            for x in range(start, end + 1)
            if image.getpixel((x, y)) == bar_color
        ]
        assert min(bar_pixels) == pytest.approx(
            chart_docx._map_y(value, low, high, 72, 860),
            abs=1,
        )
