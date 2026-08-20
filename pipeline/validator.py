# ============================================================
# pipeline/validator.py
# 职责：可追溯性验证
#   - 验证每个 PRD 需求是否都有评论证据支撑
#   - 验证每个测试用例是否都关联了有效需求
#   - 无证据支撑的结论标记为"假设"或建议移除
#   - 输出验证报告：通过率、未通过项及原因
# 依赖：无 LLM 依赖，纯规则校验（确定性、可重复）
# ============================================================
"""可追溯性验证模块。

本模块刻意不调用 LLM，确保验证结论是确定性的、可复现的，
符合"证据可追溯"的设计原则。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CheckItem:
    """单条验证项。"""

    target: str  # 需求 ID 或用例 ID
    target_type: str  # requirement | test_case
    passed: bool
    reason: str
    related_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "target_type": self.target_type,
            "passed": self.passed,
            "reason": self.reason,
            "related_ids": self.related_ids,
        }


@dataclass
class ValidationReport:
    """验证报告。"""

    total: int = 0
    passed: int = 0
    failed: int = 0
    items: list[CheckItem] = field(default_factory=list)
    assumed_conclusions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total * 100) if self.total else 0.0

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate, 2),
            "items": [i.as_dict() for i in self.items],
            "assumed_conclusions": self.assumed_conclusions,
            "notes": self.notes,
        }


def validate(
    prd: dict,
    test_suite: dict,
    reviews: list[dict],
    analysis: dict | None = None,
) -> ValidationReport:
    """执行可追溯性验证。

    Args:
        prd: PRD 字典（含 requirements / version_plan）
        test_suite: 测试套件字典（含 test_cases）
        reviews: 清洗后评论列表（用于核对 review_id 是否真实存在）
        analysis: 分析报告字典（可选，用于校验主题是否假设）

    Returns:
        ValidationReport
    """
    report = ValidationReport()
    valid_review_ids = {str(r.get("review_id")) for r in reviews}

    # ---- 1. 校验 PRD 需求 ----
    reqs = prd.get("requirements", []) or []
    req_id_set = {str(r.get("req_id", "")) for r in reqs}

    for r in reqs:
        report.total += 1
        req_id = str(r.get("req_id", ""))
        rids = [str(x) for x in (r.get("source_review_ids") or [])]
        valid_rids = [x for x in rids if x in valid_review_ids]

        if not rids:
            report.failed += 1
            report.items.append(
                CheckItem(
                    target=req_id,
                    target_type="requirement",
                    passed=False,
                    reason="需求未关联任何评论证据，标记为假设，建议移除或补充证据。",
                    related_ids=[],
                )
            )
            report.assumed_conclusions.append(req_id)
            continue

        if not valid_rids:
            report.failed += 1
            report.items.append(
                CheckItem(
                    target=req_id,
                    target_type="requirement",
                    passed=False,
                    reason="需求关联的 review_id 在数据中不存在（疑似 LLM 编造），"
                           "标记为假设，建议移除。",
                    related_ids=rids,
                )
            )
            report.assumed_conclusions.append(req_id)
            continue

        if len(valid_rids) < len(rids):
            report.passed += 1
            report.items.append(
                CheckItem(
                    target=req_id,
                    target_type="requirement",
                    passed=True,
                    reason=f"部分 review_id 无效（{len(rids)-len(valid_rids)} 个被过滤），"
                           f"但仍保留 {len(valid_rids)} 条有效证据。",
                    related_ids=valid_rids,
                )
            )
            continue

        report.passed += 1
        report.items.append(
            CheckItem(
                target=req_id,
                target_type="requirement",
                passed=True,
                reason=f"需求有 {len(valid_rids)} 条评论证据支撑。",
                related_ids=valid_rids,
            )
        )

    # ---- 2. 校验测试用例 ----
    cases = test_suite.get("test_cases", []) or []
    for c in cases:
        report.total += 1
        case_id = str(c.get("case_id", ""))
        req_id = str(c.get("req_id", ""))
        rids = [str(x) for x in (c.get("source_review_ids") or [])]
        valid_rids = [x for x in rids if x in valid_review_ids]

        if not req_id or req_id not in req_id_set:
            report.failed += 1
            report.items.append(
                CheckItem(
                    target=case_id,
                    target_type="test_case",
                    passed=False,
                    reason="测试用例未关联有效需求 ID。",
                    related_ids=[],
                )
            )
            continue

        if not rids:
            report.failed += 1
            report.items.append(
                CheckItem(
                    target=case_id,
                    target_type="test_case",
                    passed=False,
                    reason="测试用例未关联评论证据，无法验证是否覆盖用户问题。",
                    related_ids=[],
                )
            )
            continue

        if not valid_rids:
            report.failed += 1
            report.items.append(
                CheckItem(
                    target=case_id,
                    target_type="test_case",
                    passed=False,
                    reason="测试用例关联的 review_id 在数据中不存在。",
                    related_ids=rids,
                )
            )
            continue

        report.passed += 1
        report.items.append(
            CheckItem(
                target=case_id,
                target_type="test_case",
                passed=True,
                reason=f"测试用例关联需求 {req_id}，且有 {len(valid_rids)} 条评论证据。",
                related_ids=valid_rids,
            )
        )

    # ---- 3. 汇总 ----
    if report.assumed_conclusions:
        report.notes.append(
            f"发现 {len(report.assumed_conclusions)} 个无证据支撑的结论"
            f"（{', '.join(report.assumed_conclusions)}），已标记为假设。"
        )
    if report.total == 0:
        report.notes.append("无可验证对象（PRD 与测试用例均为空）。")
    else:
        report.notes.append(
            f"共验证 {report.total} 项，通过 {report.passed} 项，"
            f"未通过 {report.failed} 项，通过率 {round(report.pass_rate, 2)}%。"
        )
    return report


def to_markdown(report: ValidationReport) -> str:
    """将验证报告渲染为 Markdown。"""
    lines = ["# 可追溯性验证报告", ""]
    lines.append(
        f"- 总项: {report.total} | 通过: {report.passed} | 未通过: {report.failed} | "
        f"通过率: {round(report.pass_rate, 2)}%"
    )
    if report.assumed_conclusions:
        lines.append(f"- 假设/待移除: {', '.join(report.assumed_conclusions)}")
    lines.append("\n## 明细\n")
    lines.append("| 对象 | 类型 | 是否通过 | 原因 |")
    lines.append("|---|---|---|---|")
    for it in report.items:
        lines.append(
            f"| {it.target} | {it.target_type} | "
            f"{'通过' if it.passed else '未通过'} | {it.reason} |"
        )
    lines.append("\n## 说明")
    for n in report.notes:
        lines.append(f"- {n}")
    return "\n".join(lines)
