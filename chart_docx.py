from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Sequence


DRAWING_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}


class ChartDataError(ValueError):
    pass


@dataclass
class ChartSeries:
    values: tuple[float, float, float]
    labels: tuple[str, str, str]


def build_chart_series_from_sources(
    sources: Sequence[dict[str, Any]],
    *,
    context_label: str,
) -> list[ChartSeries]:
    if not sources:
        raise ChartDataError(f"请至少添加一层{context_label}")

    result: list[ChartSeries] = []
    for idx, source in enumerate(sources, start=1):
        values: list[float] = []
        labels: list[str] = []
        for year, key in ((2023, "chart_2023"), (2024, "chart_2024"), (2025, "chart_2025")):
            raw = str(source.get(key) or "").replace(",", "").strip()
            if not raw:
                raise ChartDataError(f"第 {idx} 层{context_label}缺少 {year} 年市场规模")
            try:
                value = float(raw)
            except ValueError as exc:
                raise ChartDataError(f"第 {idx} 层{context_label}{year} 年市场规模不是有效数字") from exc
            values.append(value)
            labels.append(_format_number_label(value))
        result.append(ChartSeries(values=(values[0], values[1], values[2]), labels=(labels[0], labels[1], labels[2])))
    return result


def inject_market_charts_into_docx(
    *,
    document_root: ET.Element,
    file_map: dict[str, bytes],
    chart_series: Sequence[ChartSeries],
    context_label: str,
) -> None:
    chart_drawings = _find_chart_drawings(document_root)
    if len(chart_drawings) != len(chart_series):
        raise ChartDataError(
            f"{context_label}图表数量与模板图位不一致：数据层数 {len(chart_series)}，模板图位 {len(chart_drawings)}"
        )

    rel_path = "word/_rels/document.xml.rels"
    rel_xml = file_map.get(rel_path)
    if rel_xml is None:
        raise ChartDataError("Word 模板缺少 document.xml.rels，无法写入图表")

    rel_root = ET.fromstring(rel_xml)
    next_rid = _next_relationship_id(rel_root)
    existing_media = {path for path in file_map if path.startswith("word/media/")}

    for index, (drawing, series) in enumerate(zip(chart_drawings, chart_series), start=1):
        image_name = _next_unique_image_name(existing_media, f"chart_auto_{index}")
        media_path = f"word/media/{image_name}.png"
        existing_media.add(media_path)

        file_map[media_path] = render_market_chart_png(series)

        rid = f"rId{next_rid}"
        next_rid += 1
        ET.SubElement(
            rel_root,
            f"{{{DRAWING_NS['rel']}}}Relationship",
            {
                "Id": rid,
                "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
                "Target": f"media/{image_name}.png",
            },
        )

        blip = drawing.find(".//a:blip", DRAWING_NS)
        if blip is None:
            raise ChartDataError("模板图位缺少图片引用，无法替换图表")
        blip.set(f"{{{DRAWING_NS['r']}}}embed", rid)

    file_map[rel_path] = ET.tostring(rel_root, encoding="utf-8", xml_declaration=True)
    _ensure_png_content_type(file_map)


def render_market_chart_png(series: ChartSeries) -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise ChartDataError("缺少 Pillow 依赖，无法生成图表图片") from exc

    width, height = 1600, 980
    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)

    left, right = 170, width - 88
    top, bottom = 72, height - 120
    axis_color = "#c9c9c9"
    tick_color = "#4f4f4f"
    bar_color = "#5b92c7"
    grid_color = "#e9eef4"
    value_font = _load_heiti_font(ImageFont, 52)
    x_font = _load_heiti_font(ImageFont, 46)
    y_font = _load_heiti_font(ImageFont, 40)

    values = list(series.values)
    low, high, step = _compute_y_axis(values)

    # Axis baseline
    draw.line([(left, bottom), (right, bottom)], fill=axis_color, width=3)

    # Y labels
    for tick in _frange(low, high, step):
        y = _map_y(tick, low, high, top, bottom)
        draw.line([(left, y), (right, y)], fill=grid_color, width=2)
        tick_text = _format_number_label(tick)
        tick_bbox = draw.textbbox((0, 0), tick_text, font=y_font)
        tick_width = tick_bbox[2] - tick_bbox[0]
        tick_height = tick_bbox[3] - tick_bbox[1]
        draw.text((left - 20 - tick_width, y - tick_height / 2), tick_text, fill=tick_color, font=y_font)

    years = ["2023", "2024", "2025"]
    span = (right - left) / 3
    bar_width = int(span * 0.4)

    for i, value in enumerate(values):
        center_x = left + span * (i + 0.5)
        x1 = int(center_x - bar_width / 2)
        x2 = int(center_x + bar_width / 2)
        y = _map_y(value, low, high, top, bottom)

        draw.rectangle([(x1, y), (x2, bottom)], fill=bar_color)

        value_text = series.labels[i]
        value_bbox = draw.textbbox((0, 0), value_text, font=value_font)
        value_width = value_bbox[2] - value_bbox[0]
        value_height = value_bbox[3] - value_bbox[1]
        _draw_bold_text(
            draw=draw,
            x=center_x - value_width / 2,
            y=y - value_height - 18,
            text=value_text,
            font=value_font,
            fill="#3e3e3e",
        )

        year_text = years[i]
        year_bbox = draw.textbbox((0, 0), year_text, font=x_font)
        year_width = year_bbox[2] - year_bbox[0]
        draw.text((center_x - year_width / 2, bottom + 18), year_text, fill=tick_color, font=x_font)

    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _compute_y_axis(values: Sequence[float]) -> tuple[float, float, float]:
    max_v = max(0.0, max(values))
    low = 0.0
    step = _pick_axis_step(max_v)
    high = math.ceil(max_v / step) * step
    if high <= low:
        high = step
    return low, high, step


