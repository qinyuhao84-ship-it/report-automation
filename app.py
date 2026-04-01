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
from inference import (
    CancelTaskResponse,
    CreateTaskResponse,
    InferConfigPatch,
    InferenceConfig,
    InferenceInput,
    InferenceTaskManager,
    TaskResult,
    TaskStatus,
)
from other_proof import (
    OtherProofError,
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

class SourceBlock(BaseModel):
    name: str
    url: str
    chart_title: str
    analysis: str

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

    # 4. Construct flat values list based on modified structure
    vals = []
    
    # Fixed 0-15
    vals.extend([
        data.get("province", ""), data.get("company_name", ""), data.get("product_name", ""), data.get("product_code", ""),
        data.get("sale_23", ""), data.get("total_mkt_23", ""), data.get("pct_23", ""), data.get("rank_23", ""),
        data.get("sale_24", ""), data.get("total_mkt_24", ""), data.get("pct_24", ""), data.get("rank_24", ""),
        data.get("sale_25", ""), data.get("total_mkt_25", ""), data.get("pct_25", ""), data.get("rank_25", "")
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
            f"数据来源：{s['url']}",
            f"来源网址：{s['name']}"
        ])
        
    # Subsequent Fields
    vals.extend([
        data.get("product_name", ""),
        f"{data.get('sale_23', '')}万元、{data.get('sale_24', '')}万元、{data.get('sale_25', '')}万元",
        f"2023 年：（{data.get('sale_23', '')}/{data.get('total_mkt_23', '')}）*100%≈{data.get('pct_23', '')}",
        f"2024 年：（{data.get('sale_24', '')}/{data.get('total_mkt_24', '')}）*100%≈{data.get('pct_24', '')}",
        f"2025 年：（{data.get('sale_25', '')}/{data.get('total_mkt_25', '')}）*100%≈{data.get('pct_25', '')}"
    ])
    
    comp_data = data.get("competitors", [])
    c_names = [c["name"] for c in comp_data if c.get("name") and c["name"] != data.get("company_name", "")]
    vals.append("、".join(c_names))
    
    vals.extend([
        data.get("company_name", ""),
        data.get("company_name", ""),
        data.get("sale_23", ""), data.get("pct_23", ""),
        data.get("sale_24", ""), data.get("pct_24", ""),
        data.get("sale_25", ""), data.get("pct_25", "")
    ])
    
    def clean_pct(p):
        try: return float(p.replace('%', '').strip()) / 100.0
        except: return 0.0
    
    def calc_sale(mkt, pct_str):
        try:
            m = float(mkt)
            p = clean_pct(pct_str)
            return f"{m * p:.2f}"
        except: return "0.00"

    for i in range(4):
        c = comp_data[i] if i < len(comp_data) else {"name": "", "p23": "", "p24": "", "p25": ""}
        vals.extend([
            c["name"],
            calc_sale(data.get("total_mkt_23", ""), c["p23"]), c["p23"],
            calc_sale(data.get("total_mkt_24", ""), c["p24"]), c["p24"],
            calc_sale(data.get("total_mkt_25", ""), c["p25"]), c["p25"]
        ])
        
    vals.extend([
        data.get("company_name", ""), data.get("product_name", ""),
        data.get("pct_23", ""), f"（{data.get('rank_23', '')}）",
        data.get("pct_24", ""), f"（{data.get('rank_24', '')}）",
        data.get("pct_25", ""), f"（{data.get('rank_25', '')}）"
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
    file_map["word/document.xml"] = ET.tostring(tree, encoding='utf-8', xml_declaration=True)
    with zipfile.ZipFile(output_path, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
        for n, d in file_map.items(): zout.writestr(n, d)

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

inference_task_manager = InferenceTaskManager()


@app.post("/infer-market-share", response_model=CreateTaskResponse, status_code=202)
def submit_inference_task(payload: InferenceInput):
    try:
        return inference_task_manager.submit(payload)
    except Exception:
        raise HTTPException(status_code=500, detail="推理任务提交失败")


@app.get("/infer-market-share/{task_id}", response_model=TaskResult)
def get_inference_task(task_id: str):
    task = inference_task_manager.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="推理任务不存在")
    return task


@app.post("/infer-market-share/{task_id}/cancel", response_model=CancelTaskResponse)
def cancel_inference_task(task_id: str):
    result = inference_task_manager.cancel(task_id)
    if not result.accepted and result.status == TaskStatus.FAILED:
        raise HTTPException(status_code=404, detail=result.message or "推理任务不存在")
    return result


@app.get("/infer-config", response_model=InferenceConfig)
def get_inference_config():
    return inference_task_manager.get_config()


@app.put("/infer-config", response_model=InferenceConfig)
def update_inference_config(patch: InferConfigPatch):
    try:
        return inference_task_manager.update_config(patch)
    except Exception:
        raise HTTPException(status_code=400, detail="配置更新失败，请检查参数")


@app.post("/other-proof/chapter1")
def generate_other_proof_chapter1_api(payload: Chapter1Request):
    try:
        return generate_other_chapter1(payload.product_name, inference_task_manager.get_config())
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
