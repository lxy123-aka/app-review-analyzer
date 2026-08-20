# ============================================================
# pipeline/cleaner.py
# 职责：评论数据清洗
#   - 去重（基于 review_id）
#   - 过滤空内容、纯符号、过短评论（< 5 字符）
#   - 字段标准化：日期、评分、版本号
#   - 语言检测：标记 en / other
#   - 输出清洗报告（原始 / 去重 / 过滤 / 最终数量）
# 依赖：utils.helpers (语言检测、日期标准化、is_meaningful)
# ============================================================
"""评论数据清洗模块。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from utils.helpers import detect_language, is_meaningful, normalize_date, strip_noise

logger = logging.getLogger(__name__)


_VERSION_PATTERN = re.compile(r"v?(\d+(?:\.\d+){0,3})", re.IGNORECASE)


@dataclass
class CleanReport:
    """清洗过程报告。"""

    raw_count: int = 0
    duplicate_count: int = 0
    empty_count: int = 0
    too_short_count: int = 0
    final_count: int = 0
    language_distribution: dict[str, int] = field(default_factory=dict)
    rating_distribution: dict[int, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "raw_count": self.raw_count,
            "duplicate_count": self.duplicate_count,
            "empty_count": self.empty_count,
            "too_short_count": self.too_short_count,
            "final_count": self.final_count,
            "removed_count": self.raw_count - self.final_count,
            "language_distribution": self.language_distribution,
            "rating_distribution": self.rating_distribution,
            "notes": self.notes,
        }


def _normalize_version(version: str) -> str:
    """从版本字符串中抽取形如 1.2.3 的版本号。"""
    if not version:
        return ""
    m = _VERSION_PATTERN.search(version)
    return m.group(1) if m else version.strip()


def clean(reviews: list[dict]) -> tuple[list[dict], CleanReport]:
    """执行清洗流程，返回 (清洗后评论, 报告)。

    Args:
        reviews: 原始评论列表（来自 collector）

    Returns:
        (cleaned, report)
    """
    report = CleanReport(raw_count=len(reviews))
    cleaned: list[dict] = []
    seen_ids: set[str] = set()

    for r in reviews:
        review_id = str(r.get("review_id") or "").strip()

        # 去重
        if review_id and review_id in seen_ids:
            report.duplicate_count += 1
            continue
        if review_id:
            seen_ids.add(review_id)

        content = strip_noise(str(r.get("content") or ""))

        # 空内容
        if not content:
            report.empty_count += 1
            continue

        # 过短或无意义（纯符号）
        if not is_meaningful(content, min_len=5):
            report.too_short_count += 1
            continue

        # 字段标准化
        rating_raw = r.get("rating")
        rating = int(rating_raw) if isinstance(rating_raw, (int, float)) else None
        if rating is None and str(rating_raw or "").strip().isdigit():
            rating = int(str(rating_raw).strip())

        version = _normalize_version(str(r.get("version") or ""))
        updated = normalize_date(str(r.get("updated") or ""))
        language = detect_language(content)

        item = {
            "review_id": review_id or f"NOID-{len(cleaned)+1}",
            "title": strip_noise(str(r.get("title") or "")),
            "content": content,
            "rating": rating,
            "version": version,
            "author": str(r.get("author") or "").strip(),
            "updated": updated,
            "country": str(r.get("country") or "us"),
            "language": language,
        }
        cleaned.append(item)

        # 统计
        report.language_distribution[language] = report.language_distribution.get(language, 0) + 1
        if rating is not None:
            report.rating_distribution[rating] = report.rating_distribution.get(rating, 0) + 1

    report.final_count = len(cleaned)

    if report.raw_count == 0:
        report.notes.append("未输入任何评论数据。")
    else:
        removed = report.raw_count - report.final_count
        report.notes.append(
            f"清洗完成: 原始 {report.raw_count} 条 → 去重 {report.duplicate_count} 条，"
            f"空内容 {report.empty_count} 条，过短/纯符号 {report.too_short_count} 条 → "
            f"保留 {report.final_count} 条（共移除 {removed} 条）。"
        )
        if report.final_count < 5:
            report.notes.append(
                "有效评论数量偏少（< 5 条），后续 AI 分析结果置信度可能较低。"
            )

    return cleaned, report
