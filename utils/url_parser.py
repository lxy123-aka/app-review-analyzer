# ============================================================
# utils/url_parser.py
# 职责：解析 App Store 链接，从中提取 App ID 与区域代码
# 依赖：标准库 re / urllib.parse
# ============================================================
"""App Store URL 解析工具。

支持的链接格式示例：
    https://apps.apple.com/us/app/workout-for-women-fit-at-home/id1357527742
    https://apps.apple.com/app/id1357527742
    https://itunes.apple.com/us/app/.../id1357527742?mt=8
    也支持纯 App ID 字符串：1357527742

设计为纯函数，无副作用，便于单元测试与复用。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


# App ID 必须是纯数字，通常 9-10 位
_APP_ID_PATTERN = re.compile(r"/id(\d+)")
# 纯数字字符串
_NUMERIC_PATTERN = re.compile(r"^\d+$")
# /{country}/app/ 前缀中提取国家代码
_COUNTRY_PATTERN = re.compile(r"/([a-z]{2})/app/", re.IGNORECASE)


class URLParseError(ValueError):
    """URL 解析失败时抛出。"""


def parse_app_store_url(url_or_id: str) -> tuple[str, str]:
    """从用户输入解析出 (app_id, country)。

    Args:
        url_or_id: App Store 链接或纯数字 App ID

    Returns:
        (app_id_str, country_code_lower) 例如 ("1357527742", "us")

    Raises:
        URLParseError: 当无法识别 App ID 时
    """
    if not url_or_id or not isinstance(url_or_id, str):
        raise URLParseError("输入为空，请提供 App Store 链接或 App ID。")

    text = url_or_id.strip()

    # 情况 1：用户直接输入纯数字 App ID
    if _NUMERIC_PATTERN.match(text):
        return text, "us"

    # 情况 2：完整 URL
    # 去除可能的查询参数与片段
    parsed = urlparse(text if "://" in text else f"https://{text}")
    path = parsed.path or ""

    app_id_match = _APP_ID_PATTERN.search(path)
    if not app_id_match:
        raise URLParseError(
            f"无法从链接中解析出 App ID（缺少 /id<数字> 片段）: {url_or_id}"
        )
    app_id = app_id_match.group(1)

    # 解析国家代码（默认 us）
    country_match = _COUNTRY_PATTERN.search(path)
    country = country_match.group(1).lower() if country_match else "us"

    return app_id, country


def build_rss_url(app_id: str, country: str, page: int) -> str:
    """构造 Apple RSS 评论接口 URL。

    注意：page=1 通常只返回应用元信息，实际评论从 page=2 开始返回。
    """
    return (
        f"https://itunes.apple.com/{country}/rss/customerreviews/"
        f"id={app_id}/sortBy=mostRecent/page={page}/json"
    )


def build_app_info_url(app_id: str, country: str) -> str:
    """构造应用元信息查询接口（lookup）。"""
    return f"https://itunes.apple.com/lookup?id={app_id}&country={country}"
