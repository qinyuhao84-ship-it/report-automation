from __future__ import annotations

import copy
import html
import json
import re
import subprocess
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Sequence

import httpx

from chart_docx import (
    ChartDataError,
    build_chart_series_from_sources,
    inject_market_charts_into_docx,
)
from inference import InferenceConfig, LLMOrchestrator

def _register_all_namespaces(xml_content):
    import re
    # Extract all xmlns:prefix="uri" from the XML content
    ns_matches = re.findall(r'xmlns:([^=]+)="([^"]+)"', xml_content.decode('utf-8') if isinstance(xml_content, bytes) else xml_content)
    for prefix, uri in ns_matches:
        ET.register_namespace(prefix, uri)

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)

CHAPTER1_SECTION_SPECS: List[Dict[str, Any]] = [
    {"key": "background_overview", "title": "背景与概述", "slot_count": 4},
    {"key": "definition", "title": "定义", "slot_count": 3},
    {"key": "working_principle", "title": "工作原理", "slot_count": 14},
    {"key": "product_attributes", "title": "产品属性", "slot_count": 11},
    {"key": "technical_specifications", "title": "技术规范", "slot_count": 5},
    {"key": "industry_history", "title": "行业发展历程", "slot_count": 5},
    {"key": "industry_environment", "title": "行业发展环境", "slot_count": 17},
    {"key": "industry_trends", "title": "行业发展趋势", "slot_count": 28},
    {"key": "industry_supply_chain", "title": "行业供应链", "slot_count": 18},
]
CHAPTER1_BATCH_KEYS: List[List[str]] = [
    [
        "background_overview",
    ],
    [
        "definition",
        "working_principle",
        "product_attributes",
        "technical_specifications",
    ],
    [
        "industry_history",
    ],
    [
        "industry_environment",
        "industry_trends",
    ],
    [
        "industry_supply_chain",
    ],
]
CHAPTER1_VISIBLE_PARAGRAPH_COUNTS: Dict[str, int] = {
    "background_overview": 2,
    "definition": 2,
    "working_principle": 5,
    "product_attributes": 4,
    "technical_specifications": 2,
    "industry_history": 2,
    "industry_environment": 5,
    "industry_trends": 6,
    "industry_supply_chain": 5,
}

SUPPLY_CHAIN_SUBTOPICS: List[str] = [
    "上游供应链",
    "中游制造与集成",
    "下游应用与分销",
    "行业供应链的核心特征与面临的挑战",
    "行业供应链的发展方向",
]

CHAPTER1_SPEC_MAP = {item["key"]: item for item in CHAPTER1_SECTION_SPECS}
EXPECTED_CHAPTER1_SLOT_COUNT = sum(item["slot_count"] for item in CHAPTER1_SECTION_SPECS)
PLACEHOLDER_TEXT = "该部分生成失败，请人工补充。"
CHAPTER1_RETRY_GUIDANCE = "第一章暂未生成完成。通常是模型响应较慢或服务繁忙。请直接重试；如需先继续出报告，可勾选“第一章失败后跳过继续生成”，系统会跳过第一章正文并继续生成后续章节。"
AIQICHA_TIMEOUT = 20.0
SEARCH_TIMEOUT = 20.0
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0"}
BODY_HEADING_PATTERN = re.compile(
    r"^\s*(图表|数据来源|来源网址|第[一二三四五六七八九十0-9]+章|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\([一二三四五六七八九十]+\)|\d+[\.、])"
)


class OtherProofError(RuntimeError):
    pass


class OtherProofTimeoutError(OtherProofError):
    pass


def generate_other_chapter1(product_name: str, config: InferenceConfig, allow_partial: bool = False) -> Dict[str, Any]:
    if not product_name or not product_name.strip():
        raise OtherProofError("主导产品名称不能为空")

    orchestrator = LLMOrchestrator.from_config(config)
    if not orchestrator.is_available() or orchestrator.client is None:
        raise OtherProofError("LLM 未配置，无法生成他证第一章")

    product = product_name.strip()
    # timeout_seconds <= 0 时，底层客户端会关闭请求超时限制。
    chapter1_timeout_seconds = 0
    chapter1_model = _resolve_chapter1_model_name(
        config.llm_model,
        getattr(orchestrator.client, "model", "") or config.llm_model,
    )
    chapter1_max_output_tokens = max(3200, min(int(config.llm_max_output_tokens), 5200))
    if _is_reasoner_model(chapter1_model):
        chapter1_max_output_tokens = 4200
    warnings: List[str] = []

    try:
        raw_sections, batch_warnings = _generate_chapter1_sections_in_batches(
            client=orchestrator.client,
            product_name=product,
            model=chapter1_model,
            timeout_seconds=chapter1_timeout_seconds,
            max_output_tokens=chapter1_max_output_tokens,
        )
    except Exception as exc:
        raise OtherProofTimeoutError(CHAPTER1_RETRY_GUIDANCE) from exc

    warnings.extend(batch_warnings)

    if not raw_sections:
        raise OtherProofTimeoutError(CHAPTER1_RETRY_GUIDANCE)

    normalized, normalize_warnings = normalize_chapter1_sections(raw_sections)
    warnings.extend(normalize_warnings)
    normalized, repair_warnings, repaired_keys = _repair_empty_chapter1_sections(
        client=orchestrator.client,
        model=chapter1_model,
        product_name=product,
        sections=normalized,
        timeout_seconds=chapter1_timeout_seconds,
        max_output_tokens=chapter1_max_output_tokens,
    )
    if repaired_keys:
        repaired_titles = {
            CHAPTER1_SPEC_MAP[key]["title"]
            for key in repaired_keys
            if key in CHAPTER1_SPEC_MAP
        }
        warnings = [
            item
            for item in warnings
            if not (
                any(f"第一章《{title}》" in item for title in repaired_titles)
                and ("未生成成功" in item or "段落不足" in item)
            )
        ]
    warnings.extend(repair_warnings)
    if not allow_partial:
        strict_failed_titles = []
        for spec in CHAPTER1_SECTION_SPECS:
            section = next((item for item in normalized if str(item.get("key") or "").strip() == spec["key"]), None)
            paragraphs = section.get("paragraphs") if isinstance(section, dict) else []
            non_placeholder = [item for item in (paragraphs or []) if str(item).strip() and str(item).strip() != PLACEHOLDER_TEXT]
            has_placeholder = any(_is_chapter1_placeholder_text(item) for item in (paragraphs or []))
            if not non_placeholder or has_placeholder:
                strict_failed_titles.append(spec["title"])
        if strict_failed_titles:
            raise OtherProofTimeoutError(
                "第一章有部分小节内容不完整（"
                + "、".join(strict_failed_titles)
                + "），系统已停止写入，避免生成错乱报告。请直接重试；如需先继续出报告，可勾选“第一章失败后跳过继续生成”，系统会跳过第一章正文并继续生成后续章节。"
            )

    return {"sections": normalized, "warnings": warnings}


def _generate_chapter1_sections_in_batches(
    *,
    client: Any,
    product_name: str,
    model: str,
    timeout_seconds: int,
    max_output_tokens: int,
) -> tuple[List[Dict[str, Any]], List[str]]:
    merged_sections: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for batch_index, batch_keys in enumerate(CHAPTER1_BATCH_KEYS, start=1):
        batch_sections, batch_warning = _generate_chapter1_batch(
            client=client,
            product_name=product_name,
            model=model,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            batch_index=batch_index,
            batch_keys=batch_keys,
            generated_sections=merged_sections,
        )
        merged_sections.extend(batch_sections)
        if batch_warning:
            warnings.append(batch_warning)
    return merged_sections, warnings


def _generate_chapter1_batch(
    *,
    client: Any,
    product_name: str,
    model: str,
    timeout_seconds: int,
    max_output_tokens: int,
    batch_index: int,
    batch_keys: Sequence[str],
    generated_sections: Sequence[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], str]:
    batch_specs = [CHAPTER1_SPEC_MAP[key] for key in batch_keys if key in CHAPTER1_SPEC_MAP]
    messages = [
        {
            "role": "system",
            "content": (
                "你是产业研究分析师。"
                "当前任务是分批生成第一章章节。"
                "只输出 JSON，不要解释，不要 Markdown 代码块。"
                "不得编造企业私有信息。"
            ),
        },
        {
            "role": "user",
            "content": _build_chapter1_batch_prompt(
                product_name=product_name,
                batch_specs=batch_specs,
                generated_sections=generated_sections,
            ),
        },
    ]
    raw = client.complete(
        messages,
        model=model,
        temperature=0.15,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        retry_max_attempts=1,
    )

    warning = ""
    raw_sections: Any
    try:
        parsed = _extract_json_payload(raw)
        raw_sections = parsed.get("sections")
        if not isinstance(raw_sections, list):
            raise OtherProofError("第一章 JSON 缺少 sections 数组")
    except OtherProofError:
        raw_sections, warning = _coerce_chapter1_sections_from_text(raw)
    aligned_sections, align_warning = _align_batch_chapter1_sections(raw_sections, batch_keys)
    if align_warning:
        warning = f"{warning}；{align_warning}" if warning else align_warning
    return aligned_sections, warning


def _align_batch_chapter1_sections(raw_sections: Any, batch_keys: Sequence[str]) -> tuple[List[Dict[str, Any]], str]:
    if not isinstance(raw_sections, list):
        return [], ""

    cleaned: List[Dict[str, Any]] = []
    valid_key_count = 0
    explicit_key_count = 0
    for item in raw_sections:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key:
            explicit_key_count += 1
        title = str(item.get("title") or "").strip()
        paragraphs = item.get("paragraphs")
        if not isinstance(paragraphs, list):
            paragraphs = [paragraphs] if paragraphs else []
        paragraph_list = [str(text).strip() for text in paragraphs if str(text).strip()]
        if not paragraph_list:
            continue
        if key in batch_keys:
            valid_key_count += 1
        cleaned.append({"key": key, "title": title, "paragraphs": paragraph_list})

    if not cleaned:
        return [], ""

    filtered = [item for item in cleaned if str(item.get("key") or "").strip() in batch_keys]
    if filtered:
        return filtered, ""

    # 返回里有明确 key 但全部不属于当前批次时，直接判定为错批，避免标题正文错配。
    if explicit_key_count > 0 and valid_key_count == 0:
        return [], "第一章分批返回的小节与当前批次不匹配，已丢弃该批内容"

    expected_len = len(batch_keys)
    if len(cleaned) < expected_len or valid_key_count >= expected_len:
        return [], ""

    remapped: List[Dict[str, Any]] = []
    for idx, key in enumerate(batch_keys):
        if idx >= len(cleaned):
            break
        spec = CHAPTER1_SPEC_MAP.get(key)
        if not spec:
            continue
        source = cleaned[idx]
        remapped.append(
            {
                "key": key,
                "title": spec["title"],
                "paragraphs": list(source["paragraphs"]),
            }
        )
    if remapped:
        return remapped, "第一章分批返回的 key 不完整，系统已按目录顺序重排"
    return [], ""


def generate_other_chapter1_section(
    product_name: str,
    section_key: str,
    generated_sections: Sequence[Dict[str, Any]],
    config: InferenceConfig,
) -> Dict[str, Any]:
    if not product_name or not product_name.strip():
        raise OtherProofError("主导产品名称不能为空")
    normalized_key = str(section_key or "").strip()
    spec = CHAPTER1_SPEC_MAP.get(normalized_key)
    if not spec:
        raise OtherProofError("第一章小节标识无效")

    orchestrator = LLMOrchestrator.from_config(config)
    if not orchestrator.is_available() or orchestrator.client is None:
        raise OtherProofError("LLM 未配置，无法生成他证第一章")

    product = product_name.strip()
    # 兼容手动按小节重生：同样不设置超时，避免慢模型中断。
    chapter1_timeout_seconds = 0
    chapter1_model = _resolve_chapter1_model_name(
        config.llm_model,
        getattr(orchestrator.client, "model", "") or config.llm_model,
    )
    chapter1_max_output_tokens = max(1200, min(int(config.llm_max_output_tokens), 2400))
    if _is_reasoner_model(chapter1_model):
        chapter1_max_output_tokens = 2400

    normalized_generated, _ = normalize_chapter1_sections(list(generated_sections or []))
    section_raw, section_warning = _generate_chapter1_section(
        client=orchestrator.client,
        product_name=product,
        spec=spec,
        model=chapter1_model,
        timeout_seconds=chapter1_timeout_seconds,
        max_output_tokens=chapter1_max_output_tokens,
        generated_sections=normalized_generated,
    )
    normalized_section_list, normalize_warnings = normalize_chapter1_sections([section_raw])
    section = next(
        (
            item
            for item in normalized_section_list
            if str(item.get("key") or "").strip() == spec["key"]
        ),
        section_raw,
    )
    warnings: List[str] = []
    if section_warning:
        warnings.append(f"第一章《{spec['title']}》{section_warning}")
    current_title_prefix = f"第一章《{spec['title']}》"
    warnings.extend(
        item
        for item in normalize_warnings
        if current_title_prefix in str(item or "")
    )
    return {"section": section, "warnings": warnings}


def _generate_chapter1_section(
    *,
    client: Any,
    product_name: str,
    spec: Dict[str, Any],
    model: str,
    timeout_seconds: int,
    max_output_tokens: int,
    generated_sections: Sequence[Dict[str, Any]],
) -> tuple[Dict[str, Any], str]:
    section_key = str(spec["key"])
    section_title = str(spec["title"])
    section_messages = [
        {
            "role": "system",
            "content": (
                "你是产业研究分析师。"
                "当前仅生成一个章节。"
                "只输出 JSON，不要解释，不要 Markdown 代码块。"
                "不得编造企业私有信息。"
            ),
        },
        {
            "role": "user",
            "content": _build_chapter1_section_prompt(
                product_name=product_name,
                spec=spec,
                generated_sections=generated_sections,
            ),
        },
    ]
    raw = client.complete(
        section_messages,
        model=model,
        temperature=0.2,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        retry_max_attempts=0,
        section_key=section_key,
    )
    warning = ""
    paragraphs: List[str] = []
    try:
        parsed = _extract_json_payload(raw)
        paragraphs = _extract_section_paragraphs(parsed, section_key)
    except OtherProofError:
        paragraphs, warning = _coerce_chapter1_section_paragraphs_from_text(raw)

    cleaned = [str(item).strip() for item in paragraphs if str(item).strip()]
    if not cleaned:
        raise RuntimeError(f"第一章《{section_title}》返回为空")
    return {"key": section_key, "title": section_title, "paragraphs": cleaned}, warning


def _resolve_chapter1_model_name(config_model: str, fallback_model: str) -> str:
    model = str(config_model or fallback_model or "").strip()
    lower = model.lower()
    # 第一章是整章一次性输出，R1/Reasoner 在该场景下耗时明显更高，容易触发网关断连。
    if lower in {"deepseek-r1", "deepseek-reasoner"}:
        return "deepseek-chat"
    return model or fallback_model


def _is_reasoner_model(model_name: str) -> bool:
    model = str(model_name or "").strip().lower()
    return model in {"deepseek-reasoner", "deepseek-r1"}



