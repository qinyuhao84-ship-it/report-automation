#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from inference.models import ProviderConfig, ProviderMode, ProviderName
from inference.providers import (
    DoubaoProvider,
    MitataProvider,
    ProviderError,
    ProviderLoginRequiredError,
    YuanbaoProvider,
)


@dataclass(frozen=True)
class ProviderSpec:
    name: ProviderName
    base_url: str

    @property
    def env_key(self) -> str:
        return f"{self.name.value.upper()}_BROWSER_USER_DATA_DIR"


SPECS: List[ProviderSpec] = [
    ProviderSpec(name=ProviderName.MITATA, base_url="https://metaso.cn"),
    ProviderSpec(name=ProviderName.DOUBAO, base_url="https://www.doubao.com/chat/"),
    ProviderSpec(name=ProviderName.YUANBAO, base_url="https://yuanbao.tencent.com"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="初始化并校验秘塔/豆包/元宝登录态")
    parser.add_argument(
        "--root",
        default="data/inference/browser_profiles",
        help="浏览器持久化目录根路径（默认: data/inference/browser_profiles）",
    )
    parser.add_argument(
        "--providers",
        default="mitata,doubao,yuanbao",
        help="需要处理的平台，逗号分隔（mitata,doubao,yuanbao）",
    )
    parser.add_argument(
        "--query",
        default="中国 市场规模 2025 市占率",
        help="登录态校验时的检索词",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="仅做可用性探针，不打开人工登录窗口",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help="每个平台登录窗口自动等待秒数。大于 0 时不要求终端回车。",
    )
    return parser.parse_args()


def selected_specs(raw: str) -> List[ProviderSpec]:
    names = {item.strip().lower() for item in raw.split(",") if item.strip()}
    selected = [spec for spec in SPECS if spec.name.value in names]
    if not selected:
        raise ValueError("providers 为空，至少传入一个：mitata,doubao,yuanbao")
    return selected


def open_for_manual_login(profile_dir: Path, spec: ProviderSpec, wait_seconds: int) -> None:
    from playwright.sync_api import sync_playwright

    print(f"\n=== {spec.name.value} 登录步骤 ===")
    print(f"1) 已打开页面: {spec.base_url}")
    print("2) 请在浏览器中完成登录（如需扫码，请扫码）")
    if wait_seconds > 0:
        print(f"3) 程序会自动等待 {wait_seconds} 秒，然后进入探针验证")
    else:
        print("3) 登录完成后回到终端按回车，进入自动探针验证")

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(spec.base_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        if wait_seconds > 0:
            page.wait_for_timeout(wait_seconds * 1000)
        else:
            input("已完成登录请按回车继续：")
        context.close()


def build_provider(spec: ProviderSpec):
    cfg = ProviderConfig(
        name=spec.name,
        mode=ProviderMode.BROWSER,
        base_url=spec.base_url,
        timeout_seconds=30,
        max_results=3,
    )
    if spec.name == ProviderName.MITATA:
        return MitataProvider(cfg)
    if spec.name == ProviderName.DOUBAO:
        return DoubaoProvider(cfg)
    return YuanbaoProvider(cfg)


def probe_provider(spec: ProviderSpec, profile_dir: Path, query: str) -> Dict[str, object]:
    os.environ[spec.env_key] = str(profile_dir.resolve())
    os.environ[f"{spec.name.value.upper()}_BROWSER_HEADLESS"] = "1"

    provider = build_provider(spec)
    started = datetime.utcnow()
    try:
        hits = provider.search(query=query, max_results=1, market_path=["CN", "登录态校验"])
        ok = bool(hits)
        return {
            "provider": spec.name.value,
            "ok": ok,
            "status": "ok" if ok else "empty",
            "hits": len(hits),
            "sample_url": hits[0].url if hits else None,
            "sample_title": hits[0].title if hits else None,
            "checked_at": started.isoformat(),
            "error": None,
        }
    except ProviderLoginRequiredError as exc:
        return {
            "provider": spec.name.value,
            "ok": False,
            "status": "login_required",
            "hits": 0,
            "sample_url": None,
            "sample_title": None,
            "checked_at": started.isoformat(),
            "error": str(exc),
        }
    except ProviderError as exc:
        return {
            "provider": spec.name.value,
            "ok": False,
            "status": "provider_error",
            "hits": 0,
            "sample_url": None,
            "sample_title": None,
            "checked_at": started.isoformat(),
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "provider": spec.name.value,
            "ok": False,
            "status": "unexpected_error",
            "hits": 0,
            "sample_url": None,
            "sample_title": None,
            "checked_at": started.isoformat(),
            "error": str(exc),
        }


def write_env_file(path: Path, profiles: Dict[str, Path]) -> None:
    lines = [
        "# 自动生成：推理渠道登录态环境变量",
        "export INFER_BROWSER_HEADLESS=1",
    ]
    for key, profile in profiles.items():
        lines.append(f"export {key}='{profile.resolve()}'")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    specs = selected_specs(args.providers)

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)

    profiles: Dict[str, Path] = {}
    results: List[Dict[str, object]] = []

    for spec in specs:
        profile_dir = root / spec.name.value
        profile_dir.mkdir(parents=True, exist_ok=True)
        profiles[spec.env_key] = profile_dir

        if not args.skip_login:
            open_for_manual_login(profile_dir, spec, wait_seconds=max(0, args.wait_seconds))

        result = probe_provider(spec, profile_dir, args.query)
        results.append(result)
        print(
            f"[{spec.name.value}] status={result['status']} ok={result['ok']} "
            f"hits={result['hits']} error={result['error']}"
        )

    status_payload = {
        "generated_at": datetime.utcnow().isoformat(),
        "root": str(root.resolve()),
        "query": args.query,
        "results": results,
    }
    status_path = Path("data/inference/provider_login_status.json")
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(status_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    env_path = Path("data/inference/provider_sessions.env")
    write_env_file(env_path, profiles)
    print(f"\n状态文件: {status_path.resolve()}")
    print(f"环境文件: {env_path.resolve()}")
    print(f"可执行: source {env_path.resolve()}")

    all_ok = all(bool(item.get("ok")) for item in results)
    print(f"最终结论: {'全部可用' if all_ok else '仍有未打通渠道'}")
    return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
