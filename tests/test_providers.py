from __future__ import annotations

from inference.providers import _is_doubao_login_redirect, _looks_like_noise_link


def test_doubao_login_redirect_detection():
    assert _is_doubao_login_redirect("https://www.doubao.com/chat/?from_logout=1")
    assert _is_doubao_login_redirect(
        "https://open.douyin.com/platform/oauth/connect?client_id=test"
    )
    assert _is_doubao_login_redirect("https://www.doubao.com/auth/callback")
    assert not _is_doubao_login_redirect("https://www.doubao.com/chat/")


def test_noise_link_blocks_douyin_oauth():
    assert _looks_like_noise_link("https://open.douyin.com/platform/oauth/connect?x=1")
    assert not _looks_like_noise_link("https://www.doubao.com/chat/")
