"""Open the project-specific Edge profile for normal Toutiao login."""

from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

try:
    from .toutiao_crawler import LOGIN_URL, PROFILE_DIR, create_context
except ImportError:
    from toutiao_crawler import LOGIN_URL, PROFILE_DIR, create_context


def main() -> int:
    print("即将打开项目专用 Edge。请手动登录/扫码并完成网站要求的验证。")
    print("登录成功后，请关闭整个 Edge 窗口；会话将保存在 toutiao/.toutiao_profile。")
    with sync_playwright() as playwright:
        context = create_context(playwright, PROFILE_DIR, headed=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            while context.pages:
                time.sleep(1)
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
