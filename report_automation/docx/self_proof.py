from __future__ import annotations

import copy
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import List, Optional

from chart_docx import (
    ChartDataError,
    build_chart_series_from_sources,
    inject_market_charts_into_docx,
)
from other_proof import OtherProofError
from report_automation.settings import SELF_TEMPLATE_PATH


def register_all_namespaces(xml_content):
    import re
    # Extract all xmlns:prefix="uri" from the XML content
    ns_matches = re.findall(r'xmlns:([^=]+)="([^"]+)"', xml_content.decode('utf-8') if isinstance(xml_content, bytes) else xml_content)
    for prefix, uri in ns_matches:
        ET.register_namespace(prefix, uri)

namespaces = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
}
for prefix, uri in namespaces.items():
    ET.register_namespace(prefix, uri)
NS = namespaces
BODY_HEADING_PATTERN = re.compile(
    r"^\s*(图表|数据来源|来源网址|第[一二三四五六七八九十0-9]+章|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\([一二三四五六七八九十]+\)|\d+[\.、])"
)
CN_DIGITS = {0: "零", 1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}

def get_text(node):
    if node is None: return ""
    texts = node.findall('.//w:t', namespaces=NS)
    return "".join(t.text or "" for t in texts)

def is_yellow_run(r):
    highlights = r.findall('.//w:rPr/w:highlight', namespaces=NS)
    for h in highlights:
        if h.get(f"{{{NS['w']}}}val") == 'yellow':
            return True
    return False


def rewrite_header_titles(file_map: dict, company_name: str, product_name: str) -> None:
    product_title = f"{product_name}市场占有率证明报告"
    combined_title = f"{company_name}{product_title}"
    for name, blob in list(file_map.items()):
        if not name.startswith("word/header") or not name.endswith(".xml"):
            continue
        try:
            root = ET.fromstring(blob)
        except ET.ParseError:
            continue
        paragraphs = []
        texts = []
        for p in root.findall(".//w:p", namespaces=NS):
            text = "".join((t.text or "") for t in p.findall(".//w:t", namespaces=NS)).strip()
            if not text:
                continue
            paragraphs.append(p)
            texts.append(text)
        title_index = next((idx for idx, text in enumerate(texts) if "市场占有率证明报告" in text), -1)
        changed = False
        if title_index >= 0:
            def set_paragraph_text(p, value):
                runs = p.findall('./w:r', namespaces=NS)
                if not runs:
                    return
                first = runs[0]
                ts = first.findall('./w:t', namespaces=NS)
                t = ts[0] if ts else ET.SubElement(first, f"{{{NS['w']}}}t")
                for ex in ts[1:]:
                    first.remove(ex)
                t.text = value
                for other in runs[1:]:
                    for ot in other.findall('./w:t', namespaces=NS):
                        ot.text = ""

            if title_index > 0:
                set_paragraph_text(paragraphs[title_index - 1], company_name)
                set_paragraph_text(paragraphs[title_index], product_title)
            else:
                set_paragraph_text(paragraphs[title_index], combined_title)
            changed = True
        if changed:
            file_map[name] = ET.tostring(root, encoding='utf-8', xml_declaration=True)


def rewrite_summary_market_research_phrase(tree: ET.Element, product_name: str) -> None:
    product_name = str(product_name or "").strip()
    if not product_name:
        return
    pattern = re.compile(r"对[“\"].+?[”\"]细分市场进行拆分和规模测算")
    replacement = f"对“{product_name}”细分市场进行拆分和规模测算"
    for p in tree.findall(f".//{{{NS['w']}}}p"):
        text = "".join((t.text or "") for t in p.findall(".//w:t", namespaces=NS))
        if "细分市场进行拆分和规模测算" not in text:
            continue
        updated = pattern.sub(replacement, text)
        if updated == text:
            continue
        runs = p.findall('./w:r', namespaces=NS)
        if not runs:
            continue
        first = runs[0]
        ts = first.findall('./w:t', namespaces=NS)
        t = ts[0] if ts else ET.SubElement(first, f"{{{NS['w']}}}t")
        for ex in ts[1:]:
            first.remove(ex)
        t.text = updated
        for other in runs[1:]:
            for ot in other.findall('./w:t', namespaces=NS):
                ot.text = ""


def _write_run_text_with_breaks(run: ET.Element, text_value: str) -> None:
    text = str(text_value or "")
    for child in list(run):
        if child.tag in {f"{{{NS['w']}}}t", f"{{{NS['w']}}}br"}:
            run.remove(child)
    lines = text.split("\n")
    if not lines:
        lines = [""]
    for idx, line in enumerate(lines):
        t = ET.SubElement(run, f"{{{NS['w']}}}t")
        if line[:1].isspace() or line[-1:].isspace():
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = line
        if idx < len(lines) - 1:
            ET.SubElement(run, f"{{{NS['w']}}}br")


def _run_text_with_breaks(run: Optional[ET.Element]) -> str:
    if run is None:
        return ""
    parts = []
    for child in run.iter():
        if child.tag == f"{{{NS['w']}}}t":
            parts.append(child.text or "")
        elif child.tag == f"{{{NS['w']}}}br":
            parts.append("\n")
    return "".join(parts)


def _paragraph_text_with_breaks(paragraph: Optional[ET.Element]) -> str:
    if paragraph is None:
        return ""
    return "".join(
        _run_text_with_breaks(run)
        for run in paragraph.iter(f"{{{NS['w']}}}r")
    )