def normalize_chapter1_sections(raw_sections: Any) -> tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    normalized_map: Dict[str, List[str]] = {}

    if isinstance(raw_sections, dict):
        iterable = []
        for key, value in raw_sections.items():
            iterable.append({"key": key, "paragraphs": value})
    elif isinstance(raw_sections, list):
        iterable = raw_sections
    else:
        iterable = []

    for item in iterable:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        title = str(item.get("title") or "").strip()
        paragraphs = item.get("paragraphs")
        if not key:
            key = _key_from_title(title)
        if key not in CHAPTER1_SPEC_MAP:
            continue
        if not isinstance(paragraphs, list):
            paragraphs = [paragraphs] if paragraphs else []
        cleaned = [str(x).strip() for x in paragraphs if str(x).strip()]
        cleaned = _sanitize_chapter1_raw_paragraphs(cleaned)
        normalized_map[key] = cleaned

    normalized_sections: List[Dict[str, Any]] = []
    for spec in CHAPTER1_SECTION_SPECS:
        key = spec["key"]
        title = spec["title"]
        slot_count = spec["slot_count"]
        source_paragraphs = list(normalized_map.get(key, []))
        if key == "industry_environment":
            paragraphs = _sanitize_industry_environment_paragraphs(source_paragraphs)
        elif key == "industry_trends":
            paragraphs = _sanitize_industry_trends_paragraphs(source_paragraphs)
        else:
            paragraphs = _merge_heading_like_paragraphs(source_paragraphs)
        if key == "industry_supply_chain":
            paragraphs = _ensure_supply_chain_subsections(paragraphs)
        if not paragraphs:
            warnings.append(f"第一章《{title}》未生成成功，已写入占位内容")
            paragraphs = [PLACEHOLDER_TEXT]
        if key == "industry_supply_chain":
            paragraphs, section_warnings = _fit_supply_chain_paragraphs_to_slot_count(paragraphs, title)
        else:
            paragraphs, section_warnings = _fit_paragraphs_to_slot_count(paragraphs, slot_count, title)
        paragraphs = [_clean_chapter1_paragraph_text(paragraph) or PLACEHOLDER_TEXT for paragraph in paragraphs]
        warnings.extend(section_warnings)
        normalized_sections.append({"key": key, "title": title, "paragraphs": paragraphs})

    return normalized_sections, _unique_preserve_order(warnings)


def _sanitize_chapter1_raw_paragraphs(paragraphs: Sequence[str]) -> List[str]:
    cleaned: List[str] = []
    for raw in paragraphs:
        text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            continue
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r'^\s*key\s*[:：]\s*[^,，]+[,，]\s*', "", text, flags=re.I)
        text = re.sub(r'^\s*title\s+(.+?)\s+paragraphs\s*[：:]\s*', r"\1：", text, flags=re.I)
        text = re.sub(r'^\s*title\s*[:：]\s*', "", text, flags=re.I)
        text = re.sub(r'^\s*paragraphs\s*[:：]\s*', "", text, flags=re.I)
        text = re.sub(r'^\s*sections?\s*[:：]\s*', "", text, flags=re.I)
        text = _clean_chapter1_paragraph_text(text)
        token_probe = re.sub(r"[\s\[\]\{\}\"'_,:：\-]+", "", text).lower()
        if token_probe in {"title", "key", "paragraphs", "sections", "section", "json"}:
            continue
        if _is_chapter1_instruction_placeholder(text):
            continue
        if not text.strip():
            continue
        cleaned.append(text.strip())
    return cleaned


def _clean_chapter1_paragraph_text(text: Any) -> str:
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"([。！？；])\s*[,，]\s*[\"'“”‘’]*", r"\1", cleaned)
    cleaned = re.sub(r"^[\s,，、;；:：。.!?！？\"'“”‘’]+", "", cleaned)
    cleaned = re.sub(r"[\s\"'“”‘’]+$", "", cleaned)
    cleaned = re.sub(r'^\s*key\s*[:：]\s*[^,，]+[,，]\s*', "", cleaned, flags=re.I)
    cleaned = re.sub(r'^\s*title\s+(.+?)\s+paragraphs\s*[：:]\s*', r"\1：", cleaned, flags=re.I)
    cleaned = re.sub(r'^\s*title\s*[:：]\s*', "", cleaned, flags=re.I)
    cleaned = re.sub(r'^\s*paragraphs\s*[:：]\s*', "", cleaned, flags=re.I)
    cleaned = re.sub(r'^\s*sections?\s*[:：]\s*', "", cleaned, flags=re.I)
    return cleaned.strip()


def _is_chapter1_instruction_placeholder(text: Any) -> bool:
    normalized = str(text or "").strip()
    return (
        normalized.startswith("该部分用于说明")
        or "请结合公开行业资料补充" in normalized
        or "待补充" in normalized
    )


def _is_chapter1_placeholder_text(text: Any) -> bool:
    normalized = str(text or "").strip()
    return (
        not normalized
        or normalized == PLACEHOLDER_TEXT
        or normalized.startswith("该部分生成失败")
        or _is_chapter1_instruction_placeholder(normalized)
    )



