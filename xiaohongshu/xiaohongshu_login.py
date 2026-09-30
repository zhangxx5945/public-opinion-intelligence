"""启动项目专用 Edge 配置，由用户正常登录小红书并保存会话。"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


PROJECT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = PROJECT_DIR / ".xiaohongshu_profile"
EDGE_PATHS = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)
HOME_URL = "https://www.xiaohongshu.com/explore"


def main() -> int:
    edge_path = next((path for path in EDGE_PATHS if path.exists()), None)
    if edge_path is None:
        print("未找到 Microsoft Edge。", file=sys.stderr)
        return 1

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print("正在打开项目专用 Edge……")
    print("请正常登录小红书；登录成功并看到首页后，关闭整个 Edge 窗口。")

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                executable_path=str(edge_path),
                headless=False,
                viewport={"width": 1440, "height": 900},
                args=["--start-maximized"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_event("close", timeout=15 * 60 * 1_000)
            except PlaywrightError:
                pass
            finally:
                try:
                    context.close()
                except PlaywrightError:
                    pass
    except PlaywrightError as exc:
        print(f"浏览器运行失败：{exc}", file=sys.stderr)
        return 1

    print(f"会话已保存：{PROFILE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