def set_paragraph_text(p: ET.Element, value: str) -> None:
    runs = p.findall('./w:r', namespaces=NS)
    if not runs:
        return
    first = runs[0]
    ts = first.findall('./w:t', namespaces=NS)
    for ex in ts[1:]:
        first.remove(ex)
    if not ts:
        ET.SubElement(first, f"{{{NS['w']}}}t")
    _write_run_text_with_breaks(first, str(value))
    for other in runs[1:]:
        for ot in other.findall('./w:t', namespaces=NS):
            ot.text = ""
        for br in other.findall('./w:br', namespaces=NS):
            other.remove(br)


def _normalize_source_values(raw: object) -> List[str]:
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    normalized: List[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        if text in normalized:
            continue
        normalized.append(text)
    return normalized


def _extract_source_names(source: dict) -> List[str]:
    names = _normalize_source_values(source.get("names"))
    if names:
        return names
    return _normalize_source_values(source.get("name"))


def _extract_source_urls(source: dict) -> List[str]:
    urls = _normalize_source_values(source.get("urls"))
    if urls:
        return urls
    return _normalize_source_values(source.get("url"))


def _format_numbered_lines(items: List[str], *, always_number: bool = False) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return f"1. {items[0]}" if always_number else items[0]
    lines = [f"{idx}. {item}" for idx, item in enumerate(items, start=1)]
    return "\n".join(lines)


def _format_labeled_source_text(label: str, items: List[str]) -> str:
    if not items:
        return f"{label}："
    if len(items) == 1:
        return f"{label}：{items[0]}"
    numbered = _format_numbered_lines(items, always_number=True)
    return f"{label}：\n{numbered}"


def _extract_numbered_source_lines(raw_text: str, label: str) -> List[str]:
    text = str(raw_text or "").strip()
    pattern = rf"^\s*{re.escape(label)}\s*[：:]\s*"
    if not re.match(pattern, text):
        raise ValueError(f"来源字段格式异常，未找到“{label}：”")
    body = re.sub(pattern, "", text, count=1).strip()
    if not body:
        raise ValueError(f"来源字段格式异常，“{label}：”后没有内容")

    marker_pattern = re.compile(r"(?<!\d)\d+[\.\、]\s+")
    markers = list(marker_pattern.finditer(body))
    if markers and markers[0].start() == 0:
        values = []
        for idx, marker in enumerate(markers):
            next_start = markers[idx + 1].start() if idx + 1 < len(markers) else len(body)
            value = body[marker.end():next_start].strip()
            if value:
                values.append(value)
        if values:
            return values

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    values = [re.sub(r"^\d+[\.\、]\s*", "", line).strip() for line in lines]
    return [value for value in values if value]


def _extract_market_values_yi(analysis_text: str, *, source_no: int) -> tuple[str, str, str]:
    text = str(analysis_text or "")
    match = re.search(r"分别为[：:]?\s*([^。；;\n]+)", text)
    if not match:
        raise ValueError(f"第 {source_no} 层来源正文缺少“分别为：...亿元、...亿元、...亿元”格式，无法填充图表数据")
    values = re.findall(r"(\d+(?:\.\d+)?)\s*亿元", match.group(1))
    if len(values) < 3:
        raise ValueError(f"第 {source_no} 层来源正文未识别到 2023-2025 三个“亿元”数值")
    return values[0], values[1], values[2]


def _detect_self_target_scope(paragraphs: List[ET.Element]) -> str:
    texts = [get_text(p).strip() for p in paragraphs]
    for text in texts:
        compact = re.sub(r"\s+", "", text)
        if "全球细分市场规模" in compact or "全球市场规模" in compact:
            return "GLOBAL"
        if "全国细分市场规模" in compact or "国内细分市场规模" in compact or "中国细分市场规模" in compact:
            return "CN"
    raise ValueError("未在自证模板中识别到“全国/全球细分市场规模”，无法填充市场范围")


def _previous_non_empty_paragraph_index(paragraphs: List[ET.Element], start_idx: int) -> Optional[int]:
    for idx in range(start_idx, -1, -1):
        if get_text(paragraphs[idx]).strip():
            return idx
    return None


def _first_source_analysis_start(paragraphs: List[ET.Element], title_idx: int) -> int:
    for idx in range(title_idx - 1, -1, -1):
        text = get_text(paragraphs[idx]).strip()
        if "细分市场规模" in text:
            return idx + 1
    raise ValueError("未识别到第一层来源正文起点")


def _is_source_analysis_heading(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return (
        not compact
        or compact.startswith("图表")
        or compact.startswith("数据来源")
        or compact.startswith("来源网址")
        or compact.startswith(("一、", "二、", "三、", "（一）", "（二）", "(一)", "(二)"))
    )


def _extract_self_company_intro(paragraphs: List[ET.Element]) -> str:
    start_idx = None
    end_idx = None
    for idx, paragraph in enumerate(paragraphs):
        text = get_text(paragraph).strip()
        if text == "我司主导产品市场占有率自证说明":
            start_idx = idx
            continue
        if start_idx is not None and text.startswith("一、界定细分市场范围和市场规模"):
            end_idx = idx
            break

    if start_idx is None or end_idx is None or end_idx <= start_idx:
        raise ValueError("未识别到企业介绍的起止位置")

    intro_parts = [
        _paragraph_text_with_breaks(paragraph).strip()
        for paragraph in paragraphs[start_idx + 1:end_idx]
        if _paragraph_text_with_breaks(paragraph).strip()
    ]
    if not intro_parts:
        raise ValueError("企业介绍为空，无法填充表单")
    return "\n".join(intro_parts)


def _extract_self_source_blocks(paragraphs: List[ET.Element]) -> List[dict]:
    blocks = []
    analysis_start_idx = None
    for idx, paragraph in enumerate(paragraphs):
        data_source_text = _paragraph_text_with_breaks(paragraph).strip()
        if not re.match(r"^\s*数据来源\s*[：:]", data_source_text):
            continue

        if idx + 1 >= len(paragraphs):
            raise ValueError(f"第 {len(blocks) + 1} 层来源缺少“来源网址”段落")
        source_url_text = _paragraph_text_with_breaks(paragraphs[idx + 1]).strip()
        if not re.match(r"^\s*来源网址\s*[：:]", source_url_text):
            raise ValueError(f"第 {len(blocks) + 1} 层来源的“数据来源”后未找到“来源网址”段落")

        title_idx = _previous_non_empty_paragraph_index(paragraphs, idx - 1)
        if title_idx is None:
            raise ValueError(f"第 {len(blocks) + 1} 层来源缺少图表标题")
        if analysis_start_idx is None:
            analysis_start_idx = _first_source_analysis_start(paragraphs, title_idx)

        source_no = len(blocks) + 1
        analysis_parts = []
        for analysis_paragraph in paragraphs[analysis_start_idx:title_idx]:
            analysis_text = _paragraph_text_with_breaks(analysis_paragraph).strip()
            if _is_source_analysis_heading(analysis_text):
                continue
            analysis_parts.append(analysis_text)
        if not analysis_parts:
            raise ValueError(f"第 {source_no} 层来源缺少正文")
        analysis = "\n".join(analysis_parts)
        chart_title = _paragraph_text_with_breaks(paragraphs[title_idx]).strip()
        chart_2023, chart_2024, chart_2025 = _extract_market_values_yi(analysis, source_no=source_no)

        names_list = _extract_numbered_source_lines(data_source_text, "数据来源")
        urls_list = _extract_numbered_source_lines(source_url_text, "来源网址")

        blocks.append({
            "name": names_list[0],
            "names": names_list,
            "url": urls_list[0],
            "urls": urls_list,
            "chart_title": chart_title,
            "chart_2023": chart_2023,
            "chart_2024": chart_2024,
            "chart_2025": chart_2025,
            "analysis": analysis,
        })
        analysis_start_idx = idx + 2
    if not blocks:
        raise ValueError("未识别到任何“数据来源：/来源网址：”来源层")
    return blocks


def _set_paragraph_alignment(paragraph: ET.Element, align: str) -> None:
    ppr = paragraph.find("./w:pPr", namespaces=NS)
    if ppr is None:
        ppr = ET.Element(f"{{{NS['w']}}}pPr")
        paragraph.insert(0, ppr)
    jc = ppr.find("./w:jc", namespaces=NS)
    if jc is None:
        jc = ET.SubElement(ppr, f"{{{NS['w']}}}jc")
    jc.set(f"{{{NS['w']}}}val", align)


def apply_body_plain_paragraph_justification(root: ET.Element) -> None:
    parent_map = {child: parent for parent in root.iter() for child in list(parent)}
    for paragraph in root.findall(".//w:p", namespaces=NS):
        if _is_paragraph_inside_table(paragraph, parent_map):
            continue
        text = get_text(paragraph).strip()
        if not text or BODY_HEADING_PATTERN.match(text):
            continue
        if not _is_plain_small4_paragraph(paragraph):
            continue
        ppr = paragraph.find("./w:pPr", namespaces=NS)
        if ppr is None:
            ppr = ET.Element(f"{{{NS['w']}}}pPr")
            paragraph.insert(0, ppr)
        jc = ppr.find("./w:jc", namespaces=NS)
        if jc is None:
            jc = ET.SubElement(ppr, f"{{{NS['w']}}}jc")
        jc.set(f"{{{NS['w']}}}val", "both")


def _is_paragraph_inside_table(paragraph: ET.Element, parent_map: dict[ET.Element, ET.Element]) -> bool:
    node = paragraph
    while node in parent_map:
        node = parent_map[node]
        if node.tag == f"{{{NS['w']}}}tc":
            return True
    return False


def _is_plain_small4_paragraph(paragraph: ET.Element) -> bool:
    text_runs = []
    for run in paragraph.findall("./w:r", namespaces=NS):
        texts = "".join((node.text or "") for node in run.findall("./w:t", namespaces=NS)).strip()
        if texts:
            text_runs.append(run)
    if not text_runs:
        return False

    has_small4 = False
    for run in text_runs:
        rpr = run.find("./w:rPr", namespaces=NS)
        if rpr is None:
            continue
        if _run_is_bold(rpr) or _run_is_underline(rpr):
            return False
        if _run_font_size(rpr) == "24":
            has_small4 = True
    return has_small4


def _run_font_size(rpr: ET.Element) -> str:
    size_node = rpr.find("./w:sz", namespaces=NS)
    if size_node is None:
        size_node = rpr.find("./w:szCs", namespaces=NS)
    if size_node is None:
        return ""
    return str(size_node.get(f"{{{NS['w']}}}val") or "").strip()


def _run_is_bold(rpr: ET.Element) -> bool:
    node = rpr.find("./w:b", namespaces=NS)
    if node is None:
        return False
    value = str(node.get(f"{{{NS['w']}}}val") or "1").strip().lower()
    return value not in {"0", "false", "off"}


def _run_is_underline(rpr: ET.Element) -> bool:
    node = rpr.find("./w:u", namespaces=NS)
    if node is None:
        return False
    value = str(node.get(f"{{{NS['w']}}}val") or "single").strip().lower()
    return value not in {"none", "0", "false", "off"}


def _number_to_cn(num: int) -> str:
    if num < 0:
        return str(num)
    if num < 10:
        return CN_DIGITS[num]
    if num == 10:
        return "十"
    if num < 20:
        return "十" + CN_DIGITS[num % 10]
    if num < 100:
        tens, ones = divmod(num, 10)
        return CN_DIGITS[tens] + "十" + (CN_DIGITS[ones] if ones else "")
    return str(num)


def _format_rank_text(raw_rank: str) -> str:
    text = str(raw_rank or "").strip()
    if not text:
        return ""
    matched = re.search(r"\d+", text)
    if not matched:
        return text
    try:
        rank_no = int(matched.group(0))
    except ValueError:
        return text
    if rank_no < 1:
        return text
    return f"第{_number_to_cn(rank_no)}名"


def generate_docx_v4(data: dict, template_path, output_path):
    # 1. Open template to prepare tree
    with zipfile.ZipFile(template_path, 'r') as z:
        xml_content = z.read("word/document.xml")
        file_map = {name: z.read(name) for name in z.namelist()}
    
    register_all_namespaces(xml_content)
    tree = ET.fromstring(xml_content)
    
    # 2. Dry run to map fields to w:p elements
    p_for_field = {}
    cur = 0
    for p in tree.iter(f"{{{NS['w']}}}p"):
        runs = p.findall('./w:r', namespaces=NS)
        curr_field_runs = []
        def commit(fr):
            nonlocal cur
            if fr:
                p_for_field[cur] = p
                cur += 1
        for r in runs:
            if is_yellow_run(r):
                curr_field_runs.append(r)
            else:
                commit(curr_field_runs)
                curr_field_runs = []
        commit(curr_field_runs)

    # 3. Structural duplication or deletion
    raw_source_blocks = data.get("sources", [])
    srcs = []
    for source in raw_source_blocks if isinstance(raw_source_blocks, list) else []:
        if not isinstance(source, dict):
            continue
        normalized_names = _extract_source_names(source)
        normalized_urls = _extract_source_urls(source)
        merged = dict(source)
        merged["_names"] = normalized_names
        merged["_urls"] = normalized_urls
        merged["name"] = normalized_names[0] if normalized_names else str(source.get("name") or "").strip()
        merged["url"] = normalized_urls[0] if normalized_urls else str(source.get("url") or "").strip()
        srcs.append(merged)
    comp_data = data.get("competitors", [])
    company_name = str(data.get("company_name", "")).strip()
    named_competitors = [
        c for c in comp_data
        if str(c.get("name", "")).strip() and str(c.get("name", "")).strip() != company_name
    ]
    rank_23_text = _format_rank_text(str(data.get("rank_23", "")))
    rank_24_text = _format_rank_text(str(data.get("rank_24", "")))
    rank_25_text = _format_rank_text(str(data.get("rank_25", "")))
    try:
        chart_series = build_chart_series_from_sources(srcs, context_label="数据来源")
    except ChartDataError as exc:
        raise OtherProofError(str(exc)) from exc
    num_sources = len(srcs)
    
    body = tree.find(f".//{{{NS['w']}}}body")
    if body is not None and 22 in p_for_field and 31 in p_for_field:
        children = list(body)
        try:
            b1_start = children.index(p_for_field[22])
            b1_end = children.index(p_for_field[26])
            # block 2 starts right after block 1 ends to preserve spacing
            b2_start = b1_end + 1
            b2_end = children.index(p_for_field[31])
            
            if num_sources == 0:
                for elem in children[b1_start : b2_end + 1]:
                    body.remove(elem)
            elif num_sources == 1:
                for elem in children[b2_start : b2_end + 1]:
                    body.remove(elem)
            elif num_sources >= 2:
                insert_idx = b2_end + 1
                for i in range(num_sources - 2):
                    for elem in children[b2_start : b2_end + 1]:
                        new_elem = copy.deepcopy(elem)
                        body.insert(insert_idx, new_elem)
                        insert_idx += 1
                        
            # Refresh tree after structural changes
            # ET.fromstring effectively re-parses if we needed to, but we just modified it in place!
        except Exception as e:
            print("Structural modification failed, proceeding with mapping. Error:", e)
    if body is not None:
        _apply_self_sales_table_structure(body, company_total=1 + len(named_competitors))

    # 4. Construct flat values list based on modified structure
    vals = []
    
    # Fixed 0-15
    vals.extend([
        data.get("province", ""), data.get("company_name", ""), data.get("product_name", ""), data.get("product_code", ""),
        data.get("sale_23", ""), data.get("total_mkt_23", ""), data.get("pct_23", ""), rank_23_text,
        data.get("sale_24", ""), data.get("total_mkt_24", ""), data.get("pct_24", ""), rank_24_text,
        data.get("sale_25", ""), data.get("total_mkt_25", ""), data.get("pct_25", ""), rank_25_text
    ])
    
    # Field 16, 17 (Source meta 1 and 2, which don't dynamically scale in the intro text unfortunately)
    intro_source_names: List[str] = []
    for source in srcs:
        intro_source_names.extend(source.get("_names", []))
    vals.extend([
        _format_numbered_lines(intro_source_names, always_number=True),
        "",
    ])
    
    # Field 18-21
    vals.extend([
        f"{data.get('year', '')} 年 {data.get('month', '')} 月{data.get('day', '')} 日",
        data.get("intro", ""),
        data.get("product_name", ""),
        data.get("product_code", "")
    ])
    
    # Dynamic Source Blocks
    for s in srcs:
        source_names = s.get("_names", [])
        source_urls = s.get("_urls", [])
        vals.extend([
            s["analysis"],
            s["chart_title"],
            "", # empty placeholder for chart image
            _format_labeled_source_text("数据来源", source_names),
            _format_labeled_source_text("来源网址", source_urls),
        ])
        
    # Subsequent Fields
    vals.extend([
        data.get("product_name", ""),
        f"{data.get('sale_23', '')}万元、{data.get('sale_24', '')}万元、{data.get('sale_25', '')}万元",
        f"2023 年：（{data.get('sale_23', '')}/{data.get('total_mkt_23', '')}）*100%≈{data.get('pct_23', '')}",
        f"2024 年：（{data.get('sale_24', '')}/{data.get('total_mkt_24', '')}）*100%≈{data.get('pct_24', '')}",
        f"2025 年：（{data.get('sale_25', '')}/{data.get('total_mkt_25', '')}）*100%≈{data.get('pct_25', '')}"
    ])
    
    c_names = [str(c["name"]).strip() for c in named_competitors]
    vals.append("、".join(c_names))
    
    vals.extend([
        data.get("company_name", "")
    ])
    
    def parse_pct_ratio(p):
        text = str(p or "").strip()
        if not text:
            return None
        cleaned = text.replace('%', '').replace(',', '').strip()
        try:
            ratio = float(cleaned)
        except Exception:
            return None
        if "%" in text or ratio > 1:
            ratio = ratio / 100.0
        return ratio if ratio >= 0 else None

    def parse_number(num_text):
        text = str(num_text or "").replace(',', '').strip()
        if not text:
            return None
        try:
            return float(text)
        except Exception:
            return None

    def format_number(value):
        if value is None:
            return ""
        text = f"{value:.2f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text

    def calc_sale(mkt, pct_str):
        market = parse_number(mkt)
        ratio = parse_pct_ratio(pct_str)
        if market is None or ratio is None:
            return ""
        return format_number(market * ratio)

    company_rows_for_table = []
    company_rows_for_table.append({
        "name": data.get("company_name", ""),
        "sale_23": format_number(parse_number(data.get("sale_23", ""))),
        "pct_23": data.get("pct_23", ""),
        "sale_24": format_number(parse_number(data.get("sale_24", ""))),
        "pct_24": data.get("pct_24", ""),
        "sale_25": format_number(parse_number(data.get("sale_25", ""))),
        "pct_25": data.get("pct_25", ""),
        "ratio_25": parse_pct_ratio(data.get("pct_25", "")),
        "is_self": True,
    })

    for c in named_competitors:
        sale_23 = parse_number(c.get("sale_23", ""))
        sale_24 = parse_number(c.get("sale_24", ""))
        sale_25 = parse_number(c.get("sale_25", ""))
        company_rows_for_table.append({
            "name": c["name"],
            "sale_23": format_number(sale_23) if sale_23 is not None else calc_sale(data.get("total_mkt_23", ""), c["p23"]),
            "pct_23": c["p23"],
            "sale_24": format_number(sale_24) if sale_24 is not None else calc_sale(data.get("total_mkt_24", ""), c["p24"]),
            "pct_24": c["p24"],
            "sale_25": format_number(sale_25) if sale_25 is not None else calc_sale(data.get("total_mkt_25", ""), c["p25"]),
            "pct_25": c["p25"],
            "ratio_25": parse_pct_ratio(c["p25"]),
            "is_self": False,
        })

    company_rows_for_table.sort(
        key=lambda item: (
            item["ratio_25"] is None,
            -(item["ratio_25"] if item["ratio_25"] is not None else 0.0),
            str(item.get("name", "")),
        )
    )

    for row in company_rows_for_table:
        vals.extend([
            row["name"],
            row["sale_23"], row["pct_23"],
            row["sale_24"], row["pct_24"],
            row["sale_25"], row["pct_25"]
        ])
        
    vals.extend([
        data.get("company_name", ""), data.get("product_name", ""),
        data.get("pct_23", ""), f"（{rank_23_text}）",
        data.get("pct_24", ""), f"（{rank_24_text}）",
        data.get("pct_25", ""), f"（{rank_25_text}）"
    ])

    # 5. Replacement pass
    cur = 0
    for p in tree.iter(f"{{{NS['w']}}}p"):
        runs = p.findall('./w:r', namespaces=NS)
        fields_runs = []
        def commit(fr):
            nonlocal cur
            if not fr: return
            txt = vals[cur] if cur < len(vals) else ""
            cur += 1
            f = fr[0]
            ts = f.findall('./w:t', namespaces=NS)
            if not ts:
                ET.SubElement(f, f"{{{NS['w']}}}t")
            for ex in ts[1:]: f.remove(ex)
            _write_run_text_with_breaks(f, str(txt))
            pr = f.find('./w:rPr', namespaces=NS)
            if pr is not None:
                h = pr.find('./w:highlight', namespaces=NS)
                if h is not None: pr.remove(h)
            for other in fr[1:]:
                for ot in other.findall('./w:t', namespaces=NS): ot.text = ""
                for br in other.findall('./w:br', namespaces=NS): other.remove(br)
                oPr = other.find('./w:rPr', namespaces=NS)
                if oPr is not None:
                   ohl = oPr.find('./w:highlight', namespaces=NS)
                   if ohl is not None: oPr.remove(ohl)
        for r in runs:
            if is_yellow_run(r): fields_runs.append(r)
            else: commit(fields_runs); fields_runs = []
        commit(fields_runs)

    for pe in tree.iter():
        for ch in list(pe):
            if ch.tag == f"{{{NS['w']}}}highlight" and ch.get(f"{{{NS['w']}}}val") == 'yellow':
                pe.remove(ch)

    rewrite_header_titles(
        file_map=file_map,
        company_name=str(data.get("company_name", "")).strip(),
        product_name=str(data.get("product_name", "")).strip(),
    )
    rewrite_summary_market_research_phrase(tree, str(data.get("product_name", "")).strip())
    _rewrite_self_dynamic_chart_references(tree, source_count=num_sources)
    _rewrite_self_opening_requirement_sentence(tree, str(data.get("province", "")))
    _center_self_summary_table_cells(tree)
    _left_align_self_summary_source_cells(tree)
    _bold_self_company_row_in_sales_table(tree, company_name=str(data.get("company_name", "")).strip())
    apply_body_plain_paragraph_justification(tree)
    try:
        inject_market_charts_into_docx(
            document_root=tree,
            file_map=file_map,
            chart_series=chart_series,
            context_label="自证",
        )
    except ChartDataError as exc:
        raise OtherProofError(str(exc)) from exc
    file_map["word/document.xml"] = ET.tostring(tree, encoding='utf-8', xml_declaration=True)
    with zipfile.ZipFile(output_path, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
        for n, d in file_map.items(): zout.writestr(n, d)


def _find_self_sales_table(body: ET.Element) -> Optional[ET.Element]:
    for child in list(body):
        if child.tag != f"{{{NS['w']}}}tbl":
            continue
        rows = child.findall("./w:tr", namespaces=NS)
        if len(rows) < 3:
            continue
        first_row_text = "".join(t.text or "" for t in rows[0].findall(".//w:t", namespaces=NS))
        second_row_text = "".join(t.text or "" for t in rows[1].findall(".//w:t", namespaces=NS))
        if "企业名称" in first_row_text and "2023年" in first_row_text and "销售额（万元）" in second_row_text:
            return child
    return None


def _apply_self_sales_table_structure(body: ET.Element, *, company_total: int) -> None:
    if company_total < 1:
        raise OtherProofError("企业数量异常，至少需要 1 家企业")

    target_table = _find_self_sales_table(body)

    if target_table is None:
        raise OtherProofError("模板缺少“2023~2025主要企业全国销售额情况”表格")

    rows = target_table.findall("./w:tr", namespaces=NS)
    data_rows = rows[2:]
    if not data_rows:
        raise OtherProofError("主要企业销售额表格结构异常")

    if company_total < len(data_rows):
        for row in data_rows[company_total:]:
            target_table.remove(row)
    elif company_total > len(data_rows):
        template_row = data_rows[-1]
        for _ in range(company_total - len(data_rows)):
            target_table.append(copy.deepcopy(template_row))


def _rewrite_self_opening_requirement_sentence(tree: ET.Element, province: str = "") -> None:
    for p in tree.findall(f".//{{{NS['w']}}}p"):
        text = get_text(p)
        if "根据" not in text or "申报工作要求" not in text:
            continue
        province_part = ""
        if province.strip():
            province_part = province.strip()
            # Add 省/市 suffix if not already present
            if not province_part.endswith(("省", "市", "自治区")):
                province_part += "省"
        if province_part:
            updated = re.sub(r"根据.*?申报工作要求", f"根据{province_part}申报工作要求", text, count=1)
        else:
            updated = re.sub(r"根据.*?申报工作要求", "根据申报工作要求", text, count=1)
        if updated == text:
            continue
        set_paragraph_text(p, updated)
        return


def _center_self_summary_table_cells(tree: ET.Element) -> None:
    body = tree.find(f".//{{{NS['w']}}}body")
    if body is None:
        return
    target_table = None
    for child in list(body):
        if child.tag != f"{{{NS['w']}}}tbl":
            continue
        table_text = "".join(t.text or "" for t in child.findall(".//w:t", namespaces=NS))
        normalized_text = re.sub(r"\s+", "", table_text)
        if "企业名称" in normalized_text and "主导产品名称" in normalized_text and "2023年相关数据" in normalized_text:
            target_table = child
            break
    if target_table is None:
        return

    for cell in target_table.findall(".//w:tc", namespaces=NS):
        tcpr = cell.find("./w:tcPr", namespaces=NS)
        if tcpr is None:
            tcpr = ET.Element(f"{{{NS['w']}}}tcPr")
            cell.insert(0, tcpr)
        valign = tcpr.find("./w:vAlign", namespaces=NS)
        if valign is None:
            valign = ET.SubElement(tcpr, f"{{{NS['w']}}}vAlign")
        valign.set(f"{{{NS['w']}}}val", "center")

        for paragraph in cell.findall("./w:p", namespaces=NS):
            ppr = paragraph.find("./w:pPr", namespaces=NS)
            if ppr is None:
                ppr = ET.Element(f"{{{NS['w']}}}pPr")
                paragraph.insert(0, ppr)
            jc = ppr.find("./w:jc", namespaces=NS)
            if jc is None:
                jc = ET.SubElement(ppr, f"{{{NS['w']}}}jc")
            jc.set(f"{{{NS['w']}}}val", "center")


def _left_align_self_summary_source_cells(tree: ET.Element) -> None:
    body = tree.find(f".//{{{NS['w']}}}body")
    if body is None:
        return
    target_table = None
    for child in list(body):
        if child.tag != f"{{{NS['w']}}}tbl":
            continue
        table_text = "".join(t.text or "" for t in child.findall(".//w:t", namespaces=NS))
        normalized_text = re.sub(r"\s+", "", table_text)
        if "企业名称" in normalized_text and "主导产品名称" in normalized_text and "2023年相关数据" in normalized_text:
            target_table = child
            break
    if target_table is None:
        return

    for row in target_table.findall("./w:tr", namespaces=NS):
        row_text = "".join(t.text or "" for t in row.findall(".//w:t", namespaces=NS))
        if "数据来源" not in row_text:
            continue
        cells = row.findall("./w:tc", namespaces=NS)
        if not cells:
            continue
        for idx, cell in enumerate(cells):
            target_align = "center" if idx == 0 else "left"
            for paragraph in cell.findall(".//w:p", namespaces=NS):
                _set_paragraph_alignment(paragraph, target_align)


def _set_row_bold(row: ET.Element) -> None:
    for run in row.findall(".//w:r", namespaces=NS):
        rpr = run.find("./w:rPr", namespaces=NS)
        if rpr is None:
            rpr = ET.Element(f"{{{NS['w']}}}rPr")
            run.insert(0, rpr)
        b = rpr.find("./w:b", namespaces=NS)
        if b is None:
            b = ET.SubElement(rpr, f"{{{NS['w']}}}b")
        b.set(f"{{{NS['w']}}}val", "1")


def _bold_self_company_row_in_sales_table(tree: ET.Element, company_name: str) -> None:
    normalized_company = re.sub(r"\s+", "", str(company_name or ""))
    if not normalized_company:
        return
    body = tree.find(f".//{{{NS['w']}}}body")
    if body is None:
        return
    target_table = _find_self_sales_table(body)
    if target_table is None:
        return
    rows = target_table.findall("./w:tr", namespaces=NS)
    for row in rows[2:]:
        first_cell = row.find("./w:tc", namespaces=NS)
        if first_cell is None:
            continue
        cell_text = re.sub(r"\s+", "", get_text(first_cell))
        if cell_text != normalized_company:
            continue
        _set_row_bold(row)
        return


def _rewrite_self_dynamic_chart_references(tree: ET.Element, *, source_count: int) -> None:
    comparison_chart_no = source_count + 1
    share_chart_no = source_count + 2

    for p in tree.findall(f".//{{{NS['w']}}}p"):
        text = get_text(p)
        if not text:
            continue

        if "市场规模以及各企业2023年-2025年产品销售额如图表" in text and "占有率如图表" in text:
            replace_order = [comparison_chart_no, share_chart_no]
            counter = {"i": 0}

            def _replace(match):
                idx = counter["i"]
                counter["i"] += 1
                if idx < len(replace_order):
                    return f"图表{replace_order[idx]}"
                return match.group(0)

            updated = re.sub(r"图表\s*\d+", _replace, text)
            set_paragraph_text(p, updated)
            continue

        if text.startswith("图表") and "主要企业全国销售额情况" in text:
            updated = re.sub(r"^图表\s*\d+", f"图表 {comparison_chart_no}", text, count=1)
            set_paragraph_text(p, updated)
            continue

        if text.startswith("图表") and "占有率情况" in text:
            updated = re.sub(r"^图表\s*\d+", f"图表 {share_chart_no}", text, count=1)
            set_paragraph_text(p, updated)

def extract_self_docx_fields(uploaded_path: str) -> dict:
    """Reverse-engineer a filled self-proof .docx back into form fields.

    Maps the template's yellow-highlighted runs onto corresponding runs in
    the uploaded document to extract only the user-filled values (not the
    surrounding static template text).
    """

    # 1. Build field-index → (paragraph_index, [run_indices]) from the template
    with zipfile.ZipFile(SELF_TEMPLATE_PATH, 'r') as z:
        tmpl_xml = z.read("word/document.xml")
    register_all_namespaces(tmpl_xml)
    tmpl_tree = ET.fromstring(tmpl_xml)

    # field_info[field_idx] = (paragraph_index, [run_index, ...])
    field_info = {}
    p_idx = -1
    cur = 0
    for p in tmpl_tree.iter(f"{{{NS['w']}}}p"):
        p_idx += 1
        runs = p.findall('./w:r', namespaces=NS)
        curr_field_run_indices = []
        r_idx = -1
        for r in runs:
            r_idx += 1
            if is_yellow_run(r):
                curr_field_run_indices.append(r_idx)
            else:
                if curr_field_run_indices:
                    field_info[cur] = (p_idx, tuple(curr_field_run_indices))
                    cur += 1
                curr_field_run_indices = []
        if curr_field_run_indices:
            field_info[cur] = (p_idx, tuple(curr_field_run_indices))
            cur += 1

    # 2. Parse uploaded docx
    with zipfile.ZipFile(uploaded_path, 'r') as z:
        up_xml = z.read("word/document.xml")

    register_all_namespaces(up_xml)
    up_tree = ET.fromstring(up_xml)
    up_paragraphs = list(up_tree.iter(f"{{{NS['w']}}}p"))

    # 3. Extract source blocks from the original template paragraph layout.
    sources = _extract_self_source_blocks(up_paragraphs)
    src_count = len(sources)

    TEMPLATE_SRC_COUNT = 2

    def _field_text(field_idx):
        """Return only the user-filled text for *field_idx* in the uploaded docx.

        Extracts text exclusively from the runs that correspond to
        yellow-highlighted runs in the template, ignoring static text.
        """
        if field_idx < 22:
            # Fixed preamble — paragraph index is the same
            info = field_info.get(field_idx)
            if info is None:
                return ""
            tmpl_p_idx, run_indices = info
            target_p_idx = tmpl_p_idx
        elif field_idx < 22 + 5 * src_count:
            # Inside dynamically-scaled source blocks.
            # For sources beyond the template's native source count we
            # reuse the first source block's run indices and offset the
            # paragraph index.
            source_no = (field_idx - 22) // 5
            slot = (field_idx - 22) % 5
            ref_info = field_info.get(22 + slot)
            if ref_info is None:
                return ""
            ref_p_idx, run_indices = ref_info
            target_p_idx = ref_p_idx + source_no * 5
        else:
            # Fields after source blocks — shift by (src_count - 2) * 5
            info = field_info.get(field_idx)
            if info is None:
                return ""
            tmpl_p_idx, run_indices = info
            shift = (src_count - TEMPLATE_SRC_COUNT) * 5
            target_p_idx = tmpl_p_idx + shift

        if target_p_idx < 0 or target_p_idx >= len(up_paragraphs):
            return ""

        up_p = up_paragraphs[target_p_idx]
        up_runs = up_p.findall('./w:r', namespaces=NS)

        parts = []
        for ri in run_indices:
            if ri < len(up_runs):
                parts.append(_run_text_with_breaks(up_runs[ri]))
        return "".join(parts).strip()

    # 5. Extract each high-level value
    result = {}

    # ---- Fixed 0-15 ----------------------------------------------------------
    # Province (field 0) may have been merged into the opening sentence by
    # _rewrite_self_opening_requirement_sentence, so try to extract it from
    # the full paragraph text first.
    prov_info = field_info.get(0)
    if prov_info:
        prov_p_idx = prov_info[0]
        if prov_p_idx < len(up_paragraphs):
            prov_para_text = get_text(up_paragraphs[prov_p_idx])
            prov_match = re.search(r"根据(.+?)(?:省|市|自治区)申报工作要求", prov_para_text)
            if prov_match:
                result["province"] = prov_match.group(1).strip()
            else:
                result["province"] = _field_text(0)
        else:
            result["province"] = _field_text(0)
    else:
        result["province"] = _field_text(0)
    result["company_name"] = _field_text(1)
    result["product_name"] = _field_text(2)
    result["product_code"] = _field_text(3)
    result["sale_23"]      = _field_text(4)
    result["total_mkt_23"] = _field_text(5)
    result["pct_23"]       = _field_text(6)
    result["rank_23"]      = _field_text(7)
    result["sale_24"]      = _field_text(8)
    result["total_mkt_24"] = _field_text(9)
    result["pct_24"]       = _field_text(10)
    result["rank_24"]      = _field_text(11)
    result["sale_25"]      = _field_text(12)
    result["total_mkt_25"] = _field_text(13)
    result["pct_25"]       = _field_text(14)
    result["rank_25"]      = _field_text(15)

    # ---- Intro source names (16) & empty (17) --------------------------------
    _ = _field_text(16)  # intro source names — not needed for reverse
    _ = _field_text(17)

    # ---- Report date (18), intro (19), product_name (20), product_code (21) --
    date_text = _field_text(18)
    year = month = day = ""
    date_match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", date_text)
    if date_match:
        year = date_match.group(1)
        month = date_match.group(2)
        day = date_match.group(3)
    result["year"]  = year
    result["month"] = month
    result["day"]   = day

    result["company_intro"] = _extract_self_company_intro(up_paragraphs)
    result["product_intro"] = ""
    result["proof_scope"] = ""
    result["market_name"] = ""
    result["target_scope"] = _detect_self_target_scope(up_paragraphs)

    # ---- Sources -------------------------------------------------------------
    result["sources"] = sources

    # ---- Competitors extracted from the sales table --------------------------
    body = up_tree.find(f".//{{{NS['w']}}}body")
    all_competitors = _extract_competitors_from_self_table(body)

    # The first row in the sales table is the self company — exclude it
    company_name = result.get("company_name", "")
    competitors = [
        c for c in all_competitors
        if c.get("name", "").strip() != company_name.strip()
    ]

    result["competitors"] = competitors

    # ---- Template type is always self ----------------------------------------
    result["template_type"] = "self"

    return result


def _extract_competitors_from_self_table(body) -> list:
    """Extract competitor rows from the self-proof sales table."""
    if body is None:
        return []

    target_table = None
    for child in list(body):
        if child.tag != f"{{{NS['w']}}}tbl":
            continue
        rows = child.findall("./w:tr", namespaces=NS)
        if len(rows) < 3:
            continue
        first_row_text = "".join(t.text or "" for t in rows[0].findall(".//w:t", namespaces=NS))
        second_row_text = "".join(t.text or "" for t in rows[1].findall(".//w:t", namespaces=NS))
        if "企业名称" in first_row_text and "2023年" in first_row_text and "销售额（万元）" in second_row_text:
            target_table = child
            break

    if target_table is None:
        return []

    rows = target_table.findall("./w:tr", namespaces=NS)
    data_rows = rows[2:]  # skip two header rows

    competitors = []
    for row in data_rows:
        cells = row.findall("./w:tc", namespaces=NS)
        if not cells:
            continue
        name = get_text(cells[0]).strip()
        if not name:
            continue
        competitor = {"name": name}
        # Cells are: name, sale_23, pct_23, sale_24, pct_24, sale_25, pct_25 (7 cells per row)
        if len(cells) >= 7:
            competitor["sale_23"] = get_text(cells[1]).strip()
            competitor["p23"]    = get_text(cells[2]).strip()
            competitor["sale_24"] = get_text(cells[3]).strip()
            competitor["p24"]    = get_text(cells[4]).strip()
            competitor["sale_25"] = get_text(cells[5]).strip()
            competitor["p25"]    = get_text(cells[6]).strip()
        competitors.append(competitor)

    return competitors
