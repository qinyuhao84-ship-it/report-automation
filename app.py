import zipfile
import os
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
import copy
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from chart_docx import (
    ChartDataError,
    build_chart_series_from_sources,
    inject_market_charts_into_docx,
)
from inference import (
    InferenceConfig,
)
from other_proof import (
    OtherProofError,
    OtherProofTimeoutError,
    generate_other_chapter1,
    generate_other_docx,
    lookup_other_companies,
)

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


def set_paragraph_text(p: ET.Element, value: str) -> None:
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

class SourceBlock(BaseModel):
    name: str
    url: str
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
    sale_23: str; total_mkt_23: str; pct_23: str; rank_23: str
    sale_24: str; total_mkt_24: str; pct_24: str; rank_24: str
    sale_25: str; total_mkt_25: str; pct_25: str; rank_25: str
    sources: List[SourceBlock]
    competitors: List[Competitor]
    company_intro_text: Optional[str] = None
    proof_scope: Optional[str] = None
    market_name: Optional[str] = None
    chapter2_layers: List[OtherProofLayer] = Field(default_factory=list)
    chapter1_sections: List[Chapter1Section] = Field(default_factory=list)
    resolved_company_profiles: List[ResolvedCompanyProfile] = Field(default_factory=list)

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
    srcs = data.get("sources", [])
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
    s0_name = srcs[0]["name"] if num_sources > 0 else ""
    s1_name = srcs[1]["name"] if num_sources > 1 else ""
    vals.extend([s0_name, s1_name])
    
    # Field 18-21
    vals.extend([
        f"{data.get('year', '')} 年 {data.get('month', '')} 月{data.get('day', '')} 日",
        data.get("intro", ""),
        data.get("product_name", ""),
        data.get("product_code", "")
    ])
    
    # Dynamic Source Blocks
    for s in srcs:
        vals.extend([
            s["analysis"],
            s["chart_title"],
            "", # empty placeholder for chart image
            f"数据来源：{s['name']}",
            f"来源网址：{s['url']}"
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
            t = ts[0] if ts else ET.SubElement(f, f"{{{NS['w']}}}t")
            for ex in ts[1:]: f.remove(ex)
            t.text = str(txt)
            pr = f.find('./w:rPr', namespaces=NS)
            if pr is not None:
                h = pr.find('./w:highlight', namespaces=NS)
                if h is not None: pr.remove(h)
            for other in fr[1:]:
                for ot in other.findall('./w:t', namespaces=NS): ot.text = ""
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
    _rewrite_self_opening_requirement_sentence(tree)
    _center_self_summary_table_cells(tree)
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


def _rewrite_self_opening_requirement_sentence(tree: ET.Element) -> None:
    for p in tree.findall(f".//{{{NS['w']}}}p"):
        text = get_text(p)
        if "根据" not in text or "申报工作要求" not in text:
            continue
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

app = FastAPI()

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境中应替换为具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMPLATE_PATH = "0315-浙江达航数据技术有限公司-自证-初版.docx"
OTHER_TEMPLATE_PATH = "0323-高安全性自锁紧型电源连接系统市场占有率证明报告-初版.docx"


@app.post("/other-proof/chapter1")
def generate_other_proof_chapter1_api(payload: Chapter1Request):
    try:
        return generate_other_chapter1(
            payload.product_name,
            InferenceConfig(),
            allow_partial=bool(payload.allow_partial),
        )
    except OtherProofTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except OtherProofError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/other-proof/company-lookup")
def lookup_other_proof_companies_api(payload: CompanyLookupRequest):
    try:
        return lookup_other_companies([item.model_dump() for item in payload.companies])
    except OtherProofError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/generate")
def generate_api(data: DataModel):
    try:
        if data.template_type == "self":
            generate_docx_v4(data.model_dump(), TEMPLATE_PATH, "output.docx")
            return FileResponse("output.docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename="output.docx")
        if data.template_type == "other":
            warnings = generate_other_docx(data.model_dump(), OTHER_TEMPLATE_PATH, "output.docx")
            headers = {}
            if warnings:
                headers["X-Generate-Warnings"] = urllib.parse.quote(json.dumps(warnings, ensure_ascii=False))
            return FileResponse(
                "output.docx",
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename="output.docx",
                headers=headers,
            )
        else:
            raise HTTPException(status_code=400, detail="不支持的模板类型")
    except OtherProofError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def index():
    frontend_path = Path(__file__).parent / "frontend" / "index.html"
    try:
        html = frontend_path.read_text(encoding="utf-8")
    except Exception:
        raise HTTPException(status_code=500, detail="前端模板加载失败")
    return HTMLResponse(content=html)


@app.get("/frontend/{file_path:path}")
def frontend_assets(file_path: str):
    frontend_dir = (Path(__file__).parent / "frontend").resolve()
    target = (frontend_dir / file_path).resolve()
    if not str(target).startswith(str(frontend_dir)) or not target.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(target)

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000)
