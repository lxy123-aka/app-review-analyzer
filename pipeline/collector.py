# ============================================================
# pipeline/collector.py
# 职责：评论数据抓取
#   - 解析用户输入的 App Store 链接，提取 App ID 与国家
#   - 调用 Apple 官方 RSS Feed 接口循环抓取评论
#   - 速率限制（请求间隔 >= 1s）
#   - 支持从本地 JSON / CSV 文件导入（离线演示与测试）
#   - 抓取失败时抛出明确异常，绝不静默
# 依赖：requests、config.settings、utils.url_parser、utils.helpers
# 数据源：https://itunes.apple.com/{country}/rss/customerreviews/id={appId}/sortBy=mostRecent/page={page}/json
# ============================================================
"""评论数据抓取模块。"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import requests

from config import settings
from utils.url_parser import build_app_info_url, build_rss_url, parse_app_store_url

logger = logging.getLogger(__name__)


class CollectorError(RuntimeError):
    """抓取过程统一异常。"""


@dataclass
class AppMeta:
    """应用元信息。"""

    app_id: str
    name: str = ""
    developer: str = ""
    country: str = "us"
    primary_genre: str = ""
    track_id: str = ""

    def as_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "name": self.name,
            "developer": self.developer,
            "country": self.country,
            "primary_genre": self.primary_genre,
            "track_id": self.track_id,
        }


@dataclass
class CollectReport:
    """抓取过程报告。"""

    app: AppMeta
    pages_attempted: int = 0
    pages_succeeded: int = 0
    pages_failed: int = 0
    raw_count: int = 0
    per_page_counts: list[int] = field(default_factory=list)
    data_source: str = "rss"  # rss | json_file | csv_file
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "app": self.app.as_dict(),
            "pages_attempted": self.pages_attempted,
            "pages_succeeded": self.pages_succeeded,
            "pages_failed": self.pages_failed,
            "raw_count": self.raw_count,
            "per_page_counts": self.per_page_counts,
            "data_source": self.data_source,
            "notes": self.notes,
        }


def _normalize_raw_review(entry: dict, country: str) -> dict | None:
    """将 RSS feed 中的单条 entry 标准化为内部 dict。

    Apple RSS 返回结构较乱（嵌套 im:rating 等），此处做扁平化。
    """
    if not isinstance(entry, dict):
        return None
    try:
        # 评论主体在 entry.content.label，作者在 im:name 等
        review_id = (
            entry.get("id", {}).get("label")
            or entry.get("id", {}).get("attributes", {}).get("im:id")
            or ""
        )
        title = entry.get("title", {}).get("label", "") if isinstance(entry.get("title"), dict) else str(entry.get("title", ""))
        content = entry.get("content", {}).get("label", "") if isinstance(entry.get("content"), dict) else str(entry.get("content", ""))

        # 评分
        rating_attr = entry.get("im:rating", {})
        rating_str = ""
        if isinstance(rating_attr, dict):
            rating_str = rating_attr.get("label") or rating_attr.get("attributes", {}).get("amount", "")
        rating = int(rating_str) if str(rating_str).isdigit() else None

        # 版本号
        version_attr = entry.get("im:version", {})
        version = ""
        if isinstance(version_attr, dict):
            version = version_attr.get("label") or version_attr.get("attributes", {}).get("label", "")

        # 作者
        author = ""
        author_obj = entry.get("author", {})
        if isinstance(author_obj, dict):
            name_obj = author_obj.get("name", {})
            if isinstance(name_obj, dict):
                author = name_obj.get("label", "")
            elif isinstance(name_obj, str):
                author = name_obj

        # 日期
        updated = ""
        updated_obj = entry.get("updated", {})
        if isinstance(updated_obj, dict):
            updated = updated_obj.get("label", "")
        elif isinstance(updated_obj, str):
            updated = updated_obj

        if not review_id or not content:
            return None
        return {
            "review_id": str(review_id),
            "title": str(title),
            "content": str(content),
            "rating": rating,
            "version": str(version),
            "author": str(author),
            "updated": str(updated),
            "country": country,
        }
    except Exception as err:  # noqa: BLE001
        logger.debug("标准化单条评论失败: %s", err)
        return None


def _fetch_json(url: str) -> dict:
    """发起 GET 请求并解析为 JSON，失败抛出 CollectorError。"""
    try:
        resp = requests.get(url, timeout=settings.http_timeout, headers={
            "User-Agent": "Mozilla/5.0 (AppReviewAnalyzer/1.0)"
        })
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as err:
        raise CollectorError(f"请求失败: {url} -> {err}") from err
    except ValueError as err:
        raise CollectorError(f"返回非 JSON: {url} -> {err}") from err


def fetch_app_meta(app_id: str, country: str) -> AppMeta:
    """通过 lookup 接口获取应用元信息。"""
    url = build_app_info_url(app_id, country)
    try:
        data = _fetch_json(url)
    except CollectorError as err:
        # lookup 失败不致命，返回占位
        logger.warning("获取应用元信息失败: %s", err)
        return AppMeta(app_id=app_id, country=country)

    results = data.get("results") or []
    if not results:
        return AppMeta(app_id=app_id, country=country)
    info = results[0]
    return AppMeta(
        app_id=app_id,
        name=info.get("trackName", ""),
        developer=info.get("artistName", ""),
        country=country,
        primary_genre=info.get("primaryGenreName", ""),
        track_id=str(info.get("trackId", app_id)),
    )


def collect_from_rss(
    url_or_id: str,
    *,
    country: str | None = None,
    max_pages: int | None = None,
    progress_callback: Callable[[str, dict], None] | None = None,
) -> tuple[list[dict], CollectReport]:
    """从 Apple RSS Feed 抓取评论。

    Args:
        url_or_id: App Store 链接或纯 App ID
        country: 强制国家代码，默认从 URL 解析或 settings.default_country
        max_pages: 最大抓取页数，默认 settings.max_review_pages
        progress_callback: (stage, payload) 进度回调

    Returns:
        (reviews, report)

    Notes:
        * Apple RSS 接口 page=1 通常只返回应用信息，评论从 page=2 开始
          因此本函数从 page=2 开始抓取评论
        * 单页最多 50 条
        * 没有更多评论时返回空 entry 列表，函数停止抓取
    """
    # 解析 App ID；国家代码忽略链接中的区域，按题目要求强制美国区
    # （"即使在中国区打开了页面，数据也得是美国的"）
    app_id, _parsed_country = parse_app_store_url(url_or_id)
    country = (country or settings.default_country or "us").lower()
    if country != "us":
        # 题目硬性要求美国区，此处强制覆盖并在报告中透明说明
        country = "us"
    max_pages = max_pages if max_pages is not None else settings.max_review_pages

    app = fetch_app_meta(app_id, country)
    report = CollectReport(app=app, data_source="rss")
    report.notes.append(
        "数据源: Apple RSS Feed（美国区 us）。RSS 接口不保证返回所有历史评论，"
        "通常仅返回最近若干页（每页 ≤50 条）。评论数量受限是接口限制。"
    )
    report.notes.append(
        "按题目要求强制使用美国区(us)数据，忽略用户链接中的区域代码。"
    )

    reviews: list[dict] = []
    seen_ids: set[str] = set()
    # RSS 评论从 page=2 开始（page=1 多返回应用信息）
    for page in range(2, 2 + max_pages):
        report.pages_attempted += 1
        url = build_rss_url(app_id, country, page)
        if progress_callback:
            progress_callback("fetch_page", {"page": page, "url": url})

        try:
            data = _fetch_json(url)
        except CollectorError as err:
            report.pages_failed += 1
            report.notes.append(f"page={page} 抓取失败: {err}")
            logger.warning("page=%d 抓取失败: %s", page, err)
            time.sleep(settings.request_interval)
            continue

        entries = data.get("feed", {}).get("entry", [])
        # RSS 中 entry 可能是 dict 或 list
        if isinstance(entries, dict):
            entries = [entries]
        if not entries:
            report.notes.append(f"page={page} 无更多评论，停止抓取。")
            report.pages_succeeded += 1
            break

        page_count = 0
        for entry in entries:
            norm = _normalize_raw_review(entry, country)
            if not norm:
                continue
            if norm["review_id"] in seen_ids:
                continue
            seen_ids.add(norm["review_id"])
            reviews.append(norm)
            page_count += 1

        report.per_page_counts.append(page_count)
        report.raw_count = len(reviews)
        report.pages_succeeded += 1

        if progress_callback:
            progress_callback(
                "page_done",
                {"page": page, "page_count": page_count, "total": len(reviews)},
            )

        # 速率限制：每页请求间隔至少 REQUEST_INTERVAL 秒
        time.sleep(settings.request_interval)

    if not reviews:
        report.notes.append(
            "未抓取到任何评论。可能原因：1) App 在该区域无评论；"
            "2) RSS 接口临时无数据；3) App ID 错误。"
        )

    return reviews, report


# ---------------------------------------------------------------------------
# 从本地文件导入（离线演示）
# ---------------------------------------------------------------------------
def collect_from_json(file_obj: io.IOBase | io.StringIO | io.BytesIO) -> tuple[list[dict], CollectReport]:
    """从 JSON 文件读取评论数据。

    支持两种结构：
      1. 已标准化的 list[dict]（含 review_id/content 字段）
      2. Apple RSS 原始结构 {"feed": {"entry": [...]}}
    """
    raw = file_obj.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise CollectorError(f"JSON 文件解析失败: {err}") from err

    reviews: list[dict] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("review_id") and item.get("content"):
                reviews.append(item)
    elif isinstance(data, dict):
        entries = data.get("feed", {}).get("entry", [])
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            norm = _normalize_raw_review(entry, "us")
            if norm:
                reviews.append(norm)

    app = AppMeta(app_id="imported", name="(本地导入数据)", country="us")
    report = CollectReport(app=app, data_source="json_file", raw_count=len(reviews))
    report.notes.append("数据来源: 本地 JSON 文件导入。")
    if not reviews:
        report.notes.append("导入文件中未识别到有效评论。")
    return reviews, report


def collect_from_csv(file_obj: io.IOBase | io.StringIO | io.BytesIO) -> tuple[list[dict], CollectReport]:
    """从 CSV 文件读取评论数据。

    期望列：review_id, content, rating(可选), version(可选), updated(可选)
    """
    raw = file_obj.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    reviews: list[dict] = []
    for row in reader:
        rid = (row.get("review_id") or row.get("id") or "").strip()
        content = (row.get("content") or row.get("review") or "").strip()
        if not rid or not content:
            continue
        rating_val = row.get("rating") or ""
        rating = int(rating_val) if str(rating_val).strip().isdigit() else None
        reviews.append({
            "review_id": rid,
            "title": (row.get("title") or "").strip(),
            "content": content,
            "rating": rating,
            "version": (row.get("version") or "").strip(),
            "author": (row.get("author") or "").strip(),
            "updated": (row.get("updated") or "").strip(),
            "country": "us",
        })
    app = AppMeta(app_id="imported", name="(本地导入数据)", country="us")
    report = CollectReport(app=app, data_source="csv_file", raw_count=len(reviews))
    report.notes.append("数据来源: 本地 CSV 文件导入。")
    if not reviews:
        report.notes.append("导入文件中未识别到有效评论。")
    return reviews, report


def import_from_file(file_obj, filename: str) -> tuple[list[dict], CollectReport]:
    """根据扩展名选择 JSON/CSV 导入方式。"""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        return collect_from_csv(file_obj)
    # 默认按 JSON 处理（含 .json 与未知扩展名）
    return collect_from_json(file_obj)
