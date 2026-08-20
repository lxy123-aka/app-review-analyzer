# ============================================================
# pipeline/prd_generator.py
# 职责：基于分析结果调用 LLM 生成 PRD（产品需求文档）
#   - PRD 必须包含：问题概述、需求列表（每需求关联评论 ID）、
#     优先级排序、版本规划（可拆分多版本，每版含目标与范围）
#   - 每个需求可追溯到具体评论
#   - Prompt 全部写在代码中；防幻觉要求 review_id 必须真实
# 依赖：models.LLMClient、config.settings、utils.helpers.gen_id
# ============================================================
"""PRD 生成模块。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from config import settings
from models import LLMClient, LLMCallError
from utils.helpers import gen_id

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是一位资深产品经理，擅长基于用户真实反馈撰写可执行的产品需求文档（PRD）。

【输出要求】严格 JSON，禁止附加文字。
【防幻觉要求】
1. 每个需求的 source_review_ids 必须来自下方提供的真实评论 ID。
2. 不确定的判断必须标注 confidence=low。
3. 不编造评论内容，需求描述基于已有主题概括。
"""


def build_user_prompt(analysis_goal: str, topics: list[dict], reviews: list[dict]) -> str:
    """构造用户提示。"""
    lines = [f"【分析目标】\n{analysis_goal}\n", "【主题分析结果】"]
    for t in topics:
        lines.append(
            f'- 主题: {t.get("topic")}; 描述: {t.get("description")}; '
            f'情感: {t.get("sentiment")}; 置信度: {t.get("confidence")}; '
            f'关联评论: {t.get("review_ids")}; 矛盾证据: {t.get("conflicting_evidence")}'
        )
    lines.append("\n【评论原文索引】（用于核对 review_id）")
    for r in reviews:
        lines.append(
            f'- id={r.get("review_id")}: {str(r.get("content",""))[:120]}'
        )
    lines.append(
        "\n【输出 JSON 结构】\n"
        '{\n'
        '  "problem_summary": "问题概述",\n'
        '  "requirements": [\n'
        '    {\n'
        '      "req_id": "需求ID",\n'
        '      "title": "需求标题",\n'
        '      "description": "需求描述",\n'
        '      "priority": "P0|P1|P2|P3",\n'
        '      "source_topic": "来源主题",\n'
        '      "source_review_ids": ["评论ID"],\n'
        '      "acceptance_criteria": "验收标准",\n'
        '      "confidence": "high|medium|low"\n'
        '    }\n'
        '  ],\n'
        '  "version_plan": [\n'
        '    {\n'
        '      "version": "v1.0",\n'
        '      "goal": "本版本目标",\n'
        '      "scope": "本版本范围描述",\n'
        '      "req_ids": ["需求ID"],\n'
        '      "rationale": "拆分理由"\n'
        '    }\n'
        '  ]\n'
        '}'
    )
    lines.append("请严格按结构输出 JSON。")
    return "\n".join(lines)


@dataclass
class Requirement:
    req_id: str
    title: str
    description: str
    priority: str
    source_topic: str
    source_review_ids: list[str]
    acceptance_criteria: str
    confidence: str

    def as_dict(self) -> dict:
        return {
            "req_id": self.req_id,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "source_topic": self.source_topic,
            "source_review_ids": self.source_review_ids,
            "acceptance_criteria": self.acceptance_criteria,
            "confidence": self.confidence,
        }


@dataclass
class VersionPlan:
    version: str
    goal: str
    scope: str
    req_ids: list[str]
    rationale: str

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "goal": self.goal,
            "scope": self.scope,
            "req_ids": self.req_ids,
            "rationale": self.rationale,
        }


@dataclass
class PRD:
    problem_summary: str
    requirements: list[Requirement] = field(default_factory=list)
    version_plan: list[VersionPlan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "problem_summary": self.problem_summary,
            "requirements": [r.as_dict() for r in self.requirements],
            "version_plan": [v.as_dict() for v in self.version_plan],
            "notes": self.notes,
        }


