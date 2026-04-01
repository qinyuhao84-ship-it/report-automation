#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Dict, List

from playwright.sync_api import sync_playwright


DEFAULT_TARGETS: Dict[str, str] = {
    "mitata": "https://metaso.cn",
    "yuanbao": "https://yuanbao.tencent.com",
    "doubao": "https://www.doubao.com/chat/",
}


def _resolve_profile_dir(provider: str) -> Path:
    key = provider.upper()
    direct = os.getenv(f"{key}_BROWSER_USER_DATA_DIR")
    if direct and direct.strip():
        path = Path(direct.strip()).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    shared = os.getenv("INFER_BROWSER_USER_DATA_DIR")
    if shared and shared.strip():
        path = (Path(shared.strip()).expanduser().resolve() / provider)
        path.mkdir(parents=True, exist_ok=True)
        return path

    path = (Path("data") / "inference" / "browser_profiles" / provider).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _open_provider_login(provider: str, url: str) -> None:
    profile_dir = _resolve_profile_dir(provider)
    print(f"\n[{provider}] 打开登录页: {url}")
    print(f"[{provider}] 固定 profile 路径: {profile_dir}")
    print(f"[{provider}] 请在打开的浏览器窗口完成登录，登录后回到终端按回车继续。")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        input(">>> 已完成登录请按回车（若跳过直接回车）: ")
        try:
            context.close()
        except Exception:
            # 用户手动关闭窗口时允许继续，不中断后续平台登录
            pass

    print(f"[{provider}] 已写入登录态 profile。")


def main() -> None:
    parser = argparse.ArgumentParser(description="一次性完成秘塔/元宝/豆包的固定 profile 登录。")
    parser.add_argument(
        "--providers",
        nargs="+",
        default=["mitata", "yuanbao"],
        help="需要登录的平台，默认: mitata yuanbao",
    )
    args = parser.parse_args()

    providers: List[str] = []
    for item in args.providers:
        key = item.strip().lower()
        if key in DEFAULT_TARGETS:
            providers.append(key)
    if not providers:
        raise SystemExit("未识别到可登录平台，请传入 mitata / yuanbao / doubao")

    print("开始执行固定 profile 登录。关闭窗口前，登录 cookie 会保存在对应 profile 目录。")
    for provider in providers:
        _open_provider_login(provider, DEFAULT_TARGETS[provider])
    print("\n全部完成。后续推理会自动复用这些 profile。")


if __name__ == "__main__":
    main()