def _pick_axis_step(max_v: float) -> float:
    if max_v <= 0:
        return 1.0

    preferred_steps: list[float] = []
    for power in range(0, 9):
        base = 10 ** power
        preferred_steps.extend([1.0 * base, 5.0 * base])

    valid_steps = [step for step in preferred_steps if math.ceil(max_v / step) + 1 <= 10]
    if not valid_steps:
        return preferred_steps[-1]

    target_ticks = 7
    return min(
        valid_steps,
        key=lambda step: (
            abs((math.ceil(max_v / step) + 1) - target_ticks),
            step,
        ),
    )


def _map_y(value: float, low: float, high: float, top: int, bottom: int) -> int:
    ratio = (value - low) / (high - low)
    ratio = min(max(ratio, 0.0), 1.0)
    return int(bottom - ratio * (bottom - top))


def _frange(start: float, stop: float, step: float) -> list[float]:
    values: list[float] = []
    cur = start
    while cur <= stop + 1e-9:
        values.append(cur)
        cur += step
    return values


def _find_chart_drawings(document_root: ET.Element) -> list[ET.Element]:
    body = document_root.find(".//w:body", DRAWING_NS)
    if body is None:
        raise ChartDataError("Word 模板缺少正文结构")

    children = list(body)
    results: list[ET.Element] = []
    for idx, child in enumerate(children):
        if child.tag != f"{{{DRAWING_NS['w']}}}p":
            continue
        drawing = child.find(".//w:drawing", DRAWING_NS)
        if drawing is None:
            continue

        prev_text = _find_neighbor_paragraph_text(children, idx, -1)
        next_text = _find_neighbor_paragraph_text(children, idx, +1)
        if _is_chart_title(prev_text) and next_text.startswith("数据来源"):
            results.append(drawing)
    return results


def _is_chart_title(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("图表")


def _find_neighbor_paragraph_text(children: list[ET.Element], start: int, step: int) -> str:
    i = start + step
    while 0 <= i < len(children):
        node = children[i]
        if node.tag == f"{{{DRAWING_NS['w']}}}p":
            text = "".join(t.text or "" for t in node.findall(".//w:t", DRAWING_NS)).strip()
            if text:
                return text
        i += step
    return ""


def _next_relationship_id(rel_root: ET.Element) -> int:
    max_id = 0
    for rel in rel_root.findall("rel:Relationship", DRAWING_NS):
        rid = rel.get("Id") or ""
        if rid.startswith("rId") and rid[3:].isdigit():
            max_id = max(max_id, int(rid[3:]))
    return max_id + 1


def _next_unique_image_name(existing_media: set[str], prefix: str) -> str:
    candidate = prefix
    idx = 1
    while f"word/media/{candidate}.png" in existing_media:
        idx += 1
        candidate = f"{prefix}_{idx}"
    return candidate


def _ensure_png_content_type(file_map: dict[str, bytes]) -> None:
    content_types_path = "[Content_Types].xml"
    content_xml = file_map.get(content_types_path)
    if content_xml is None:
        raise ChartDataError("Word 模板缺少 [Content_Types].xml")

    ET.register_namespace("", DRAWING_NS["ct"])
    root = ET.fromstring(content_xml)
    defaults = root.findall("ct:Default", DRAWING_NS)
    has_png = any((node.get("Extension") or "").lower() == "png" for node in defaults)
    if not has_png:
        ET.SubElement(
            root,
            f"{{{DRAWING_NS['ct']}}}Default",
            {"Extension": "png", "ContentType": "image/png"},
        )
        file_map[content_types_path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _format_number_label(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    text = f"{value:.2f}"
    return text.rstrip("0").rstrip(".")


def _load_heiti_font(image_font_module, size: int):
    candidates = [
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/SimHei.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for font_path in candidates:
        try:
            return image_font_module.truetype(font_path, size=size)
        except OSError:
            continue
    try:
        return image_font_module.load_default(size=size)
    except TypeError:
        return image_font_module.load_default()


def _draw_bold_text(*, draw, x: float, y: float, text: str, font, fill: str) -> None:
    for dx, dy in ((0, 0), (0.35, 0)):
        draw.text((x + dx, y + dy), text, fill=fill, font=font)