def lookup_other_companies(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        raise OtherProofError("企业列表不能为空")

    resolved: List[Dict[str, Any]] = []
    for item in items:
        requested_name = str(item.get("company_name") or "").strip()
        if not requested_name:
            continue
        resolved.append(_lookup_company_profile_via_qcc_browser(requested_name))

    return {
        "status": "resolved",
        "resolved": resolved,
        "pending": [],
    }



def generate_other_docx(data: Dict[str, Any], template_path: str | Path, output_path: str | Path) -> List[str]:
    warnings: List[str] = []
    proof_scope = str(data.get("proof_scope") or "").strip()
    market_name = str(data.get("market_name") or "").strip()
    if not proof_scope:
        raise OtherProofError("证明范围不能为空")
    if not market_name:
        raise OtherProofError("测算市场名称不能为空")

    raw_sources = [source for source in (data.get("sources") or []) if isinstance(source, dict)]
    try:
        chart_series = build_chart_series_from_sources(raw_sources, context_label="数据来源")
    except ChartDataError as exc:
        raise OtherProofError(str(exc)) from exc

    raw_chapter2_layers = [layer for layer in (data.get("chapter2_layers") or []) if isinstance(layer, dict)]
    chapter2_layers = _bind_other_layers_to_sources(raw_chapter2_layers, raw_sources)
    if not chapter2_layers:
        raise OtherProofError("第二章市场层级不能为空")

    competitors = [row for row in (data.get("competitors") or []) if isinstance(row, dict) and str(row.get("name") or "").strip()]
    raw_profiles = [profile for profile in (data.get("resolved_company_profiles") or []) if isinstance(profile, dict)]
    validated_profiles = _validate_manual_company_profiles(
        company_name=str(data.get("company_name") or "").strip(),
        competitors=competitors,
        profiles=raw_profiles,
    )

    company_rows = _build_company_rows(data, validated_profiles, competitors, market_name, proof_scope, warnings)
    sorted_rows = sorted(company_rows, key=lambda row: row["share25_value"], reverse=True)
    self_row = next((row for row in sorted_rows if row["is_self"]), None)
    if self_row is None:
        raise OtherProofError("未找到申报企业的已确认资料")

    rank_map = _build_year_rank_map(sorted_rows)
    layer_count = len(chapter2_layers)
    company_count = len(sorted_rows)
    skip_chapter1 = bool(data.get("skip_chapter1"))
    if skip_chapter1:
        chapter1_sections = []
    else:
        chapter1_sections, chapter1_warnings = normalize_chapter1_sections(data.get("chapter1_sections"))
        warnings.extend(chapter1_warnings)

    with zipfile.ZipFile(template_path, "r") as archive:
        xml_content = archive.read("word/document.xml")
        file_map = {name: archive.read(name) for name in archive.namelist()}

    _register_all_namespaces(xml_content)
    root = ET.fromstring(xml_content)
    metadata = _prepare_other_structure(root, layer_count=layer_count, company_count=company_count)
    field_count = _count_highlight_fields(root)
    expected_field_count = 154 + 3 * layer_count + 20 * company_count
    if field_count != expected_field_count:
        raise OtherProofError(f"他证模板字段数量异常：期望 {expected_field_count}，实际 {field_count}")

    values = _build_other_values(
        data=data,
        sorted_rows=sorted_rows,
        self_row=self_row,
        rank_map=rank_map,
        proof_scope=proof_scope,
        market_name=market_name,
        chapter2_layers=chapter2_layers,
        chapter1_sections=chapter1_sections,
        skip_chapter1=skip_chapter1,
        warnings=warnings,
    )
    field_paragraphs = _replace_highlight_fields(root, values)
    _remove_all_yellow_highlights(root)

    _postprocess_other_document(
        root=root,
        metadata=metadata,
        sorted_rows=sorted_rows,
        rank_map=rank_map,
        proof_scope=proof_scope,
        market_name=market_name,
        chapter2_layers=chapter2_layers,
        report_date=_report_date_from_payload(data),
        self_row=self_row,
        product_name=str(data.get("product_name") or "").strip(),
        warnings=warnings,
    )
    _rewrite_summary_market_research_phrase(root, str(data.get("product_name") or "").strip())
    _rewrite_other_toc_titles(
        root=root,
        product_name=str(data.get("product_name") or "").strip(),
        chapter2_layers=chapter2_layers,
        sorted_rows=sorted_rows,
        self_company_name=self_row["display_name"],
    )
    if skip_chapter1:
        _remove_chapter1_body_and_toc(root)
    else:
        _ensure_chapter1_environment_heading(root, field_paragraphs)
        _normalize_chapter1_body_paragraphs_and_styles(field_paragraphs)
        _compress_chapter1_visual_paragraphs(root, field_paragraphs)
    _apply_body_plain_paragraph_justification(root)
    _set_signature_block_right_alignment(root)
    _remove_section_page_number_restart(root)
    _rewrite_other_header_titles(
        file_map=file_map,
        company_name=self_row["display_name"],
        product_name=str(data.get("product_name") or "").strip(),
    )
    _enable_word_update_fields_on_open(file_map)
    _mark_footer_page_fields_dirty(file_map)
    try:
        inject_market_charts_into_docx(
            document_root=root,
            file_map=file_map,
            chart_series=chart_series,
            context_label="他证",
        )
    except ChartDataError as exc:
        raise OtherProofError(str(exc)) from exc

    file_map["word/document.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, blob in file_map.items():
            archive.writestr(name, blob)

    return _unique_preserve_order(warnings)


def _bind_other_layers_to_sources(
    chapter2_layers: Sequence[Dict[str, Any]],
    sources: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    normalized_sources = [source for source in sources if isinstance(source, dict)]
    if len(normalized_sources) != len(chapter2_layers):
        raise OtherProofError(
            f"自证来源层数与他证层级不一致：第二章共 {len(chapter2_layers)} 层，但数据来源有 {len(normalized_sources)} 层"
        )

    bound_layers: List[Dict[str, Any]] = []
    for index, layer in enumerate(chapter2_layers, start=1):
        layer_name = str(layer.get("name") or "").strip()
        if not layer_name:
            raise OtherProofError(f"第二章第 {index} 层市场名称不能为空")

        source = normalized_sources[index - 1]
        analysis = str(source.get("analysis") or "").strip()
        source_urls = _normalize_source_values(source.get("urls"))
        fallback_url = str(source.get("url") or "").strip()
        if fallback_url and fallback_url not in source_urls:
            source_urls.insert(0, fallback_url)
        url = source_urls[0] if source_urls else ""
        if not analysis:
            raise OtherProofError(f"自证第 {index} 层来源正文不能为空")
        if not url:
            raise OtherProofError(f"自证第 {index} 层来源链接不能为空")

        bound_layers.append(
            {
                "name": layer_name,
                "analysis": analysis,
                "url": url,
                "urls": source_urls,
                "chart_2023": str(source.get("chart_2023") or "").strip(),
                "chart_2024": str(source.get("chart_2024") or "").strip(),
                "chart_2025": str(source.get("chart_2025") or "").strip(),
            }
        )
    return bound_layers


def _normalize_source_values(raw: Any) -> List[str]:
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


def _build_company_rows(
    data: Dict[str, Any],
    resolved_profiles: Sequence[Dict[str, Any]],
    competitors: Sequence[Dict[str, Any]],
    market_name: str,
    proof_scope: str,
    warnings: List[str],
) -> List[Dict[str, Any]]:
    profile_by_requested = {}
    for profile in resolved_profiles:
        requested_name = str(profile.get("requested_name") or "").strip()
        if requested_name:
            profile_by_requested[requested_name] = profile

    sale_23 = _require_number_text(data.get("sale_23"), "申报企业 2023 年销售额")
    sale_24 = _require_number_text(data.get("sale_24"), "申报企业 2024 年销售额")
    sale_25 = _require_number_text(data.get("sale_25"), "申报企业 2025 年销售额")
    total_23 = _require_number(data.get("total_mkt_23"), "2023 年市场规模")
    total_24 = _require_number(data.get("total_mkt_24"), "2024 年市场规模")
    total_25 = _require_number(data.get("total_mkt_25"), "2025 年市场规模")

    self_requested_name = str(data.get("company_name") or "").strip()
    self_profile = profile_by_requested.get(self_requested_name)
    if self_profile is None:
        raise OtherProofError(f"企业“{self_requested_name}”尚未完成企业资料确认")

    self_pct_23 = _normalize_percent_text(data.get("pct_23"), sale_23, total_23, "申报企业 2023 年占有率")
    self_pct_24 = _normalize_percent_text(data.get("pct_24"), sale_24, total_24, "申报企业 2024 年占有率")
    self_pct_25 = _normalize_percent_text(data.get("pct_25"), sale_25, total_25, "申报企业 2025 年占有率")

    rows = [
        _build_company_row(
            profile=self_profile,
            requested_name=self_requested_name,
            is_self=True,
            market_name=market_name,
            proof_scope=proof_scope,
            sale_23=sale_23,
            sale_24=sale_24,
            sale_25=sale_25,
            pct_23=self_pct_23,
            pct_24=self_pct_24,
            pct_25=self_pct_25,
            warnings=warnings,
        )
    ]

    for competitor in competitors:
        requested_name = str(competitor.get("name") or "").strip()
        if not requested_name:
            continue
        profile = profile_by_requested.get(requested_name)
        if profile is None:
            raise OtherProofError(f"企业“{requested_name}”尚未完成企业资料确认")
        pct_23 = _normalize_percent_only(competitor.get("p23"), f"{requested_name} 2023 年占有率")
        pct_24 = _normalize_percent_only(competitor.get("p24"), f"{requested_name} 2024 年占有率")
        pct_25 = _normalize_percent_only(competitor.get("p25"), f"{requested_name} 2025 年占有率")
        sale_23_value = total_23 * _percent_to_ratio(pct_23)
        sale_24_value = total_24 * _percent_to_ratio(pct_24)
        sale_25_value = total_25 * _percent_to_ratio(pct_25)
        rows.append(
            _build_company_row(
                profile=profile,
                requested_name=requested_name,
                is_self=False,
                market_name=market_name,
                proof_scope=proof_scope,
                sale_23=_format_amount(sale_23_value),
                sale_24=_format_amount(sale_24_value),
                sale_25=_format_amount(sale_25_value),
                pct_23=pct_23,
                pct_24=pct_24,
                pct_25=pct_25,
                warnings=warnings,
            )
        )

    return rows


def _validate_manual_company_profiles(
    *,
    company_name: str,
    competitors: Sequence[Dict[str, Any]],
    profiles: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not company_name:
        raise OtherProofError("企业名称不能为空")
    if not profiles:
        raise OtherProofError("请先填写第三章企业基本信息")

    expected_names = [company_name]
    for competitor in competitors:
        name = str(competitor.get("name") or "").strip()
        if name and name not in expected_names:
            expected_names.append(name)

    profile_by_requested: Dict[str, Dict[str, Any]] = {}
    for profile in profiles:
        requested_name = str(profile.get("requested_name") or profile.get("company_name") or "").strip()
        if requested_name:
            profile_by_requested[requested_name] = dict(profile)

    required_fields = [
        ("registered_capital", "注册资本"),
        ("established_date", "成立日期"),
        ("legal_representative", "法人代表"),
        ("company_address", "企业地址"),
        ("main_business", "主营业务"),
    ]

    validated: List[Dict[str, Any]] = []
    for expected_name in expected_names:
        profile = profile_by_requested.get(expected_name)
        if profile is None:
            raise OtherProofError(f"请先填写“{expected_name}”的第三章企业基本信息")
        profile["requested_name"] = expected_name
        profile["company_name"] = str(profile.get("company_name") or expected_name).strip() or expected_name
        profile["company_url"] = str(profile.get("company_url") or "").strip()
        for key, label in required_fields:
            value = str(profile.get(key) or "").strip()
            if not value:
                raise OtherProofError(f"请先填写“{expected_name}”的{label}")
            profile[key] = value
        validated.append(profile)
    return validated



def _build_company_row(
    *,
    profile: Dict[str, Any],
    requested_name: str,
    is_self: bool,
    market_name: str,
    proof_scope: str,
    sale_23: str,
    sale_24: str,
    sale_25: str,
    pct_23: str,
    pct_24: str,
    pct_25: str,
    warnings: List[str],
) -> Dict[str, Any]:
    company_name = str(profile.get("company_name") or requested_name).strip() or requested_name
    registered_capital = _profile_field(profile, "registered_capital", company_name, "注册资本", warnings)
    established_date = _profile_field(profile, "established_date", company_name, "成立日期", warnings)
    legal_representative = _profile_field(profile, "legal_representative", company_name, "法人代表", warnings)
    company_address = _profile_field(profile, "company_address", company_name, "企业地址", warnings)
    main_business = _profile_field(profile, "main_business", company_name, "主营业务", warnings)
    return {
        "requested_name": requested_name,
        "display_name": company_name,
        "company_url": str(profile.get("company_url") or "").strip(),
        "is_self": is_self,
        "market_name": market_name,
        "proof_scope": proof_scope,
        "registered_capital": registered_capital,
        "established_date": established_date,
        "legal_representative": legal_representative,
        "company_address": company_address,
        "main_business": main_business,
        "sale23": sale_23,
        "sale24": sale_24,
        "sale25": sale_25,
        "pct23": pct_23,
        "pct24": pct_24,
        "pct25": pct_25,
        "share23_value": _percent_value(pct_23),
        "share24_value": _percent_value(pct_24),
        "share25_value": _percent_value(pct_25),
    }



def _build_year_rank_map(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    result: Dict[str, Dict[str, int]] = {"2023": {}, "2024": {}, "2025": {}}
    year_fields = {"2023": "share23_value", "2024": "share24_value", "2025": "share25_value"}
    for year, key in year_fields.items():
        ranked = sorted(rows, key=lambda row: row[key], reverse=True)
        for index, row in enumerate(ranked, start=1):
            result[year][row["display_name"]] = index
    return result



def _build_other_values(
    *,
    data: Dict[str, Any],
    sorted_rows: Sequence[Dict[str, Any]],
    self_row: Dict[str, Any],
    rank_map: Dict[str, Dict[str, int]],
    proof_scope: str,
    market_name: str,
    chapter2_layers: Sequence[Dict[str, Any]],
    chapter1_sections: Sequence[Dict[str, Any]],
    skip_chapter1: bool,
    warnings: List[str],
) -> List[str]:
    report_date = _report_date_from_payload(data)
    chapter1_slots = [""] * EXPECTED_CHAPTER1_SLOT_COUNT if skip_chapter1 else _flatten_chapter1_slots(chapter1_sections)
    if len(chapter1_slots) != EXPECTED_CHAPTER1_SLOT_COUNT:
        raise OtherProofError(f"第一章段落数量异常：期望 {EXPECTED_CHAPTER1_SLOT_COUNT}，实际 {len(chapter1_slots)}")
    if not skip_chapter1 and any(_is_chapter1_placeholder_text(item) for item in chapter1_slots):
        raise OtherProofError("第一章仍存在未完成内容，请重新生成第一章后再导出 Word")

    self_rank_23 = rank_map["2023"][self_row["display_name"]]
    self_rank_24 = rank_map["2024"][self_row["display_name"]]
    self_rank_25 = rank_map["2025"][self_row["display_name"]]
    self_share_23 = self_row["pct23"]
    self_share_24 = self_row["pct24"]
    self_share_25 = self_row["pct25"]

    company_name = self_row["display_name"]
    product_name = str(data.get("product_name") or "").strip()
    product_code = str(data.get("product_code") or "").strip()
    company_product = f"{company_name}{product_name}"
    scope_rank_plain = f"{proof_scope}{_ordinal_plain(self_rank_25)}"
    chapter4_name_list = "、".join(row["display_name"] for row in sorted_rows)

    values: List[str] = [
        company_name,
        product_name,
        f"{report_date.year} 年 {report_date.month} 月",
        company_product,
        product_name,
        product_code,
        self_share_23,
        _scope_rank_parenthesized(proof_scope, self_rank_23),
        self_share_24,
        _scope_rank_parenthesized(proof_scope, self_rank_24),
        self_share_25,
        _scope_rank_parenthesized(proof_scope, self_rank_25),
        _format_cn_date(report_date),
        str(data.get("company_intro_text") or PLACEHOLDER_TEXT).strip() or PLACEHOLDER_TEXT,
        company_product,
        product_name,
        f"{company_name}，{product_name}{proof_scope}",
        self_share_25,
        scope_rank_plain,
        product_name,
    ]
    values.extend(chapter1_slots)

    for index, layer in enumerate(chapter2_layers, start=1):
        layer_name = str(layer.get("name") or "").strip()
        analysis = str(layer.get("analysis") or "").strip()
        if not layer_name:
            raise OtherProofError(f"第二章第 {index} 层市场名称不能为空")
        if not analysis:
            warnings.append(f"第二章第 {index} 层正文为空，已写入占位内容")
            analysis = PLACEHOLDER_TEXT
        values.extend([layer_name, analysis, layer_name])

    for row in sorted_rows:
        values.extend(
            [
                row["display_name"],
                row["display_name"],
                row["registered_capital"],
                row["established_date"],
                row["legal_representative"],
                row["company_address"],
                row["main_business"],
                row["display_name"],
                market_name,
                proof_scope,
                row["sale23"],
                row["sale24"],
                row["sale25"],
            ]
        )

    values.extend(
        [
            company_product,
            proof_scope,
            market_name,
            proof_scope,
            chapter4_name_list,
            str(len(sorted_rows)),
            market_name,
            proof_scope,
        ]
    )

    for row in sorted_rows:
        values.extend(
            [
                row["display_name"],
                row["sale23"],
                row["pct23"],
                row["sale24"],
                row["pct24"],
                row["sale25"],
                row["pct25"],
            ]
        )

    values.extend(
        [
            company_name,
            product_name,
            f"{self_share_23}、{self_share_24}、{self_share_25}",
            company_name,
            product_name,
            company_product,
            proof_scope,
            company_name,
            product_name,
            scope_rank_plain,
            company_product,
            company_name,
            product_name,
            self_share_23,
            self_share_24,
            self_share_25,
            _ordinal_with_suffix(self_rank_23),
            _ordinal_with_suffix(self_rank_24),
            _ordinal_with_suffix(self_rank_25),
            market_name,
            market_name,
        ]
    )

    return values



def _prepare_other_structure(root: ET.Element, *, layer_count: int, company_count: int) -> Dict[str, Any]:
    body = root.find(".//w:body", NS)
    if body is None:
        raise OtherProofError("模板缺少正文主体")

    if layer_count < 1:
        raise OtherProofError("第二章市场层级至少需要 1 层")
    if company_count < 1:
        raise OtherProofError("至少需要 1 家企业数据")

    _apply_layer_structure(body, layer_count)
    layer_delta = _layer_child_delta(layer_count)
    company_base_start = 201 + layer_delta
    _apply_company_block_structure(body, company_base_start, company_count)
    company_delta = 7 * (company_count - 4)
    comparison_table_index = 235 + layer_delta + company_delta
    _apply_comparison_table_structure(body, comparison_table_index, company_count)

    layer_starts = _layer_start_indices(layer_count)
    company_starts = [company_base_start + 7 * idx for idx in range(company_count)]
    chart9_table_index = 242 + layer_delta + company_delta
    chapter5_execution_index = 248 + layer_delta + company_delta
    chapter5_links_anchor_index = 258 + layer_delta + company_delta
    return {
        "layer_count": layer_count,
        "company_count": company_count,
        "layer_starts": layer_starts,
        "company_starts": company_starts,
        "comparison_table_index": comparison_table_index,
        "chart8_title_index": comparison_table_index - 1,
        "chart9_table_index": chart9_table_index,
        "chapter5_execution_index": chapter5_execution_index,
        "chapter5_links_anchor_index": chapter5_links_anchor_index,
    }



def _apply_layer_structure(body: ET.Element, layer_count: int) -> None:
    children = list(body)
    block2 = children[187:192]
    block3 = children[192:199]

    if layer_count == 1:
        for elem in children[187:199]:
            body.remove(elem)
        return
    if layer_count == 2:
        for elem in children[192:199]:
            body.remove(elem)
        return
    if layer_count == 3:
        return

    insert_index = 199
    for _ in range(layer_count - 3):
        for elem in block3:
            body.insert(insert_index, copy.deepcopy(elem))
            insert_index += 1



def _layer_child_delta(layer_count: int) -> int:
    if layer_count == 1:
        return -12
    if layer_count == 2:
        return -7
    return 7 * (layer_count - 3)



def _layer_start_indices(layer_count: int) -> List[int]:
    starts = [182]
    if layer_count >= 2:
        starts.append(187)
    if layer_count >= 3:
        starts.append(192)
    if layer_count >= 4:
        for index in range(4, layer_count + 1):
            starts.append(192 + 7 * (index - 3))
    return starts



def _apply_company_block_structure(body: ET.Element, base_start: int, company_count: int) -> None:
    children = list(body)
    last_block = children[base_start + 21 : base_start + 28]
    if company_count < 4:
        remove_start = base_start + 7 * company_count
        remove_end = base_start + 28
        for elem in children[remove_start:remove_end]:
            body.remove(elem)
        return
    if company_count == 4:
        return
    insert_index = base_start + 28
    for _ in range(company_count - 4):
        for elem in last_block:
            body.insert(insert_index, copy.deepcopy(elem))
            insert_index += 1



def _apply_comparison_table_structure(body: ET.Element, table_index: int, company_count: int) -> None:
    table = list(body)[table_index]
    rows = table.findall("./w:tr", NS)
    if len(rows) < 6:
        raise OtherProofError("第四章对比表结构异常")
    data_rows = rows[2:]
    if company_count < len(data_rows):
        for row in data_rows[company_count:]:
            table.remove(row)
        return
    if company_count == len(data_rows):
        return
    template_row = data_rows[-1]
    for _ in range(company_count - len(data_rows)):
        table.append(copy.deepcopy(template_row))



def _postprocess_other_document(
    *,
    root: ET.Element,
    metadata: Dict[str, Any],
    sorted_rows: Sequence[Dict[str, Any]],
    rank_map: Dict[str, Dict[str, int]],
    proof_scope: str,
    market_name: str,
    chapter2_layers: Sequence[Dict[str, Any]],
    report_date: date,
    self_row: Dict[str, Any],
    product_name: str,
    warnings: List[str],
) -> None:
    body = root.find(".//w:body", NS)
    if body is None:
        raise OtherProofError("模板缺少正文主体")
    children = list(body)

    _set_paragraph_text(
        children[56],
        f"测算 2023-2025年各企业的市场占有率，并对销售额进行排名，确定{self_row['display_name']}{product_name}在{proof_scope}市场的名次。",
    )
    chart_plan = _build_chart_number_plan(
        layer_count=metadata["layer_count"],
        company_count=metadata["company_count"],
    )

    for idx, start in enumerate(metadata["layer_starts"], start=1):
        heading_index = start
        chart_index = start + (2 if idx < 3 else 3)
        source_index = start + (4 if idx < 3 else 5)
        layer = chapter2_layers[idx - 1]
        layer_name = str(layer.get("name") or "").strip()
        _set_paragraph_text(children[heading_index], f"{_section_index_cn(idx)}、{layer_name}市场情况分析")
        _set_paragraph_text(
            children[chart_index],
            f"图表{chart_plan['layer'][idx - 1]}：2023-2025年{layer_name}市场规模（亿元）",
        )
        _set_paragraph_text(children[source_index], f"数据来源：算路科技整理（见链接{idx}）")

    for idx, start in enumerate(metadata["company_starts"], start=1):
        row = sorted_rows[idx - 1]
        _set_paragraph_text(children[start], f"{_section_index_cn(idx)}、主导产品企业分析——{row['display_name']}")
        _set_paragraph_text(
            children[start + 2],
            f"图表{chart_plan['company'][idx - 1]}：{row['display_name']}",
        )

    _set_paragraph_text(
        children[metadata["chart8_title_index"]],
        f"图表{chart_plan['comparison']}：2023-2025年{proof_scope}主导企业{proof_scope}销售额及占有率排名情况",
    )

    execution_start = report_date - timedelta(days=10)
    _set_paragraph_text(
        children[metadata["chapter5_execution_index"]],
        f"项目执行周期：本项目数据更新从 {_format_cn_date(execution_start)}至 {_format_cn_date(report_date)}，共执行 10 天；",
    )

    _rewrite_chapter5_links(
        body=body,
        anchor_index=metadata["chapter5_links_anchor_index"],
        links=[str(layer.get("url") or "").strip() for layer in chapter2_layers],
        warnings=warnings,
    )
    _rewrite_dynamic_chart_references(
        children=children,
        chart_plan=chart_plan,
        self_row=self_row,
        rank_map=rank_map,
        proof_scope=proof_scope,
        product_name=product_name,
    )
    _highlight_self_row_in_comparison_table(
        body=body,
        table_index=metadata["comparison_table_index"],
        self_company_name=self_row["display_name"],
    )
    _rewrite_chart9_labels(children[metadata["chart9_table_index"]], proof_scope)


def _build_chart_number_plan(*, layer_count: int, company_count: int) -> Dict[str, Any]:
    layer_numbers = list(range(1, layer_count + 1))
    company_start = layer_count + 1
    company_numbers = list(range(company_start, company_start + company_count))
    comparison_number = company_start + company_count
    share_chart_number = comparison_number + 1
    chapter5_source_chart_number = share_chart_number + 1
    return {
        "layer": layer_numbers,
        "company": company_numbers,
        "comparison": comparison_number,
        "share": share_chart_number,
        "chapter5_source": chapter5_source_chart_number,
    }


def _rewrite_dynamic_chart_references(
    *,
    children: Sequence[ET.Element],
    chart_plan: Dict[str, Any],
    self_row: Dict[str, Any],
    rank_map: Dict[str, Dict[str, int]],
    proof_scope: str,
    product_name: str,
) -> None:
    self_rank_25 = rank_map["2025"][self_row["display_name"]]
    scope_rank_plain = f"{proof_scope}{_ordinal_plain(self_rank_25)}"
    chapter4_dynamic_conclusion = (
        f"由以上分析可知，2023年至2025年，{self_row['display_name']}的{product_name}市场占有率分别为："
        f"{self_row['pct23']}、{self_row['pct24']}、{self_row['pct25']}。 因此，算路科技认为，"
        f"{self_row['display_name']}“{product_name}市场占有率{scope_rank_plain}”的市场地位结论成立。"
    )

    for child in children:
        if child.tag != f"{{{NS['w']}}}p":
            continue
        text = _get_paragraph_text(child)
        if not text:
            continue

        if "销售额如图表" in text:
            updated = re.sub(r"图表\d+", f"图表{chart_plan['comparison']}", text, count=1)
            _set_paragraph_text(child, updated)
            continue

        if text.startswith("图表") and "市场占有率" in text and self_row["display_name"] in text:
            updated = re.sub(r"^图表\d+", f"图表{chart_plan['share']}", text, count=1)
            _set_paragraph_text(child, updated)
            continue

        if "政府端数据来源" in text and text.startswith("图表"):
            updated = re.sub(r"^图表\d+", f"图表{chart_plan['chapter5_source']}", text, count=1)
            _set_paragraph_text(child, updated)
            continue

        if "由以上分析可知" in text and "市场地位结论成立" in text:
            _set_paragraph_text(child, chapter4_dynamic_conclusion)


def _rewrite_other_header_titles(file_map: Dict[str, bytes], company_name: str, product_name: str) -> None:
    product_title = f"{product_name}市场占有率证明报告"
    combined_title = f"{company_name}{product_title}"
    for name, blob in list(file_map.items()):
        if not name.startswith("word/header") or not name.endswith(".xml"):
            continue
        try:
            root = ET.fromstring(blob)
        except ET.ParseError:
            continue

        paragraphs: List[ET.Element] = []
        texts: List[str] = []
        for paragraph in root.findall(".//w:p", namespaces=NS):
            text = _get_paragraph_text(paragraph).strip()
            if not text:
                continue
            paragraphs.append(paragraph)
            texts.append(text)

        title_index = next((idx for idx, text in enumerate(texts) if "市场占有率证明报告" in text), -1)
        changed = False
        if title_index >= 0:
            if title_index > 0:
                _set_paragraph_text(paragraphs[title_index - 1], company_name)
                _set_paragraph_text(paragraphs[title_index], product_title)
            else:
                _set_paragraph_text(paragraphs[title_index], combined_title)
            changed = True

        if changed:
            file_map[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _rewrite_summary_market_research_phrase(root: ET.Element, product_name: str) -> None:
    product_name = str(product_name or "").strip()
    if not product_name:
        return

    pattern = re.compile(r"对[“\"].+?[”\"]细分市场进行拆分和规模测算")
    replacement = f"对“{product_name}”细分市场进行拆分和规模测算"
    for paragraph in root.findall(".//w:p", namespaces=NS):
        text = _get_paragraph_text(paragraph)
        if "细分市场进行拆分和规模测算" not in text:
            continue
        updated = pattern.sub(replacement, text)
        if updated != text:
            _set_paragraph_text(paragraph, updated)


def _highlight_self_row_in_comparison_table(*, body: ET.Element, table_index: int, self_company_name: str) -> None:
    children = list(body)
    if table_index >= len(children):
        return
    table = children[table_index]
    if table.tag != f"{{{NS['w']}}}tbl":
        return
    rows = table.findall("./w:tr", NS)
    if len(rows) <= 2:
        return

    data_rows = rows[2:]
    for row in data_rows:
        _set_table_row_bold(row, bold=False)

    target = None
    for row in data_rows:
        company_name = _get_row_first_cell_text(row)
        if company_name == self_company_name:
            target = row
            break
    if target is not None:
        _set_table_row_bold(target, bold=True)


def _get_row_first_cell_text(row: ET.Element) -> str:
    cells = row.findall("./w:tc", NS)
    if not cells:
        return ""
    texts = cells[0].findall(".//w:t", NS)
    return "".join(node.text or "" for node in texts).strip()


def _set_table_row_bold(row: ET.Element, *, bold: bool) -> None:
    runs = row.findall(".//w:r", NS)
    for run in runs:
        rpr = run.find("./w:rPr", NS)
        if rpr is None:
            if not bold:
                continue
            rpr = ET.SubElement(run, f"{{{NS['w']}}}rPr")
        existing = rpr.find("./w:b", NS)
        if bold:
            if existing is None:
                ET.SubElement(rpr, f"{{{NS['w']}}}b")
        else:
            if existing is not None:
                rpr.remove(existing)



def _rewrite_chapter5_links(body: ET.Element, anchor_index: int, links: Sequence[str], warnings: List[str]) -> None:
    children = list(body)
    if anchor_index >= len(children):
        return
    template_index = anchor_index + 1 if anchor_index + 1 < len(children) else anchor_index
    template_paragraph = copy.deepcopy(children[template_index])
    remove_start = anchor_index + 1
    remove_end = len(children) - 1
    for elem in children[remove_start:remove_end]:
        body.remove(elem)

    insert_index = anchor_index + 1
    normalized_links = [link for link in links if link]
    if not normalized_links:
        warnings.append("第五章链接列表为空，已写入占位内容")
        normalized_links = [PLACEHOLDER_TEXT]
    for link in normalized_links:
        paragraph = copy.deepcopy(template_paragraph)
        _set_paragraph_text(paragraph, link)
        body.insert(insert_index, paragraph)
        insert_index += 1


def _rewrite_other_toc_titles(
    *,
    root: ET.Element,
    product_name: str,
    chapter2_layers: Sequence[Dict[str, Any]],
    sorted_rows: Sequence[Dict[str, Any]],
    self_company_name: str,
) -> None:
    toc_paragraphs = _find_toc_paragraphs(root)
    if not toc_paragraphs:
        return

    layer_cursor = 0
    company_cursor = 0
    in_chapter2 = False
    in_chapter3 = False
    for child in toc_paragraphs:
        text = _get_paragraph_text(child)
        if not text:
            continue
        suffix = _toc_page_suffix(text)
        bare = text[: len(text) - len(suffix)] if suffix else text

        if bare.startswith("第一章 ") and "产品概况" in bare:
            _set_paragraph_text(child, f"第一章 {product_name}产品概况{suffix}")
            in_chapter2 = False
            in_chapter3 = False
            continue
        if bare.startswith("第二章 "):
            in_chapter2 = True
            in_chapter3 = False
            continue
        if bare.startswith("第三章 "):
            in_chapter2 = False
            in_chapter3 = True
            continue
        if bare.startswith("第四章 "):
            _set_paragraph_text(child, f"第四章 {self_company_name}{product_name}市场占有率证明{suffix}")
            in_chapter2 = False
            in_chapter3 = False
            continue
        if bare.startswith("第五章 "):
            in_chapter2 = False
            in_chapter3 = False
            continue

        if in_chapter2 and re.match(r"^[一二三四五六七八九十]+、", bare) and layer_cursor < len(chapter2_layers):
            layer_name = str(chapter2_layers[layer_cursor].get("name") or "").strip()
            if layer_name:
                _set_paragraph_text(child, f"{_section_index_cn(layer_cursor + 1)}、{layer_name}市场情况分析{suffix}")
                layer_cursor += 1
            continue

        if in_chapter3 and "主导产品企业分析——" in bare and company_cursor < len(sorted_rows):
            company_name = str(sorted_rows[company_cursor].get("display_name") or "").strip()
            if company_name:
                _set_paragraph_text(child, f"{_section_index_cn(company_cursor + 1)}、主导产品企业分析——{company_name}{suffix}")
                company_cursor += 1


def _toc_page_suffix(text: str) -> str:
    match = re.search(r"(\d+)$", str(text or "").strip())
    return match.group(1) if match else ""


def _find_toc_paragraphs(root: ET.Element) -> List[ET.Element]:
    paragraphs = list(root.findall(".//w:p", NS))
    toc_start = None
    toc_end = None
    for index, paragraph in enumerate(paragraphs):
        text = _get_paragraph_text(paragraph)
        if text == "目录":
            toc_start = index
            continue
        if toc_start is not None and text == "摘 要":
            toc_end = index
            break
    if toc_start is None or toc_end is None or toc_end <= toc_start:
        return []
    return paragraphs[toc_start + 1:toc_end]


def _rewrite_chart9_labels(table: ET.Element, proof_scope: str) -> None:
    rows = table.findall("./w:tr", NS)
    if len(rows) < 5:
        return
    _set_first_cell_text(rows[3], f"{proof_scope}市场占有率")
    _set_first_cell_text(rows[4], f"{proof_scope}排名情况")


def _set_first_cell_text(row: ET.Element, text: str) -> None:
    cells = row.findall("./w:tc", NS)
    if not cells:
        return
    paragraph = cells[0].find(".//w:p", NS)
    if paragraph is None:
        return
    _set_paragraph_text(paragraph, text)



def _count_highlight_fields(root: ET.Element) -> int:
    count = 0
    for paragraph in root.findall(".//w:p", NS):
        runs = paragraph.findall("./w:r", NS)
        inside = False
        for run in runs:
            yellow = _is_yellow_run(run)
            if yellow and not inside:
                count += 1
            inside = yellow
    return count



def _replace_highlight_fields(root: ET.Element, values: Sequence[str]) -> List[ET.Element]:
    field_index = 0
    field_paragraphs: List[ET.Element] = []
    for paragraph in root.findall(".//w:p", NS):
        runs = paragraph.findall("./w:r", NS)
        current_runs: List[ET.Element] = []

        def commit(field_runs: List[ET.Element]) -> None:
            nonlocal field_index, field_paragraphs
            if not field_runs:
                return
            text_value = str(values[field_index]) if field_index < len(values) else ""
            field_index += 1
            field_paragraphs.append(paragraph)
            first = field_runs[0]
            texts = first.findall("./w:t", NS)
            if not texts:
                ET.SubElement(first, f"{{{NS['w']}}}t")
            _write_run_text_with_breaks(first, text_value)
            for extra in texts[1:]:
                first.remove(extra)
            first_props = first.find("./w:rPr", NS)
            if first_props is not None:
                highlight = first_props.find("./w:highlight", NS)
                if highlight is not None:
                    first_props.remove(highlight)
            for other in field_runs[1:]:
                for node in other.findall("./w:t", NS):
                    node.text = ""
                for br in other.findall("./w:br", NS):
                    other.remove(br)
                other_props = other.find("./w:rPr", NS)
                if other_props is not None:
                    highlight = other_props.find("./w:highlight", NS)
                    if highlight is not None:
                        other_props.remove(highlight)

        for run in runs:
            if _is_yellow_run(run):
                current_runs.append(run)
            else:
                commit(current_runs)
                current_runs = []
        commit(current_runs)
    return field_paragraphs



def _remove_all_yellow_highlights(root: ET.Element) -> None:
    for parent in root.iter():
        for child in list(parent):
            if child.tag == f"{{{NS['w']}}}highlight" and child.get(f"{{{NS['w']}}}val") == "yellow":
                parent.remove(child)



def _is_yellow_run(run: ET.Element) -> bool:
    for highlight in run.findall(".//w:rPr/w:highlight", NS):
        if highlight.get(f"{{{NS['w']}}}val") == "yellow":
            return True
    return False



def _write_run_text_with_breaks(run: ET.Element, text_value: str) -> None:
    text = str(text_value or "")
    for child in list(run):
        if child.tag in {f"{{{NS['w']}}}t", f"{{{NS['w']}}}br"}:
            run.remove(child)
    lines = text.split("\n")
    if not lines:
        lines = [""]
    for idx, line in enumerate(lines):
        text_node = ET.SubElement(run, f"{{{NS['w']}}}t")
        if line[:1].isspace() or line[-1:].isspace():
            text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        text_node.text = line
        if idx < len(lines) - 1:
            ET.SubElement(run, f"{{{NS['w']}}}br")


def _set_paragraph_text(paragraph: ET.Element, text: str) -> None:
    # 清空段落内所有正文节点（含 hyperlink），避免新旧链接文本串联。
    ppr = paragraph.find("./w:pPr", NS)
    direct_runs = paragraph.findall("./w:r", NS)
    preserved_rpr = None
    if direct_runs:
        first_rpr = direct_runs[0].find("./w:rPr", NS)
        if first_rpr is not None:
            preserved_rpr = copy.deepcopy(first_rpr)

    for child in list(paragraph):
        if ppr is not None and child is ppr:
            continue
        paragraph.remove(child)

    run = ET.SubElement(paragraph, f"{{{NS['w']}}}r")
    if preserved_rpr is not None:
        run.append(preserved_rpr)
    _write_run_text_with_breaks(run, text)



def _get_paragraph_text(paragraph: ET.Element) -> str:
    texts = paragraph.findall(".//w:t", NS)
    return "".join(node.text or "" for node in texts).strip()


def _find_parent(root: ET.Element, target: ET.Element) -> ET.Element | None:
    for parent in root.iter():
        for child in list(parent):
            if child is target:
                return parent
    return None


def _insert_paragraph_before(root: ET.Element, target: ET.Element, paragraph: ET.Element) -> bool:
    parent = _find_parent(root, target)
    if parent is None:
        return False
    children = list(parent)
    try:
        index = children.index(target)
    except ValueError:
        return False
    parent.insert(index, paragraph)
    return True


def _ensure_chapter1_environment_heading(root: ET.Element, field_paragraphs: Sequence[ET.Element]) -> None:
    chapter1_field_start = 20
    previous_slot_count = 0
    for spec in CHAPTER1_SECTION_SPECS:
        if spec["key"] == "industry_environment":
            break
        previous_slot_count += int(spec["slot_count"])
    target_index = chapter1_field_start + previous_slot_count
    if target_index >= len(field_paragraphs):
        return

    env_first_paragraph = field_paragraphs[target_index]
    body = root.find(".//w:body", NS)
    if body is None:
        return
    children = list(body)
    try:
        target_child_index = children.index(env_first_paragraph)
    except ValueError:
        return

    for child in children[max(0, target_child_index - 3):target_child_index]:
        if _get_paragraph_text(child) == "（一）行业发展环境":
            return

    source = next(
        (child for child in children[target_child_index: target_child_index + 30] if _get_paragraph_text(child) == "（二）行业发展趋势"),
        env_first_paragraph,
    )
    heading = copy.deepcopy(source)
    _set_paragraph_text(heading, "（一）行业发展环境")
    _insert_paragraph_before(root, env_first_paragraph, heading)


def _remove_chapter1_body_and_toc(root: ET.Element) -> None:
    body = root.find(".//w:body", NS)
    if body is None:
        return
    _remove_direct_child_range(
        body,
        lambda text: text.startswith("第一章 ") and text.endswith("产品概况"),
        lambda text: text.startswith("第二章 "),
        prefer_last_start=True,
    )
    _remove_direct_child_range(
        body,
        lambda text: text.startswith("第一章 ") and "产品概况" in text,
        lambda text: text.startswith("第二章 "),
        prefer_last_start=False,
    )
    _remove_chapter1_toc_entries(root)


def _remove_chapter1_toc_entries(root: ET.Element) -> None:
    toc_paragraphs = _find_toc_paragraphs(root)
    if not toc_paragraphs:
        return
    start = None
    end = None
    for index, paragraph in enumerate(toc_paragraphs):
        text = _get_paragraph_text(paragraph)
        if start is None and text.startswith("第一章 ") and "产品概况" in text:
            start = index
            continue
        if start is not None and text.startswith("第二章 "):
            end = index
            break
    if start is None or end is None or end <= start:
        return
    for paragraph in toc_paragraphs[start:end]:
        _remove_element(root, paragraph)


def _remove_direct_child_range(
    body: ET.Element,
    start_matcher: Any,
    end_matcher: Any,
    *,
    prefer_last_start: bool,
) -> None:
    children = list(body)
    start_candidates = [
        index
        for index, child in enumerate(children)
        if child.tag == f"{{{NS['w']}}}p" and start_matcher(_get_paragraph_text(child))
    ]
    if not start_candidates:
        return
    start = start_candidates[-1] if prefer_last_start else start_candidates[0]
    end = None
    for index in range(start + 1, len(children)):
        child = children[index]
        if child.tag == f"{{{NS['w']}}}p" and end_matcher(_get_paragraph_text(child)):
            end = index
            break
    if end is None or end <= start:
        return
    for child in children[start:end]:
        body.remove(child)


def _set_paragraph_alignment(paragraph: ET.Element, alignment: str) -> None:
    ppr = paragraph.find("./w:pPr", NS)
    if ppr is None:
        ppr = ET.Element(f"{{{NS['w']}}}pPr")
        paragraph.insert(0, ppr)
    jc = ppr.find("./w:jc", NS)
    if jc is None:
        jc = ET.SubElement(ppr, f"{{{NS['w']}}}jc")
    jc.set(f"{{{NS['w']}}}val", alignment)


def _normalize_chapter1_body_paragraphs_and_styles(field_paragraphs: Sequence[ET.Element]) -> None:
    chapter1_field_start = 20
    chapter1_slot_count = EXPECTED_CHAPTER1_SLOT_COUNT
    chapter1_paragraphs = list(field_paragraphs[chapter1_field_start: chapter1_field_start + chapter1_slot_count])
    for paragraph in chapter1_paragraphs:
        original = _get_paragraph_text(paragraph)
        if not original:
            continue
        text = _normalize_chapter1_body_text(original)
        if not text:
            continue
        _set_paragraph_text(paragraph, text)
        if BODY_HEADING_PATTERN.match(text):
            continue
        _clear_paragraph_heading_level(paragraph)
        _set_paragraph_alignment(paragraph, "both")
        _normalize_paragraph_runs_as_body(paragraph)


def _normalize_chapter1_body_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\s*\n+\s*", "", normalized)
    normalized = re.sub(r"\s*([，。；：！？、])\s*", r"\1", normalized)
    normalized = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", normalized)
    normalized = re.sub(r"[ \t]{2,}", " ", normalized)
    return normalized.strip()


def _clear_paragraph_heading_level(paragraph: ET.Element) -> None:
    ppr = paragraph.find("./w:pPr", NS)
    if ppr is None:
        return
    outline = ppr.find("./w:outlineLvl", NS)
    if outline is not None:
        ppr.remove(outline)
    pstyle = ppr.find("./w:pStyle", NS)
    if pstyle is not None:
        ppr.remove(pstyle)


def _normalize_paragraph_runs_as_body(paragraph: ET.Element) -> None:
    for run in paragraph.findall("./w:r", NS):
        rpr = run.find("./w:rPr", NS)
        if rpr is None:
            rpr = ET.SubElement(run, f"{{{NS['w']}}}rPr")
        for bold_tag in ("b", "bCs"):
            node = rpr.find(f"./w:{bold_tag}", NS)
            if node is not None:
                rpr.remove(node)
        sz = rpr.find("./w:sz", NS)
        if sz is None:
            sz = ET.SubElement(rpr, f"{{{NS['w']}}}sz")
        sz.set(f"{{{NS['w']}}}val", "24")
        szcs = rpr.find("./w:szCs", NS)
        if szcs is None:
            szcs = ET.SubElement(rpr, f"{{{NS['w']}}}szCs")
        szcs.set(f"{{{NS['w']}}}val", "24")


def _set_signature_block_right_alignment(root: ET.Element) -> None:
    for paragraph in root.findall(".//w:p", NS):
        text = _get_paragraph_text(paragraph)
        if not text:
            continue
        if re.search(r"有限公司[（(]盖章[）)]", text):
            _set_paragraph_alignment(paragraph, "right")
            continue
        if re.match(r"^\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日$", text):
            _set_paragraph_alignment(paragraph, "right")


def _remove_section_page_number_restart(root: ET.Element) -> None:
    for sect in root.findall(".//w:sectPr", NS):
        page_num = sect.find("./w:pgNumType", NS)
        if page_num is None:
            continue
        sect.remove(page_num)


def _enable_word_update_fields_on_open(file_map: Dict[str, bytes]) -> None:
    raw = file_map.get("word/settings.xml")
    if not raw:
        return
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return
    update_fields = root.find("./w:updateFields", NS)
    if update_fields is None:
        update_fields = ET.SubElement(root, f"{{{NS['w']}}}updateFields")
    update_fields.set(f"{{{NS['w']}}}val", "true")
    file_map["word/settings.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _mark_footer_page_fields_dirty(file_map: Dict[str, bytes]) -> None:
    for name, raw in list(file_map.items()):
        if not (name.startswith("word/footer") and name.endswith(".xml")):
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue

        has_page_field = any(
            "PAGE" in "".join(instr.itertext()).upper()
            for instr in root.findall(".//w:instrText", NS)
        )
        if not has_page_field:
            continue

        changed = False
        for fld in root.findall(".//w:fldChar", NS):
            fld_type = str(fld.get(f"{{{NS['w']}}}fldCharType") or "").strip().lower()
            if fld_type == "begin":
                fld.set(f"{{{NS['w']}}}dirty", "true")
                changed = True
        if changed:
            file_map[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _apply_body_plain_paragraph_justification(root: ET.Element) -> None:
    parent_map = {child: parent for parent in root.iter() for child in list(parent)}
    for paragraph in root.findall(".//w:p", NS):
        if _is_paragraph_inside_table(paragraph, parent_map):
            continue
        text = _get_paragraph_text(paragraph)
        if not text or BODY_HEADING_PATTERN.match(text):
            continue
        if not _is_plain_small4_paragraph(paragraph):
            continue
        ppr = paragraph.find("./w:pPr", NS)
        if ppr is None:
            ppr = ET.Element(f"{{{NS['w']}}}pPr")
            paragraph.insert(0, ppr)
        jc = ppr.find("./w:jc", NS)
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
    text_runs: List[ET.Element] = []
    for run in paragraph.findall("./w:r", NS):
        text = "".join((node.text or "") for node in run.findall("./w:t", NS)).strip()
        if text:
            text_runs.append(run)
    if not text_runs:
        return False

    has_small4 = False
    for run in text_runs:
        rpr = run.find("./w:rPr", NS)
        if rpr is None:
            continue
        if _run_is_bold(rpr) or _run_is_underline(rpr):
            return False
        if _run_font_size(rpr) == "24":
            has_small4 = True
    return has_small4


def _run_font_size(rpr: ET.Element) -> str:
    size_node = rpr.find("./w:sz", NS)
    if size_node is None:
        size_node = rpr.find("./w:szCs", NS)
    if size_node is None:
        return ""
    return str(size_node.get(f"{{{NS['w']}}}val") or "").strip()


def _run_is_bold(rpr: ET.Element) -> bool:
    node = rpr.find("./w:b", NS)
    if node is None:
        return False
    value = str(node.get(f"{{{NS['w']}}}val") or "1").strip().lower()
    return value not in {"0", "false", "off"}


def _run_is_underline(rpr: ET.Element) -> bool:
    node = rpr.find("./w:u", NS)
    if node is None:
        return False
    value = str(node.get(f"{{{NS['w']}}}val") or "single").strip().lower()
    return value not in {"none", "0", "false", "off"}


def _compress_chapter1_visual_paragraphs(root: ET.Element, field_paragraphs: Sequence[ET.Element]) -> None:
    chapter1_field_start = 20
    cursor = chapter1_field_start
    for spec in CHAPTER1_SECTION_SPECS:
        slot_count = spec["slot_count"]
        section_paragraphs = list(field_paragraphs[cursor: cursor + slot_count])
        cursor += slot_count
        if not section_paragraphs:
            continue
        if spec["key"] in {"industry_supply_chain"}:
            # 供应链章节模板里存在固定小节标题结构，禁止做可视压缩，避免标题与正文错位。
            continue

        unique_paragraphs: List[ET.Element] = []
        section_texts: List[str] = []
        for paragraph in section_paragraphs:
            text = _get_paragraph_text(paragraph)
            if unique_paragraphs and paragraph is unique_paragraphs[-1]:
                section_texts[-1] = f"{section_texts[-1]} {text}".strip()
            else:
                unique_paragraphs.append(paragraph)
                section_texts.append(text)

        visible_target = CHAPTER1_VISIBLE_PARAGRAPH_COUNTS.get(spec["key"], max(1, slot_count))
        content_texts = [text for text in section_texts if text and text != PLACEHOLDER_TEXT]
        if not content_texts:
            content_texts = [PLACEHOLDER_TEXT]
        merged_texts = _merge_paragraphs_to_target(content_texts, max(1, visible_target))

        for index, paragraph in enumerate(unique_paragraphs):
            if index < len(merged_texts):
                _set_paragraph_text(paragraph, merged_texts[index])
            else:
                _remove_element(root, paragraph)


def _remove_element(root: ET.Element, target: ET.Element) -> None:
    for parent in root.iter():
        for child in list(parent):
            if child is target:
                parent.remove(child)
                return


def _flatten_chapter1_slots(sections: Sequence[Dict[str, Any]]) -> List[str]:
    section_map: Dict[str, Dict[str, Any]] = {}
    for item in sections:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key in CHAPTER1_SPEC_MAP and key not in section_map:
            section_map[key] = item

    slots: List[str] = []
    for spec in CHAPTER1_SECTION_SPECS:
        key = spec["key"]
        section = section_map.get(key)
        if section is None:
            slots.extend([PLACEHOLDER_TEXT] * spec["slot_count"])
            continue
        paragraphs = [str(item).strip() for item in section.get("paragraphs") or [] if str(item).strip()]
        if len(paragraphs) < spec["slot_count"]:
            paragraphs.extend([PLACEHOLDER_TEXT] * (spec["slot_count"] - len(paragraphs)))
        slots.extend(paragraphs[: spec["slot_count"]])
    return slots


def _merge_heading_like_paragraphs(paragraphs: Sequence[str]) -> List[str]:
    merged: List[str] = []
    pending_heading = ""
    for raw in paragraphs:
        text = str(raw or "").strip()
        if not text:
            continue
        if _looks_like_heading_fragment(text):
            pending_heading = f"{pending_heading} {text}".strip()
            continue
        if pending_heading:
            text = f"{pending_heading}：{text}"
            pending_heading = ""
        merged.append(text)
    if pending_heading:
        if merged:
            merged[-1] = f"{merged[-1]} {pending_heading}"
        else:
            merged.append(pending_heading)
    return merged


def _looks_like_heading_fragment(text: str) -> bool:
    if len(text) > 20:
        return False
    if re.search(r"[。！？；：，,.!?;:]", text):
        return False
    return True


def _fit_paragraphs_to_slot_count(paragraphs: Sequence[str], slot_count: int, title: str) -> tuple[List[str], List[str]]:
    warnings: List[str] = []
    fitted = [str(item).strip() for item in paragraphs if str(item).strip()]
    if not fitted:
        return [PLACEHOLDER_TEXT] * slot_count, [f"第一章《{title}》未生成成功，已写入占位内容"]

    if len(fitted) < slot_count:
        expanded = list(fitted)
        split_performed = False
        while len(expanded) < slot_count:
            idx = _find_best_split_index(expanded)
            if idx is None:
                break
            left, right = _split_paragraph_for_template(expanded[idx])
            if not left or not right:
                break
            expanded[idx:idx + 1] = [left, right]
            split_performed = True
        fitted = expanded
        if split_performed:
            warnings.append(f"第一章《{title}》已自动拆分成长段，适配模板段落数")
        if len(fitted) < slot_count:
            warnings.append(f"第一章《{title}》段落不足，已补齐占位内容")
            fitted.extend([PLACEHOLDER_TEXT] * (slot_count - len(fitted)))

    if len(fitted) > slot_count:
        warnings.append(f"第一章《{title}》段落较多，已自动合并后写入模板")
        fitted = _merge_paragraphs_to_target(fitted, slot_count)

    return fitted[:slot_count], warnings


def _ensure_supply_chain_subsections(paragraphs: Sequence[str]) -> List[str]:
    result: List[str] = []
    indexed: Dict[int, str] = {}
    unlabeled: List[str] = []
    expanded_paragraphs: List[str] = []
    for raw in paragraphs:
        expanded_paragraphs.extend(_split_supply_chain_paragraph(str(raw or "").strip()))

    for text in expanded_paragraphs:
        match = re.match(r"^[（(]([一二三四五12345])[）)]", text)
        if match:
            token = match.group(1)
            index_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
            idx = index_map.get(token)
            if idx and idx not in indexed:
                indexed[idx] = _normalize_supply_chain_content(text, topic=SUPPLY_CHAIN_SUBTOPICS[idx - 1])
                continue
        unlabeled.append(text)

    intro = unlabeled.pop(0) if unlabeled else PLACEHOLDER_TEXT
    result.append(_clean_chapter1_paragraph_text(intro) or PLACEHOLDER_TEXT)

    for idx, topic in enumerate(SUPPLY_CHAIN_SUBTOPICS, start=1):
        if idx in indexed:
            result.append(indexed[idx])
            continue
        if unlabeled:
            result.append(_clean_chapter1_paragraph_text(unlabeled.pop(0)) or PLACEHOLDER_TEXT)

    result.extend(unlabeled)
    return result


def _split_supply_chain_paragraph(text: str) -> List[str]:
    cleaned = str(text or "").strip()
    if not cleaned:
        return []
    marker_pattern = r"[（(][一二三四五12345][）)]"
    marker_matches = list(re.finditer(marker_pattern, cleaned))
    if len(marker_matches) <= 1:
        return [cleaned]

    result: List[str] = []
    prefix = cleaned[: marker_matches[0].start()].strip(" ；;，,")
    if prefix:
        result.append(prefix)
    for idx, marker in enumerate(marker_matches):
        start = marker.start()
        end = marker_matches[idx + 1].start() if idx + 1 < len(marker_matches) else len(cleaned)
        part = cleaned[start:end].strip(" ；;，,")
        if part:
            result.append(part)
    return result


def _normalize_supply_chain_content(text: str, *, topic: str) -> str:
    normalized = _clean_chapter1_paragraph_text(text)
    if not normalized:
        return PLACEHOLDER_TEXT

    # 去掉“（一）”“（二）”等编号，避免与模板固定小标题冲突。
    normalized = re.sub(r"^[（(][一二三四五12345][）)]\s*", "", normalized)
    # 去掉可能重复的小标题，模板中已经有固定标题。
    topic_aliases = [
        topic,
        "上游原材料与核心零部件",
        "中游制造与装配环节",
        "下游应用行业与客户结构",
        "渠道流通与交付协同",
        "供应链风险与优化趋势",
    ]
    for alias in topic_aliases:
        normalized = re.sub(rf"^{re.escape(alias)}\s*[:：]?", "", normalized)
    normalized = _clean_chapter1_paragraph_text(normalized)

    if "：" in normalized:
        head, body = normalized.split("：", 1)
        if body.strip():
            return f"{head}：{body.strip()}"
    if ":" in normalized:
        head, body = normalized.split(":", 1)
        if body.strip():
            return f"{head}：{body.strip()}"
    if normalized.strip():
        return normalized.strip()
    return PLACEHOLDER_TEXT


def _sanitize_industry_environment_paragraphs(paragraphs: Sequence[str]) -> List[str]:
    return _sanitize_named_topic_paragraphs(
        paragraphs=paragraphs,
        section_titles=["行业发展环境", "行业发展环境和趋势"],
        topic_titles=["政策环境", "经济环境", "技术环境", "社会环境"],
    )


def _sanitize_industry_trends_paragraphs(paragraphs: Sequence[str]) -> List[str]:
    return _sanitize_named_topic_paragraphs(
        paragraphs=paragraphs,
        section_titles=["行业发展趋势", "行业发展环境和趋势"],
        topic_titles=["技术迭代趋势", "产品发展趋势", "市场需求趋势", "行业竞争趋势", "产业链发展趋势"],
    )


def _sanitize_named_topic_paragraphs(
    *,
    paragraphs: Sequence[str],
    section_titles: Sequence[str],
    topic_titles: Sequence[str],
) -> List[str]:
    if not paragraphs:
        return []

    section_pattern = "|".join(re.escape(item) for item in section_titles if item)
    topic_pattern = "|".join(re.escape(item) for item in topic_titles if item)
    section_title_set = {item.strip() for item in section_titles if item.strip()}
    topic_title_set = {item.strip() for item in topic_titles if item.strip()}

    cleaned: List[str] = []
    for raw in paragraphs:
        text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
        segments = [segment.strip() for segment in re.split(r"\n+", text) if segment.strip()]
        for segment in segments:
            current = segment
            if section_pattern:
                current = re.sub(
                    rf"^\s*[（(]?[一二三四五六七八九十0-9]+[）)]?[\.、]?\s*(?:{section_pattern})\s*[:：]?\s*",
                    "",
                    current,
                )
            if topic_pattern:
                current = re.sub(
                    rf"^\s*[（(]?[一二三四五六七八九十0-9]+[）)]?[\.、]?\s*(?:{topic_pattern})\s*[:：]?\s*",
                    "",
                    current,
                )
                current = re.sub(
                    rf"\s+[（(]?[一二三四五六七八九十0-9]+[）)]?[\.、]?\s*(?:{topic_pattern})\s*[:：]?\s*",
                    " ",
                    current,
                )
            current = re.sub(r"\s{2,}", " ", current).strip(" ：:;；，,")
            if not current:
                continue
            if current in section_title_set or current in topic_title_set:
                continue
            cleaned.append(current)

    return _merge_heading_like_paragraphs(cleaned)


def _fit_topic_bucket_to_target(paragraphs: Sequence[str], target: int) -> List[str]:
    fitted = [
        _clean_chapter1_paragraph_text(item)
        for item in paragraphs
        if _clean_chapter1_paragraph_text(item) and not _is_chapter1_instruction_placeholder(item)
    ]
    if not fitted:
        return [PLACEHOLDER_TEXT] * target

    while len(fitted) < target:
        idx = _find_best_split_index(fitted)
        if idx is None:
            break
        left, right = _split_paragraph_for_template(fitted[idx])
        if not left or not right:
            break
        fitted[idx:idx + 1] = [left, right]
    if len(fitted) > target:
        fitted = _merge_paragraphs_to_target(fitted, target)
    if len(fitted) < target:
        fitted.extend([PLACEHOLDER_TEXT] * (target - len(fitted)))
    return fitted[:target]


def _fit_supply_chain_paragraphs_to_slot_count(paragraphs: Sequence[str], title: str) -> tuple[List[str], List[str]]:
    warnings: List[str] = []
    cleaned = [
        _clean_chapter1_paragraph_text(item)
        for item in paragraphs
        if _clean_chapter1_paragraph_text(item) and not _is_chapter1_instruction_placeholder(item)
    ]
    if not cleaned:
        return [PLACEHOLDER_TEXT] * CHAPTER1_SPEC_MAP["industry_supply_chain"]["slot_count"], [
            f"第一章《{title}》未生成成功，已写入占位内容"
        ]

    intro = cleaned[0]
    topic_buckets: List[List[str]] = [[] for _ in SUPPLY_CHAIN_SUBTOPICS]
    body_paragraphs = cleaned[1:]
    target_per_topic = [4, 3, 3, 2, 5]

    def _guess_supply_chain_topic_index(text: str) -> int | None:
        normalized = str(text or "")
        rules = [
            (4, ["发展方向", "优化方向", "模块化", "标准化", "国产化", "生态协同"]),
            (3, ["核心特征", "面临的挑战", "核心挑战"]),
            (0, ["上游", "原材料", "核心零部件", "芯片", "光学模组", "传感器", "器件"]),
            (1, ["中游", "制造", "装配", "集成", "生产", "组装", "质量控制"]),
            (2, ["下游", "应用", "客户", "分销", "渠道", "交付", "服务"]),
            (3, ["挑战", "风险", "瓶颈", "复杂度"]),
            (4, ["趋势", "未来", "转型"]),
        ]
        for idx, keywords in rules:
            if any(keyword in normalized for keyword in keywords):
                return idx
        return None

    unassigned: List[str] = []
    for text in body_paragraphs:
        guessed = _guess_supply_chain_topic_index(text)
        if guessed is None:
            unassigned.append(text)
            continue
        topic = SUPPLY_CHAIN_SUBTOPICS[guessed]
        topic_buckets[guessed].append(_normalize_supply_chain_content(text, topic=topic))

    ordered_topic_idx = 0
    for text in unassigned:
        while (
            ordered_topic_idx < len(topic_buckets) - 1
            and len(topic_buckets[ordered_topic_idx]) >= target_per_topic[ordered_topic_idx]
        ):
            ordered_topic_idx += 1
        topic = SUPPLY_CHAIN_SUBTOPICS[ordered_topic_idx]
        topic_buckets[ordered_topic_idx].append(_normalize_supply_chain_content(text, topic=topic))

    fitted_topics: List[List[str]] = []
    for bucket, target in zip(topic_buckets, target_per_topic):
        fitted_topics.append(_fit_topic_bucket_to_target(bucket, target))
    result = [intro]
    for bucket in fitted_topics:
        result.extend(bucket)
    if len(result) != CHAPTER1_SPEC_MAP["industry_supply_chain"]["slot_count"]:
        warnings.append(f"第一章《{title}》段落结构异常，已自动回填占位")
        expected = CHAPTER1_SPEC_MAP["industry_supply_chain"]["slot_count"]
        result = (result + [PLACEHOLDER_TEXT] * expected)[:expected]
    return result, warnings


def _find_best_split_index(paragraphs: Sequence[str]) -> int | None:
    candidates = [idx for idx, text in enumerate(paragraphs) if len(str(text).strip()) >= 16]
    if not candidates:
        return None
    return max(candidates, key=lambda idx: len(str(paragraphs[idx]).strip()))


def _split_paragraph_for_template(text: str) -> tuple[str, str]:
    sentences = [item.strip() for item in re.split(r"(?<=[。！？；])", text) if item.strip()]
    if len(sentences) >= 2:
        total = sum(len(item) for item in sentences)
        current = 0
        split_at = 0
        for idx, sentence in enumerate(sentences, start=1):
            current += len(sentence)
            split_at = idx
            if current >= total / 2:
                break
        left = "".join(sentences[:split_at]).strip()
        right = "".join(sentences[split_at:]).strip()
        if left and right:
            return left, right

    midpoint = len(text) // 2
    for separator in ("。", "；", "！", "？"):
        pos = text.rfind(separator, 0, midpoint + 30)
        if pos > 20:
            left = text[:pos + 1].strip()
            right = text[pos + 1:].strip()
            if left and right:
                return left, right
    return text.strip(), ""


def _merge_paragraphs_to_target(paragraphs: Sequence[str], target: int) -> List[str]:
    merged = [str(item).strip() for item in paragraphs if str(item).strip()]
    while len(merged) > target and len(merged) >= 2:
        idx = min(range(1, len(merged)), key=lambda i: len(merged[i]))
        merged[idx - 1] = f"{merged[idx - 1]} {merged[idx]}".strip()
        del merged[idx]
    return merged



def _extract_json_payload(raw_text: str) -> Dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
        if match:
            text = match.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char not in "{[":
                continue
            try:
                parsed, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed:
                return parsed
        raise OtherProofError("第一章生成结果不是合法 JSON")


def _coerce_chapter1_sections_from_text(raw_text: str) -> tuple[List[Dict[str, Any]], str]:
    text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return [], "第一章生成结果为空，已写入占位内容"

    json_like_sections = _extract_sections_from_json_like_text(text)
    if json_like_sections:
        return json_like_sections, "第一章返回了不完整 JSON，系统已自动修复并映射到模板章节"

    heading_rules = [
        ("background_overview", [r"背景与概述", r"背景概述"]),
        ("definition", [r"定义", r"基本概念"]),
        ("working_principle", [r"工作原理"]),
        ("product_attributes", [r"产品属性"]),
        ("technical_specifications", [r"技术规范"]),
        ("industry_history", [r"行业发展历程"]),
        ("industry_environment", [r"行业发展环境"]),
        ("industry_trends", [r"行业发展趋势"]),
        ("industry_supply_chain", [r"行业供应链", r"供应链"]),
    ]

    def match_heading(paragraph: str) -> str:
        first_line = paragraph.split("\n", 1)[0].strip()
        compact = re.sub(r"\s+", "", first_line)
        compact = re.sub(r"^[（(]?[一二三四五六七八九十\d]+[)）.、\s]*", "", compact)
        for key, patterns in heading_rules:
            for pattern in patterns:
                if re.search(pattern, compact):
                    return key
        return ""

    section_map: Dict[str, List[str]] = {spec["key"]: [] for spec in CHAPTER1_SECTION_SPECS}
    current_key = "background_overview"
    blocks = [block.strip() for block in re.split(r"\n{2,}", text) if block.strip()]

    for block in blocks:
        heading_key = match_heading(block)
        paragraph = block
        if heading_key:
            current_key = heading_key
            lines = [line.strip() for line in block.split("\n") if line.strip()]
            if len(lines) > 1:
                paragraph = "\n".join(lines[1:]).strip()
            else:
                paragraph = ""
        if paragraph:
            section_map.setdefault(current_key, []).append(paragraph)

    sections: List[Dict[str, Any]] = []
    for spec in CHAPTER1_SECTION_SPECS:
        key = spec["key"]
        title = spec["title"]
        paragraphs = section_map.get(key, [])
        sections.append({"key": key, "title": title, "paragraphs": paragraphs})

    return sections, "第一章返回了非 JSON 文本，系统已自动解析并映射到模板章节"


def _repair_empty_chapter1_sections(
    *,
    client: Any,
    model: str,
    product_name: str,
    sections: Sequence[Dict[str, Any]],
    timeout_seconds: int,
    max_output_tokens: int,
) -> tuple[List[Dict[str, Any]], List[str], List[str]]:
    existing_map: Dict[str, Dict[str, Any]] = {}
    empty_specs: List[Dict[str, Any]] = []
    for item in sections:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key not in CHAPTER1_SPEC_MAP:
            continue
        existing_map[key] = item
        paragraphs = [str(text).strip() for text in (item.get("paragraphs") or []) if str(text).strip()]
        non_placeholder = [text for text in paragraphs if text != PLACEHOLDER_TEXT]
        has_placeholder = any(_is_chapter1_placeholder_text(text) for text in paragraphs)
        if not non_placeholder or has_placeholder:
            empty_specs.append(CHAPTER1_SPEC_MAP[key])

    if not empty_specs:
        return list(sections), [], []

    repair_messages = [
        {
            "role": "system",
            "content": (
                "你是产业研究分析师。"
                "当前任务是补写缺失章节。"
                "只输出 JSON，不要解释，不要 Markdown。"
            ),
        },
        {"role": "user", "content": _build_chapter1_repair_prompt(product_name, empty_specs)},
    ]
    try:
        raw = client.complete(
            repair_messages,
            model=model,
            temperature=0.1,
            max_output_tokens=max(1400, min(max_output_tokens, 5000)),
            timeout_seconds=timeout_seconds,
            retry_max_attempts=0,
        )
    except Exception:
        return list(sections), ["第一章缺失章节补全请求失败，已保留当前内容"], []

    repair_parse_warning = ""
    try:
        parsed = _extract_json_payload(raw)
        repair_raw_sections = parsed.get("sections")
    except OtherProofError:
        repair_raw_sections, repair_parse_warning = _coerce_chapter1_sections_from_text(raw)

    repaired_sections, _repaired_warnings = normalize_chapter1_sections(repair_raw_sections)
    repaired_map: Dict[str, Dict[str, Any]] = {
        str(item.get("key") or "").strip(): item
        for item in repaired_sections
        if isinstance(item, dict) and str(item.get("key") or "").strip() in CHAPTER1_SPEC_MAP
    }

    replaced_titles: List[str] = []
    for spec in empty_specs:
        key = spec["key"]
        repaired = repaired_map.get(key)
        target = existing_map.get(key)
        if not repaired or not target:
            continue
        paragraphs = [str(text).strip() for text in (repaired.get("paragraphs") or []) if str(text).strip()]
        non_placeholder = [text for text in paragraphs if text != PLACEHOLDER_TEXT]
        if not non_placeholder:
            continue
        target["paragraphs"] = paragraphs
        replaced_titles.append(spec["title"])

    merged_sections: List[Dict[str, Any]] = []
    for spec in CHAPTER1_SECTION_SPECS:
        key = spec["key"]
        section = existing_map.get(key)
        if isinstance(section, dict):
            merged_sections.append(section)

    warnings: List[str] = []
    if repair_parse_warning:
        warnings.append(repair_parse_warning)
    if replaced_titles:
        warnings.append("第一章已补全缺失章节：" + "、".join(replaced_titles))
    else:
        warnings.append("第一章缺失章节补全未成功，仍需人工补充")
    replaced_keys = [spec["key"] for spec in empty_specs if spec["title"] in replaced_titles]
    return merged_sections, warnings, replaced_keys


def _extract_sections_from_json_like_text(text: str) -> List[Dict[str, Any]]:
    def _extract_balanced_array(chunk_text: str, array_start: int) -> str:
        if array_start < 0 or array_start >= len(chunk_text) or chunk_text[array_start] != "[":
            return ""
        depth = 0
        in_string = False
        escaped = False
        for idx in range(array_start, len(chunk_text)):
            ch = chunk_text[idx]
            if in_string:
                if escaped:
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                    continue
                if ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "[":
                depth += 1
                continue
            if ch == "]":
                depth -= 1
                if depth == 0:
                    return chunk_text[array_start: idx + 1]
        return ""

    key_positions = list(re.finditer(r'"key"\s*:\s*"([^"]+)"', text))
    if not key_positions:
        return []

    section_map: Dict[str, Dict[str, Any]] = {}
    for spec in CHAPTER1_SECTION_SPECS:
        section_map[spec["key"]] = {"key": spec["key"], "title": spec["title"], "paragraphs": []}

    for index, key_match in enumerate(key_positions):
        key = key_match.group(1).strip()
        if key not in section_map:
            continue
        chunk_end = key_positions[index + 1].start() if index + 1 < len(key_positions) else len(text)
        chunk = text[key_match.start():chunk_end]

        title_match = re.search(r'"title"\s*:\s*"([^"]+)"', chunk)
        if title_match:
            section_map[key]["title"] = title_match.group(1).strip() or section_map[key]["title"]

        paragraphs_start = re.search(r'"paragraphs"\s*:\s*\[', chunk)
        if not paragraphs_start:
            continue
        array_start = paragraphs_start.end() - 1
        arr_literal = _extract_balanced_array(chunk, array_start)
        if not arr_literal:
            continue

        parsed_values: List[Any] = []
        try:
            candidate = json.loads(arr_literal)
            if isinstance(candidate, list):
                parsed_values = candidate
        except Exception:
            parsed_values = []

        if not parsed_values:
            candidates = re.findall(r'"((?:\\.|[^"\\])*)"', arr_literal)
            parsed_values = []
            for raw_value in candidates:
                try:
                    parsed_values.append(json.loads(f'"{raw_value}"'))
                except Exception:
                    parsed_values.append(raw_value)

        parsed_paragraphs: List[str] = []
        for value in parsed_values:
            text_value = str(value).strip()
            if text_value:
                parsed_paragraphs.append(text_value)
        if parsed_paragraphs:
            section_map[key]["paragraphs"] = parsed_paragraphs

    if not any(item["paragraphs"] for item in section_map.values()):
        return []
    return [section_map[spec["key"]] for spec in CHAPTER1_SECTION_SPECS]


def _extract_section_paragraphs(payload: Any, section_key: str) -> List[str]:
    target = payload if isinstance(payload, dict) else {}
    section_obj: Dict[str, Any] = {}
    direct = target.get("section")
    if isinstance(direct, dict):
        section_obj = direct
    else:
        sections = target.get("sections")
        if isinstance(sections, list):
            for item in sections:
                if not isinstance(item, dict):
                    continue
                if str(item.get("key") or "").strip() == section_key:
                    section_obj = item
                    break
            if not section_obj:
                for item in sections:
                    if isinstance(item, dict):
                        section_obj = item
                        break
    if not section_obj:
        raise OtherProofError("第一章小节 JSON 结构缺失")

    paragraphs = section_obj.get("paragraphs")
    if not isinstance(paragraphs, list):
        raise OtherProofError("第一章小节 paragraphs 不是数组")
    return [str(item).strip() for item in paragraphs if str(item).strip()]


def _build_chapter1_context_excerpt(generated_sections: Sequence[Dict[str, Any]], limit: int = 3) -> str:
    snippets: List[str] = []
    for section in generated_sections[-limit:]:
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or "").strip()
        paragraphs = section.get("paragraphs")
        if not isinstance(paragraphs, list):
            continue
        non_empty = [str(item).strip() for item in paragraphs if str(item).strip()]
        if not non_empty:
            continue
        snippets.append(f"- {title}：{non_empty[-1]}")
    if not snippets:
        return "（暂无已生成小节）"
    return "\n".join(snippets)


def _chapter1_style_constraints_text() -> str:
    return (
        "写作原则：采用咨询报告/研究报告风格，语气克制、严谨、客观；每段都是完整正文，不写小标题、项目符号或清单。\n"
        "内容原则：必须围绕产品本身展开，说明产品定位、技术特征、应用场景和产业链位置，避免泛泛而谈。\n"
        "数据原则：不写具体数字、年份、金额、比例、增速、排名、市场份额，也不要写“数十毫秒”这类量化指标；需要表达程度时使用定性描述。\n"
        "表达原则：不要写“待补充”，不要编造企业私有信息，不使用夸张营销表达，段落之间要自然衔接。\n"
        "句式原则：相邻段落开头不要重复同一种句式，不要每段都用“随着、当前、从……来看、在……方面”等套话起句。\n"
    )


def _coerce_chapter1_section_paragraphs_from_text(raw_text: str) -> tuple[List[str], str]:
    text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return [], "返回为空，已改写为占位"
    json_like_sections = _extract_sections_from_json_like_text(text)
    if json_like_sections:
        section = json_like_sections[0] if isinstance(json_like_sections[0], dict) else {}
        paragraphs = [str(item).strip() for item in (section.get("paragraphs") or []) if str(item).strip()]
        if paragraphs:
            return paragraphs, "返回了不完整 JSON，已自动修复"
    blocks = [item.strip() for item in re.split(r"\n{2,}", text) if item.strip()]
    paragraphs = [item for item in blocks if len(re.sub(r"\s+", "", item)) >= 12]
    if not paragraphs:
        paragraphs = [line.strip() for line in text.split("\n") if line.strip() and len(line.strip()) >= 12]
    return paragraphs, "返回了非标准 JSON，已自动解析"


def _build_chapter1_batch_prompt(
    *,
    product_name: str,
    batch_specs: Sequence[Dict[str, Any]],
    generated_sections: Sequence[Dict[str, Any]],
) -> str:
    if not batch_specs:
        raise OtherProofError("第一章分批生成配置为空")

    full_outline = "\n".join(f"- {item['title']}" for item in CHAPTER1_SECTION_SPECS)
    batch_outline = "\n".join(f"- {item['key']}（{item['title']}）" for item in batch_specs)
    context_excerpt = _build_chapter1_context_excerpt(generated_sections, limit=6)
    style_constraints = _chapter1_style_constraints_text()
    def _paragraph_requirement(spec: Dict[str, Any]) -> str:
        key = str(spec.get("key") or "").strip()
        if key == "industry_supply_chain":
            return (
                "输出 6 段完整正文：第 1 段为供应链总述；第 2 段只写上游供应链，覆盖芯片、光学模组、传感器、结构件和能源部件；"
                "第 3 段只写中游制造与集成，覆盖工业设计、软硬件协同、整机组装、测试验证和质量控制；"
                "第 4 段只写下游应用与分销，覆盖企业级场景、消费场景、渠道交付和售后服务；"
                "第 5 段只写行业供应链的核心特征与面临的挑战；第 6 段只写行业供应链的发展方向。"
                "每段 180-260 字，由多句完整陈述句组成，不要在正文中写（一）（二）或小标题。"
            )
        if key in {"industry_environment", "industry_trends"}:
            return "至少 5 段，每段 110-220 字，段落必须是完整陈述句。"
        if key in {"working_principle", "product_attributes"}:
            return "至少 4 段，每段 110-220 字，段落必须是完整陈述句。"
        return "至少 3 段，每段 110-220 字，段落必须是完整陈述句。"

    min_paragraph_lines = "\n".join(
        f"- {item['key']}（{item['title']}）：{_paragraph_requirement(item)}"
        for item in batch_specs
    )
    strict_json = (
        '{"sections":['
        + ",".join(
            f'{{"key":"{item["key"]}","title":"{item["title"]}","paragraphs":["..."]}}'
            for item in batch_specs
        )
        + "]}"
    )
    return (
        f"产品：{product_name}\n"
        "你正在撰写行业研究报告第一章。请按目录分批生成，保证标题和正文一一对应。\n"
        "第一章完整目录（用于保持口径一致）：\n"
        f"{full_outline}\n"
        "本次只生成以下小节：\n"
        f"{batch_outline}\n"
        "已完成内容摘录（用于衔接，不要重复）：\n"
        f"{context_excerpt}\n"
        "输出要求：\n"
        "1) 仅输出 JSON，不要解释，不要 Markdown。\n"
        f"2) JSON 必须使用以下结构（key/title 必须完全一致）：{strict_json}\n"
        "3) 不允许输出本批次之外的小节。\n"
        f"4) 每个小节段落要求如下：\n{min_paragraph_lines}\n"
        "5) key/title/paragraphs/sections 只能作为 JSON 字段名出现，paragraphs 正文里不得出现这些词。\n"
        f"{style_constraints}"
    )


def _build_chapter1_section_prompt(
    *,
    product_name: str,
    spec: Dict[str, Any],
    generated_sections: Sequence[Dict[str, Any]],
) -> str:
    key = str(spec["key"])
    title = str(spec["title"])
    slot_count = int(spec.get("slot_count", 6))
    min_paragraphs = max(2, min(8, slot_count // 3))
    chapter_outline = "\n".join(f"- {item['title']}" for item in CHAPTER1_SECTION_SPECS)
    context_excerpt = _build_chapter1_context_excerpt(generated_sections)
    style_constraints = _chapter1_style_constraints_text()
    return (
        f"产品：{product_name}\n"
        "任务背景：你正在写一份“行业研究报告”的第一章，最终要把所有小节拼接为一篇连贯正文。\n"
        "第一章完整小节目录（按顺序）：\n"
        f"{chapter_outline}\n"
        "已完成小节摘录（用于上下文衔接，避免重复）：\n"
        f"{context_excerpt}\n"
        f"请仅生成第一章中的一个小节：{title}。\n"
        "要求：\n"
        "1) 仅输出 JSON，不要输出解释，不要 Markdown。\n"
        f'2) JSON 格式固定为：{{"section":{{"key":"{key}","title":"{title}","paragraphs":["..."]}}}}。\n'
        f"3) paragraphs 至少 {min_paragraphs} 段，每段 110-220 字，段落必须是完整陈述句。\n"
        "4) key/title/paragraphs/sections 只能作为 JSON 字段名出现，paragraphs 正文里不得出现这些词。\n"
        "5) 与已完成小节保持术语和叙述口径一致，行文必须可直接拼接。\n"
        f"{style_constraints}"
    )



def _build_chapter1_repair_prompt(product_name: str, specs: Sequence[Dict[str, Any]]) -> str:
    lines = []
    for item in specs:
        slot_count = int(item["slot_count"])
        min_paragraphs = max(3, min(8, slot_count // 3))
        lines.append(
            f"- key={item['key']}，title={item['title']}：至少 {min_paragraphs} 段，每段 100-180 字，"
            "段落应是完整陈述句。"
        )
    requirements = "\n".join(lines)
    style_constraints = _chapter1_style_constraints_text()
    return (
        f"产品：{product_name}\n"
        "以下章节在第一轮生成中缺失，请只补写这些章节。\n"
        "只输出 JSON，格式：{\"sections\":[{\"key\":\"...\",\"title\":\"...\",\"paragraphs\":[\"...\"]}]}\n"
        "要求：\n"
        f"{requirements}\n"
        f"{style_constraints}"
        "不要输出任何 JSON 之外的文本。"
    )


def _build_chapter1_prompt(product_name: str) -> str:
    specs = [f"- {item['key']}（{item['title']}）：最终需要适配模板的 {item['slot_count']} 个段落槽位" for item in CHAPTER1_SECTION_SPECS]
    spec_text = "\n".join(specs)
    return (
        f"产品：{product_name}。为这个产品撰写行业研究报告。\n"
        "要求：不要出现具体统计数据，尽可能通过文字描述。全文建议 2200-3200 字，优先保证目录完整。注意用词用语。\n"
        "目标：撰写报告\n"
        "受众：专业人士\n"
        "类型：行业研究报告\n"
        "目录如下：\n"
        "一、背景与概述\n"
        "二、基本概念\n"
        "（一）定义\n"
        "（二）工作原理\n"
        "（三）产品属性\n"
        "（四）技术规范\n"
        "三、行业发展历程\n"
        "四、行业发展环境和趋势\n"
        "（一）行业发展环境\n"
        "（二）行业发展趋势\n"
        "五、行业供应链\n"
        "（一）上游原材料与核心零部件\n"
        "（二）中游制造与装配环节\n"
        "（三）下游应用行业与客户结构\n"
        "（四）渠道流通与交付协同\n"
        "（五）供应链风险与优化趋势\n"
        "\n"
        "为了写入模板，必须严格输出 JSON，结构如下：\n"
        '{"sections":[{"key":"background_overview","title":"背景与概述","paragraphs":["..."]}]}\n'
        "要求：\n"
        "1. sections 必须完整覆盖以下 9 个部分，key 和 title 必须完全一致。\n"
        f"{spec_text}\n"
        "2. 每个 paragraphs 元素都必须是一段完整、连贯、正式的研究报告段落，禁止输出“总体工作原理”“机械自锁结构”这类孤立小标题或短语。\n"
        "3. 不要使用项目符号、清单式罗列、词条式拆分，也不要输出除 JSON 之外的任何文字。\n"
        "4. 内容必须是面向专业人士的行业研究报告写法，不要口语化，不要写企业私有数据，不要写“待补充”。\n"
        "5. 每个一级部分至少 2 段；industry_trends 至少 4 段；industry_supply_chain 至少 6 段。\n"
        "6. industry_supply_chain 必须包含“（一）到（五）”五个小分类，每个小分类至少 1 段，不得遗漏。\n"
    )



def _lookup_company_profile_via_qcc_browser(requested_name: str) -> Dict[str, Any]:
    company_url, company_name = _search_qcc_exact_company_via_browser(requested_name)
    if not company_url:
        raise OtherProofError(
            f"企查查没有找到“{requested_name}”的精确结果，请确认公司全称，并保持 Chrome 已登录企查查。"
        )

    page_text = _read_qcc_detail_text_via_browser(company_url)
    profile = _parse_qcc_browser_profile(
        page_text=page_text,
        requested_name=requested_name,
        company_name=company_name or requested_name,
        company_url=company_url,
    )
    if _normalize_company_name(profile["company_name"]) != _normalize_company_name(requested_name):
        raise OtherProofError(f"企查查返回的企业不是“{requested_name}”，已停止生成。")
    return profile


def _search_qcc_exact_company_via_browser(requested_name: str) -> tuple[str, str]:
    search_url = "https://www.qcc.com/web/search?key=" + urllib.parse.quote(requested_name)
    script = f"""
(() => {{
  const normalize = (value) => (value || "")
    .trim()
    .replace(/（/g, "(")
    .replace(/）/g, ")")
    .replace(/\\s+/g, "");
  const target = normalize({json.dumps(requested_name, ensure_ascii=False)});
  const links = [...document.querySelectorAll('a[href*="/firm/"]')]
    .map((a) => ({{
      text: (a.innerText || a.textContent || "").trim(),
      href: a.href || ""
    }}))
    .filter((item) => item.text && item.href && item.href.includes("/firm/"));
  const exact = links.find((item) => normalize(item.text) === target);
  return JSON.stringify(exact || {{}});
}})()
""".strip()
    raw = _run_chrome_javascript(search_url, script)
    parsed = _parse_browser_json(raw)
    if isinstance(parsed, dict):
        return str(parsed.get("href") or "").strip(), str(parsed.get("text") or "").strip()
    return "", ""


def _read_qcc_detail_text_via_browser(company_url: str) -> str:
    return _run_chrome_javascript(company_url, "document.body.innerText")


def _parse_qcc_browser_profile(
    *,
    page_text: str,
    requested_name: str,
    company_name: str,
    company_url: str,
) -> Dict[str, Any]:
    actual_name = _extract_qcc_text_field(page_text, "企业名称") or company_name or requested_name
    registered_capital = _extract_qcc_text_field(page_text, "注册资本")
    established_date = _extract_qcc_text_field(page_text, "成立日期")
    legal_representative = _extract_qcc_text_field(page_text, "法定代表人") or _extract_qcc_text_field(page_text, "法人代表")
    company_address = (
        _extract_qcc_text_field(page_text, "注册地址")
        or _extract_qcc_text_field(page_text, "企业地址")
        or _extract_qcc_text_field(page_text, "地址")
    )
    main_business = _extract_qcc_text_field(page_text, "经营范围") or _extract_qcc_text_field(page_text, "主营业务")

    missing = []
    if not registered_capital:
        missing.append("注册资本")
    if not established_date:
        missing.append("成立日期")
    if not legal_representative:
        missing.append("法人代表")
    if not company_address:
        missing.append("企业地址")
    if not main_business:
        missing.append("主营业务")
    if missing:
        raise OtherProofError(f"企查查页面缺少“{requested_name}”的字段：{'、'.join(missing)}")

    return {
        "requested_name": requested_name,
        "company_name": actual_name,
        "company_url": company_url,
        "registered_capital": registered_capital,
        "established_date": established_date,
        "legal_representative": legal_representative,
        "company_address": company_address,
        "main_business": main_business,
        "matched_exactly": _normalize_company_name(actual_name) == _normalize_company_name(requested_name),
    }


def _run_chrome_javascript(url: str, javascript: str) -> str:
    script = f'''
tell application "Google Chrome"
    if (count of windows) = 0 then make new window
    set targetWindow to front window
    set targetTab to active tab of targetWindow
    set URL of targetTab to "{_escape_applescript_string(url)}"
end tell
repeat 80 times
    tell application "Google Chrome"
        if loading of targetTab is false then exit repeat
    end tell
    delay 0.25
end repeat
delay 0.5
tell application "Google Chrome"
    return execute targetTab javascript "{_escape_applescript_string(javascript)}"
end tell
'''.strip()
    result = subprocess.run(
        ["osascript"],
        cwd=Path(__file__).resolve().parent,
        input=script,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        if "Apple 事件中的 JavaScript" in message:
            raise OtherProofError(
                "Chrome 还没有打开“允许 Apple 事件中的 JavaScript”。请在 Chrome 菜单栏依次点击“查看 > 开发者 > 允许 Apple 事件中的 JavaScript”，然后再试。"
            )
        raise OtherProofError(message or "Chrome 页面读取失败")
    return result.stdout.strip()


def _escape_applescript_string(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _parse_browser_json(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, str):
            return json.loads(parsed)
        return parsed
    except json.JSONDecodeError as exc:
        raise OtherProofError("企查查浏览器返回结果无法解析") from exc


def _normalize_company_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "").strip().replace("（", "(").replace("）", ")"))


def _extract_qcc_text_field(text: str, label: str) -> str:
    patterns = {
        "企业名称": [
            r"企业名称\s+(.+?)复制",
        ],
        "注册资本": [
            r"注册资本[:：]\s*([^\n复制]+)",
            r"注册资本\s+([^\n]+?)\s+实缴资本",
        ],
        "成立日期": [
            r"成立日期[:：]\s*([0-9-]{10})",
            r"成立日期\s+([0-9-]{10})复制",
        ],
        "法定代表人": [
            r"法定代表人[:：]\s*\n?([^\n]+)",
            r"法定代表人\s+\n?([^\n]+?)\s{2,}",
        ],
        "注册地址": [
            r"注册地址\s+(.+?)(?:（邮编.*?|附近企业|\n)",
        ],
        "企业地址": [
            r"企业地址\s+(.+?)(?:（邮编.*?|附近企业|\n)",
        ],
        "地址": [
            r"地址[:：]\s*\n?([^\n]+)",
        ],
        "经营范围": [
            r"经营范围\s+(.+?)(?:复制|\n股东信息|\n股东|发生变更时通知我)",
        ],
        "主营业务": [
            r"主营业务\s+(.+?)(?:复制|\n股东信息|\n股东|发生变更时通知我)",
        ],
    }
    for pattern in patterns.get(label, []):
        match = re.search(pattern, text, re.S)
        if match:
            return _clean_browser_field(match.group(1))
    return ""


def _clean_browser_field(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()
    text = text.replace("复制", "").strip()
    return text


def search_aiqicha_candidates(client: httpx.Client, company_name: str, limit: int = 5) -> List[Dict[str, str]]:
    query = f'site:aiqicha.baidu.com "{company_name}" 爱企查'
    url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    response = client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=SEARCH_TIMEOUT)
    response.raise_for_status()
    matches = re.findall(r'class="result__a" href="([^"]+)".*?>(.*?)</a>', response.text, re.S)

    candidates: List[Dict[str, str]] = []
    seen: set[str] = set()
    for href, raw_title in matches:
        decoded_href = html.unescape(href)
        if decoded_href.startswith("//"):
            decoded_href = "https:" + decoded_href
        parsed = urllib.parse.urlparse(decoded_href)
        if parsed.netloc.endswith("duckduckgo.com"):
            target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
            decoded_href = urllib.parse.unquote(target) if target else decoded_href
        decoded_href = decoded_href.split("?")[0]
        if "aiqicha.baidu.com" not in decoded_href:
            continue
        if "/company_detail_" not in decoded_href and "/company_basic_" not in decoded_href:
            continue
        basic_url = decoded_href.replace("/company_detail_", "/company_basic_")
        if basic_url in seen:
            continue
        seen.add(basic_url)
        title = _strip_tags(raw_title)
        candidates.append({"title": title, "company_url": basic_url})
        if len(candidates) >= limit:
            break
    return candidates



def search_qcc_candidates(client: httpx.Client, company_name: str, limit: int = 5) -> List[Dict[str, str]]:
    query = f'site:qcc.com "{company_name}" 企查查'
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
    response = client.get(url, headers=BROWSER_HEADERS, timeout=SEARCH_TIMEOUT)
    response.raise_for_status()
    matches = re.findall(r'<li class="b_algo".*?<h2><a href="([^"]+)"[^>]*>(.*?)</a>', response.text, re.S)

    candidates: List[Dict[str, str]] = []
    seen: set[str] = set()
    for href, raw_title in matches:
        decoded_href = html.unescape(href).split("?")[0]
        parsed = urllib.parse.urlparse(decoded_href)
        if "qcc.com" not in parsed.netloc:
            continue
        if any(part in decoded_href for part in ["/web/search", "/weblogin", "/search"]):
            continue
        if decoded_href in seen:
            continue
        seen.add(decoded_href)
        title = _strip_tags(raw_title)
        candidates.append({"title": title, "company_url": decoded_href})
        if len(candidates) >= limit:
            break
    return candidates



def _lookup_company_profile(client: httpx.Client, requested_name: str) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    for provider in ("aiqicha", "qcc"):
        provider_candidates = _collect_provider_candidates(client, requested_name, provider)
        exact_profile = next((item for item in provider_candidates if item.get("company_name") == requested_name), None)
        if exact_profile is not None:
            return {"exact_profile": exact_profile, "candidates": [], "lookup_message": ""}
        candidates.extend(provider_candidates)

    unique_candidates = _dedupe_company_profiles(candidates)
    if unique_candidates:
        return {
            "exact_profile": None,
            "candidates": unique_candidates,
            "lookup_message": "没有自动找到精确匹配，请先确认候选企业。",
        }
    return {
        "exact_profile": None,
        "candidates": [],
        "lookup_message": "爱企查和企查查都没有返回可用结果，请手动填写第三章企业基本信息。",
    }



def _collect_provider_candidates(client: httpx.Client, requested_name: str, provider: str) -> List[Dict[str, Any]]:
    if provider == "aiqicha":
        raw_candidates = search_aiqicha_candidates(client, requested_name)
    elif provider == "qcc":
        raw_candidates = search_qcc_candidates(client, requested_name)
    else:
        return []

    parsed_candidates: List[Dict[str, Any]] = []
    for candidate in raw_candidates:
        company_url = str(candidate.get("company_url") or "").strip()
        if not company_url:
            continue
        try:
            profile = _fetch_profile_by_url(client, company_url, requested_name=requested_name)
        except Exception:
            continue
        if profile is None:
            continue
        profile["provider"] = provider
        parsed_candidates.append(profile)
    return parsed_candidates



def _fetch_profile_by_url(client: httpx.Client, company_url: str, *, requested_name: str) -> Dict[str, Any] | None:
    normalized_url = str(company_url or "").strip()
    if not normalized_url:
        return None
    parsed = urllib.parse.urlparse(normalized_url)
    host = parsed.netloc.lower()
    try:
        if "aiqicha.baidu.com" in host:
            profile = fetch_aiqicha_profile(client, normalized_url, requested_name=requested_name)
        elif "qcc.com" in host:
            profile = fetch_qcc_profile(client, normalized_url, requested_name=requested_name)
        else:
            return None
    except Exception:
        return None
    return profile



def _dedupe_company_profiles(candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.get("company_url") or candidate.get("company_name") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped



def fetch_aiqicha_profile(client: httpx.Client, company_url: str, *, requested_name: str) -> Dict[str, Any]:
    normalized_url = company_url.replace("/company_detail_", "/company_basic_").split("?")[0]
    response = client.get(normalized_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=AIQICHA_TIMEOUT)
    response.raise_for_status()
    text = response.text

    company_name = _extract_company_name(text)
    if not company_name:
        raise OtherProofError(f"无法解析爱企查页面：{normalized_url}")

    registered_capital = _extract_table_value(text, "注册资本")
    established_date = _extract_table_value(text, "成立日期")
    legal_representative = _extract_legal_representative(text)
    company_address = _extract_address(text)
    main_business = _extract_business_scope(text)

    return {
        "requested_name": requested_name,
        "company_name": company_name,
        "company_url": normalized_url,
        "registered_capital": registered_capital,
        "established_date": established_date,
        "legal_representative": legal_representative,
        "company_address": company_address,
        "main_business": main_business,
        "matched_exactly": company_name == requested_name,
    }



def fetch_qcc_profile(client: httpx.Client, company_url: str, *, requested_name: str) -> Dict[str, Any]:
    normalized_url = company_url.split("?")[0]
    response = client.get(normalized_url, headers=BROWSER_HEADERS, timeout=AIQICHA_TIMEOUT)
    response.raise_for_status()
    text = response.text
    if 'renderData' in text and 'aliyun_waf_aa' in text:
        raise OtherProofError(f"企查查页面被校验拦截：{normalized_url}")
    if '/weblogin' in str(response.url):
        raise OtherProofError(f"企查查页面需要登录：{normalized_url}")

    company_name = _extract_qcc_company_name(text)
    if not company_name:
        raise OtherProofError(f"无法解析企查查页面：{normalized_url}")

    return {
        "requested_name": requested_name,
        "company_name": company_name,
        "company_url": normalized_url,
        "registered_capital": _extract_qcc_field(text, "注册资本"),
        "established_date": _extract_qcc_field(text, "成立日期"),
        "legal_representative": _extract_qcc_field(text, "法定代表人") or _extract_qcc_field(text, "法人代表"),
        "company_address": _extract_qcc_field(text, "企业地址") or _extract_qcc_field(text, "注册地址") or _extract_qcc_field(text, "地址"),
        "main_business": _extract_qcc_field(text, "经营范围") or _extract_qcc_field(text, "主营业务"),
        "matched_exactly": company_name == requested_name,
    }



def _extract_qcc_company_name(text: str) -> str:
    patterns = [
        r'<title>(.*?) - 企查查</title>',
        r'"companyName"\s*:\s*"([^"]+)"',
        r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            return _clean_html_text(match.group(1))
    return ""



def _extract_qcc_field(text: str, label: str) -> str:
    patterns = [
        rf'{re.escape(label)}</span>.*?<span[^>]*>(.*?)</span>',
        rf'{re.escape(label)}</div>.*?<div[^>]*>(.*?)</div>',
        rf'"{re.escape(label)}"\s*:\s*"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            value = _clean_html_text(match.group(1))
            if value:
                return value
    return ""



def _extract_company_name(text: str) -> str:
    value = _extract_table_value(text, "企业名称")
    if value:
        return value
    match = re.search(r"<title>(.*?) - 工商信息查询 - 爱企查</title>", text, re.S)
    return _clean_html_text(match.group(1)) if match else ""



def _extract_table_value(text: str, label: str) -> str:
    patterns = [
        rf"<tr><td>{re.escape(label)}</td><td>(.*?)</td>",
        rf"<tr><td>{re.escape(label)}</td><td colspan=\"3\">(.*?)</td></tr>",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            return _clean_html_text(match.group(1))
    return ""



def _extract_legal_representative(text: str) -> str:
    match = re.search(r"<tr><td>法定代表人</td><td>(.*?)</td><td>经营状态</td>", text, re.S)
    return _clean_html_text(match.group(1)) if match else ""



def _extract_address(text: str) -> str:
    patterns = [
        r"<tr><td>(?:注册地址|住所)</td><td colspan=\"3\">(.*?)<span class=\"use-map\">",
        r"<tr><td>(?:注册地址|住所)</td><td colspan=\"3\">(.*?)</td></tr>",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            return _clean_html_text(match.group(1))
    return ""



def _extract_business_scope(text: str) -> str:
    match = re.search(r"<tr><td>经营范围</td><td colspan=\"3\">(.*?)</td></tr>", text, re.S)
    return _clean_html_text(match.group(1)) if match else ""



def _clean_html_text(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"<script.*?</script>", "", text, flags=re.S)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S)
    text = re.sub(r"<.*?>", "", text, flags=re.S)
    text = re.sub(r"\s+", " ", text)
    return text.strip()



def _strip_tags(raw: str) -> str:
    return _clean_html_text(raw)



def _key_from_title(title: str) -> str:
    title_map = {item["title"]: item["key"] for item in CHAPTER1_SECTION_SPECS}
    return title_map.get(title.strip(), "")



def _profile_field(profile: Dict[str, Any], key: str, company_name: str, label: str, warnings: List[str]) -> str:
    value = str(profile.get(key) or "").strip()
    if value:
        return value
    warnings.append(f"{company_name} 的{label}未获取到，已写入占位内容")
    return PLACEHOLDER_TEXT



def _report_date_from_payload(data: Dict[str, Any]) -> date:
    return date(int(data.get("year")), int(data.get("month")), int(data.get("day")))



def _format_cn_date(value: date) -> str:
    return f"{value.year} 年 {value.month} 月 {value.day} 日"



def _require_number_text(value: Any, field_name: str) -> str:
    number = _require_number(value, field_name)
    return _format_amount(number)



def _require_number(value: Any, field_name: str) -> float:
    text = str(value or "").replace(",", "").strip()
    if not text:
        raise OtherProofError(f"{field_name}不能为空")
    try:
        return float(text)
    except ValueError as exc:
        raise OtherProofError(f"{field_name}不是有效数字") from exc



def _normalize_percent_text(raw_value: Any, sale_text: str, total_market: float, field_name: str) -> str:
    text = str(raw_value or "").strip()
    if text:
        return _normalize_percent_only(text, field_name)
    sale_value = _require_number(sale_text, field_name.replace("占有率", "销售额"))
    if total_market <= 0:
        raise OtherProofError(f"{field_name}对应的市场规模必须大于 0")
    return f"{sale_value / total_market * 100:.2f}%"



def _normalize_percent_only(raw_value: Any, field_name: str) -> str:
    text = str(raw_value or "").strip()
    if not text:
        raise OtherProofError(f"{field_name}不能为空")
    if text.endswith("%"):
        number = text[:-1]
    else:
        number = text
    try:
        value = float(number)
    except ValueError as exc:
        raise OtherProofError(f"{field_name}不是有效百分比") from exc
    return f"{value:.2f}%"



def _percent_value(text: str) -> float:
    return float(text.replace("%", "").strip())



def _percent_to_ratio(text: str) -> float:
    return _percent_value(text) / 100.0



def _format_amount(value: float) -> str:
    formatted = f"{value:.2f}"
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted



def _scope_rank_parenthesized(scope: str, rank_number: int) -> str:
    return f"（{scope}第{rank_number}）"



def _section_index_cn(index: int) -> str:
    mapping = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
    return mapping.get(index, str(index))



def _ordinal_plain(number: int) -> str:
    mapping = {
        1: "第一",
        2: "第二",
        3: "第三",
        4: "第四",
        5: "第五",
        6: "第六",
        7: "第七",
        8: "第八",
        9: "第九",
        10: "第十",
    }
    return mapping.get(number, f"第{number}")



def _ordinal_with_suffix(number: int) -> str:
    return f"{_ordinal_plain(number)}名"



def _unique_preserve_order(items: Sequence[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
