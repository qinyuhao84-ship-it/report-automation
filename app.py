import zipfile
import os
import json
import xml.etree.ElementTree as ET
import copy
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from typing import List, Optional
import uvicorn
from fastapi.middleware.cors import CORSMiddleware

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

class DataModel(BaseModel):
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

def generate_docx_v4(data: dict, template_path, output_path):
    # 1. Open template to prepare tree
    with zipfile.ZipFile(template_path, 'r') as z:
        xml_content = z.read("word/document.xml")
        file_map = {name: z.read(name) for name in z.namelist()}
    
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

@app.post("/generate")
def generate_api(data: DataModel):
    try:
        generate_docx_v4(data.dict(), TEMPLATE_PATH, "output.docx")
        return FileResponse("output.docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename="output.docx")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
@app.get("/")
def index():
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>结构化报告自动化系统</title>
    <style>
        :root { --p: #3b82f6; --t: #1e293b; --bg: #f8fafc; }
        body { font-family: -apple-system, sans-serif; background: var(--bg); color: var(--t); margin:0; padding:20px; }
        .wrap { max-width: 1100px; margin: 0 auto; }
        .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; }
        .card { background: white; padding: 20px; border-radius: 12px; border: 1px solid #e2e8f0; }
        h2 { font-size: 1rem; color: var(--p); border-bottom: 2px solid #f1f5f9; padding-bottom: 8px; margin-top:0; }
        .row { margin-bottom: 12px; }
        label { display: block; font-size: 12px; font-weight: bold; color: #64748b; margin-bottom: 4px; }
        input, select, textarea { width: 100%; padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box; }
        textarea { height: 100px; resize: none; font-size: 13px; line-height: 1.5; }
        .full { grid-column: span 2; }
        .source-card { border-left: 4px solid var(--p); margin-bottom: 15px; background: #fdfdfd; padding: 15px; border-radius: 4px; position: relative; }
        .tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
        .tbl th, .tbl td { border: 1px solid #eee; padding: 6px; text-align: center; }
        .btn-plus { background: #10b981; color: white; border: none; padding: 8px 15px; border-radius: 4px; cursor: pointer; float: right; margin-bottom: 10px; font-size: 13px; font-weight:bold;}
        .btn-plus:hover { background: #059669; }
        .btn-del { position: absolute; top: 10px; right: 10px; color: #ef4444; border: none; background: none; cursor: pointer; font-size: 18px; }
        .actions { text-align: center; margin-top: 30px; position: sticky; bottom: 10px; }
        .main-btn { padding: 15px 40px; background: var(--p); color: white; border: none; border-radius: 10px; font-size: 16px; font-weight: bold; cursor: pointer; }
        input:read-only { background-color: #f1f5f9; color: #94a3b8; cursor: not-allowed; }
        .tips { background: #fffbeb; border: 1px solid #fef3c7; color: #92400e; padding: 15px; border-radius: 8px; margin-bottom: 25px; line-height: 1.6; }
    </style>
</head>
<body>
<div class="wrap">
    <div class="tips">
        <strong>⚠️ 填写须知（生成后需手动微调）：</strong><br>
        1. 第一页表格的数据来源部分未自动加上序号，请在 Word 中手动校对。<br>
        2. 图表（如柱状图）需使用 Excel 另行生成并插入，请注意图表序号需与报告思路顺序对应改变。
    </div>
    
    <h1>📄 结构化报告自动化</h1>
    <div class="grid">
        <div class="card">
            <h2>🏢 基础信息</h2>
            <div class="row"><label>省份</label><input id="province" value=""></div>
            <div class="row"><label>企业名</label><input id="company_name" value=""></div>
            <div class="row"><label>产品名</label><input id="product_name" value=""></div>
            <div class="row"><label>代码</label><input id="product_code" value=""></div>
            <div class="row" style="display:flex; gap:10px;">
                <div style="flex:1"><label>年</label><select id="year"></select></div>
                <div style="flex:1"><label>月</label><select id="month"></select></div>
                <div style="flex:1"><label>日</label><select id="day"></select></div>
            </div>
        </div>
        <div class="card">
            <h2>📝 企业介绍</h2>
            <textarea id="intro" style="height:250px;"></textarea>
        </div>
        <div class="card full">
            <h2>📈 我的经营业绩 (2023-2025)</h2>
            <table class="tbl">
                <tr><th>年份</th><th>销售额 (万元)</th><th>总规模 (万元)</th><th>占有率 (%) (自动计算)</th><th>排名</th></tr>
                <tr><td>2023</td><td><input id="sale_23" oninput="calcMyPct('23')"></td><td><input id="total_mkt_23" oninput="calcMyPct('23')"></td><td><input id="pct_23" readonly></td><td><input id="rank_23"></td></tr>
                <tr><td>2024</td><td><input id="sale_24" oninput="calcMyPct('24')"></td><td><input id="total_mkt_24" oninput="calcMyPct('24')"></td><td><input id="pct_24" readonly></td><td><input id="rank_24"></td></tr>
                <tr><td>2025</td><td><input id="sale_25" oninput="calcMyPct('25')"></td><td><input id="total_mkt_25" oninput="calcMyPct('25')"></td><td><input id="pct_25" readonly></td><td><input id="rank_25"></td></tr>
            </table>
        </div>
        <div class="card full" id="source_container">
            <h2>📂 报告思路与数据源</h2>
            <button class="btn-plus" onclick="addSource()">+ 添加一段数据来源与分析</button>
            <div style="clear:both; margin-bottom:10px;"></div>
            <div id="sources_list"></div>
        </div>
        <div class="card full">
            <h2>🏆 竞争对手 (只需填写名称与占有率)</h2>
            <table class="tbl" id="comp_table">
                <tr><th>对手名称</th><th>2023 占有率 (%)</th><th>2024 占有率 (%)</th><th>2025 占有率 (%)</th></tr>
                <tr><td><input class="c-name"></td><td><input class="c-p23"></td><td><input class="c-p24"></td><td><input class="c-p25"></td></tr>
                <tr><td><input class="c-name"></td><td><input class="c-p23"></td><td><input class="c-p24"></td><td><input class="c-p25"></td></tr>
                <tr><td><input class="c-name"></td><td><input class="c-p23"></td><td><input class="c-p24"></td><td><input class="c-p25"></td></tr>
                <tr><td><input class="c-name"></td><td><input class="c-p23"></td><td><input class="c-p24"></td><td><input class="c-p25"></td></tr>
            </table>
        </div>
    </div>
    <div class="actions">
        <button class="main-btn" onclick="generate()">🚀 生成最终 Word</button>
        <div id="status" style="margin-top:10px; font-weight:bold;"></div>
    </div>
</div>
<script>
    function initDates() {
        const y = document.getElementById('year'), m = document.getElementById('month'), d = document.getElementById('day');
        for(let i=2020; i<=2035; i++) y.add(new Option(i, i)); y.value="2026";
        for(let i=1; i<=12; i++) m.add(new Option(i, i)); m.value="3";
        for(let i=1; i<=31; i++) d.add(new Option(i, i)); d.value="15";
    }
    function calcMyPct(year) {
        let sale = parseFloat(document.getElementById('sale_'+year).value);
        let mkt = parseFloat(document.getElementById('total_mkt_'+year).value);
        let pctInput = document.getElementById('pct_'+year);
        if (!isNaN(sale) && !isNaN(mkt) && mkt !== 0) { pctInput.value = (sale / mkt * 100).toFixed(2) + '%'; } else { pctInput.value = ''; }
    }
    let sourceCount = 0;
    function addSource() {
        const div = document.createElement('div'); div.className = 'source-card';
        sourceCount++;
        div.innerHTML = `<button class="btn-del" onclick="this.parentElement.remove()">×</button>
            <div style="font-weight:bold; color:var(--p); margin-bottom:10px;">► 数据段落 ${sourceCount}</div>
            <div class="row"><label>数据源名称</label><input class="s-name"></div>
            <div class="row"><label>数据源网址/链接</label><input class="s-url"></div>
            <div class="row"><label>配套图表名称</label><input class="s-chart"></div>
            <div class="row"><label>分析结论文本块</label><textarea class="s-text"></textarea></div>`;
        document.getElementById('sources_list').appendChild(div);
    }
    initDates(); addSource(); // Start with one empty slot
    async function generate() {
        const data = {
            province: document.getElementById('province').value,
            company_name: document.getElementById('company_name').value,
            product_name: document.getElementById('product_name').value,
            product_code: document.getElementById('product_code').value,
            year: document.getElementById('year').value,
            month: document.getElementById('month').value,
            day: document.getElementById('day').value,
            intro: document.getElementById('intro').value,
            sale_23: document.getElementById('sale_23').value, total_mkt_23: document.getElementById('total_mkt_23').value, pct_23: document.getElementById('pct_23').value, rank_23: document.getElementById('rank_23').value,
            sale_24: document.getElementById('sale_24').value, total_mkt_24: document.getElementById('total_mkt_24').value, pct_24: document.getElementById('pct_24').value, rank_24: document.getElementById('rank_24').value,
            sale_25: document.getElementById('sale_25').value, total_mkt_25: document.getElementById('total_mkt_25').value, pct_25: document.getElementById('pct_25').value, rank_25: document.getElementById('rank_25').value,
            sources: [], competitors: []
        };
        document.querySelectorAll('.source-card').forEach(c => {
            data.sources.push({ name: c.querySelector('.s-name').value, url: c.querySelector('.s-url').value, chart_title: c.querySelector('.s-chart').value, analysis: c.querySelector('.s-text').value });
        });
        document.querySelectorAll('#comp_table tr').forEach((tr, i) => {
            if(i===0) return;
            data.competitors.push({ name: tr.querySelector('.c-name').value, p23: tr.querySelector('.c-p23').value, p24: tr.querySelector('.c-p24').value, p25: tr.querySelector('.c-p25').value });
        });
        document.getElementById('status').innerText = "生成中...";
        try {
            let r = await fetch('/generate', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data) });
            if(r.ok) { 
                let b=await r.blob(); let a=document.createElement('a'); a.href=URL.createObjectURL(b); a.download='output.docx'; a.click(); 
                document.getElementById('status').innerText = "✅ 生成并下载成功！";
            } else { let err = await r.json(); document.getElementById('status').innerText = "❌ 生成失败：" + err.detail; }
        } catch(e) { document.getElementById('status').innerText = "❌ 网络错误：" + e; }
    }
</script>
</body>
</html>"""
    return HTMLResponse(content=html)

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000)