def generate(
    analysis_goal: str,
    topics: list[dict],
    reviews: list[dict],
    *,
    llm_client: LLMClient | None = None,
    progress_callback: Callable[[str, dict], None] | None = None,
) -> PRD:
    """生成 PRD。

    Args:
        analysis_goal: 分析目标
        topics: analyzer 输出的主题列表（list[dict]）
        reviews: 清洗后评论列表（用于核对 ID）
        llm_client: LLM 客户端
        progress_callback: 进度回调
    """
    if not topics:
        return PRD(problem_summary="无可用主题，无法生成 PRD。",
                   notes=["输入主题为空，跳过 PRD 生成。"])

    if llm_client is None:
        llm_client = LLMClient()

    if progress_callback:
        progress_callback("prd_llm", {"model": settings.llm_model})

    valid_ids = {str(r.get("review_id")) for r in reviews}

    messages = LLMClient.build_messages(
        SYSTEM_PROMPT,
        build_user_prompt(analysis_goal, topics, reviews),
    )
    try:
        data = llm_client.chat_json(
            messages,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
    except LLMCallError as err:
        logger.error("PRD 生成 LLM 调用失败: %s", err)
        raise

    prd = PRD(problem_summary=str(data.get("problem_summary", "")).strip())

    reqs_raw = data.get("requirements", []) if isinstance(data, dict) else []
    if isinstance(reqs_raw, dict):
        reqs_raw = [reqs_raw]
    for r in reqs_raw:
        if not isinstance(r, dict):
            continue
        rids = [
            str(x) for x in (r.get("source_review_ids") or [])
            if str(x) in valid_ids
        ]
        req = Requirement(
            req_id=str(r.get("req_id", "")) or gen_id("REQ"),
            title=str(r.get("title", "")).strip(),
            description=str(r.get("description", "")).strip(),
            priority=str(r.get("priority", "P2")).strip().upper(),
            source_topic=str(r.get("source_topic", "")).strip(),
            source_review_ids=rids,
            acceptance_criteria=str(r.get("acceptance_criteria", "")).strip(),
            confidence=str(r.get("confidence", "low")).strip().lower(),
        )
        prd.requirements.append(req)

    # 优先级排序 P0 -> P3
    _order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    prd.requirements.sort(
        key=lambda r: (_order.get(r.priority, 9), -len(r.source_review_ids))
    )

    vps_raw = data.get("version_plan", []) if isinstance(data, dict) else []
    if isinstance(vps_raw, dict):
        vps_raw = [vps_raw]
    req_id_set = {r.req_id for r in prd.requirements}
    for v in vps_raw:
        if not isinstance(v, dict):
            continue
        ids = [str(x) for x in (v.get("req_ids") or []) if str(x) in req_id_set]
        prd.version_plan.append(
            VersionPlan(
                version=str(v.get("version", "")).strip(),
                goal=str(v.get("goal", "")).strip(),
                scope=str(v.get("scope", "")).strip(),
                req_ids=ids,
                rationale=str(v.get("rationale", "")).strip(),
            )
        )

    if not prd.version_plan and prd.requirements:
        prd.version_plan.append(
            VersionPlan(
                version="v1.0",
                goal="一次性覆盖所有需求",
                scope=", ".join(r.req_id for r in prd.requirements),
                req_ids=[r.req_id for r in prd.requirements],
                rationale="需求规模较小，无需拆分多版本。",
            )
        )

    prd.notes.append(
        f"生成 {len(prd.requirements)} 条需求，{len(prd.version_plan)} 个版本规划。"
    )
    return prd


def to_markdown(prd: PRD) -> str:
    """将 PRD 渲染为 Markdown（便于在 UI 与文档展示）。"""
    lines = ["# 产品需求文档 (PRD)", ""]
    lines.append(f"## 问题概述\n{prd.problem_summary}\n")
    lines.append("## 需求列表\n")
    lines.append(
        "| 需求ID | 标题 | 优先级 | 来源主题 | 关联评论 | 置信度 | 验收标准 |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for r in prd.requirements:
        lines.append(
            f"| {r.req_id} | {r.title} | {r.priority} | {r.source_topic} | "
            f"{', '.join(r.source_review_ids)} | {r.confidence} | "
            f"{r.acceptance_criteria} |"
        )
    lines.append("\n## 版本规划\n")
    for v in prd.version_plan:
        lines.append(f"### {v.version}")
        lines.append(f"- 目标: {v.goal}")
        lines.append(f"- 范围: {v.scope}")
        lines.append(f"- 包含需求: {', '.join(v.req_ids)}")
        lines.append(f"- 拆分理由: {v.rationale}\n")
    return "\n".join(lines)
