# ============================================================
# pipeline/validator.py
# 职责：可追溯性验证
#   - 验证每个 PRD 需求是否都有评论证据支撑
#   - 验证每个测试用例是否都关联了有效需求
#   - 无证据支撑的结论标记为"假设"或建议移除
#   - 输出验证报告：通过率、未通过项及原因
#   - 可选：调用 LLM 进行语义一致性抽查
# 依赖：核心验证无 LLM 依赖（确定性）；语义校验可选依赖 LLMClient
# ============================================================
"""可追溯性验证模块。

核心验证（validate）刻意不调用 LLM，确保验证结论是确定性的、可复现的。
语义一致性校验（semantic_consistency_check）是可选的 LLM 抽查，
用于判断需求描述是否能从评论原文中合理推导出来。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.llm_client import LLMClient

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


# ============================================================
# 语义一致性校验（可选，LLM 抽查）
# ============================================================

_SEMANTIC_SYSTEM_PROMPT = """你是一位严格的需求审计员。

你的任务是判断一条产品需求描述是否能从用户评论原文中合理推导出来。

【规则】
1. 只基于评论原文判断，不要被需求描述本身的合理性说服。
2. 必须在评论中找到具体的、可引用的支撑内容。
3. 如果评论只提到相关话题但不足以支撑需求的具体内容，判定为不通过。
4. 严格输出 JSON，禁止附加文字。

【输出 JSON 结构】
{"passed": true/false, "reason": "简述判断依据", "confidence": "high/medium/low"}
"""


def _build_semantic_user_prompt(req: dict, review_texts: list[str]) -> str:
    """构造语义校验的用户提示。"""
    lines = [
        "【需求信息】",
        f'标题: {req.get("title", "")}',
        f'描述: {req.get("description", "")}',
        f'验收标准: {req.get("acceptance_criteria", "")}',
        "",
        "【对应的用户评论原文】",
    ]
    for i, text in enumerate(review_texts, 1):
        lines.append(f"评论{i}: {text}")
    lines.append("")
    lines.append("请判断上述需求是否能从这些评论中合理推导出来。严格输出 JSON。")
    return "\n".join(lines)


def semantic_consistency_check(
    prd: dict,
    reviews: list[dict],
    llm_client: "LLMClient",
    *,
    sample_size: int = 3,
) -> list[CheckItem]:
    """对 PRD 需求进行 LLM 语义一致性抽查。

    从 prd.requirements 中随机抽取 sample_size 条需求，
    对每条需求调用 LLM 判断其描述是否能从 source_review_ids 对应的评论原文中合理推导出来。

    Args:
        prd: PRD 字典（含 requirements）
        reviews: 清洗后评论列表
        llm_client: LLM 客户端实例
        sample_size: 抽查数量

    Returns:
        未通过的 CheckItem 列表（target_type="requirement_semantic"）
    """
    reqs = prd.get("requirements", []) or []
    if not reqs:
        return []

    reviews_by_id = {str(r.get("review_id")): r for r in reviews}
    # 随机抽取，但只抽有 source_review_ids 的需求
    candidates = [r for r in reqs if r.get("source_review_ids")]
    if not candidates:
        return []

    sample = random.sample(
        candidates, min(sample_size, len(candidates))
    )

    failed_items: list[CheckItem] = []

    from config import settings as _settings
    from utils.helpers import safe_json_loads

    for req in sample:
        req_id = str(req.get("req_id", ""))
        rids = [str(x) for x in (req.get("source_review_ids") or [])
                if str(x) in reviews_by_id]
        if not rids:
            continue

        review_texts = [
            str(reviews_by_id[rid].get("content", ""))[:500] for rid in rids
        ]

        messages = llm_client.build_messages(
            _SEMANTIC_SYSTEM_PROMPT,
            _build_semantic_user_prompt(req, review_texts),
        )

        try:
            data = llm_client.chat_json(
                messages,
                temperature=0.1,
                max_tokens=_settings.llm_max_tokens,
            )
        except Exception as err:
            logger.warning("语义校验 LLM 调用失败 (req=%s): %s", req_id, err)
            continue

        if not isinstance(data, dict):
            continue

        passed = bool(data.get("passed", True))
        reason = str(data.get("reason", ""))
        confidence = str(data.get("confidence", "medium"))

        if not passed:
            failed_items.append(
                CheckItem(
                    target=req_id,
                    target_type="requirement_semantic",
                    passed=False,
                    reason=f"语义校验未通过: {reason} (置信度: {confidence})",
                    related_ids=rids,
                )
            )

    return failed_items


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
