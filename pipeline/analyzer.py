# ============================================================
# pipeline/analyzer.py
# 职责：AI 动态分析（核心模块）
#   - 必须调用 LLM，不能只用关键词匹配
#   - 动态主题发现：让 LLM 从评论中自动总结 5-10 个问题主题
#     （不预设固定类别，能处理任意 App）
#   - 问题归类：每条评论归入动态发现的主题
#   - 情感分析：正面/负面/中性
#   - 证据评估：每个主题评估证据充分性、矛盾反馈、置信度
#   - 每个结果必须含 review_ids、样本数、置信度、矛盾证据
#   - Prompt 全部写在代码中，便于面试官审查
#   - 防幻觉：Prompt 强制 LLM 只基于提供的评论作答，
#     不确定时明确标注，禁止编造 review_id
# 依赖：models.LLMClient、config.settings
# ============================================================
"""AI 动态分析模块。

工作流程：
1. 将清洗后的评论切片喂给 LLM，要求其返回结构化 JSON：
   - 5-10 个动态发现的主题（topic / description / review_ids / sentiment /
     confidence / conflicting_evidence / sample_count）
2. 对每条评论打标：归入主题 ID、情感、置信度
3. 评估证据充分性与矛盾反馈

所有 LLM 输出均为结构化 JSON，并要求 review_id 必须来自真实输入，
从而实现"证据可追溯"。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from config import settings
from models import LLMClient, LLMCallError

logger = logging.getLogger(__name__)

# 每批喂给 LLM 的最大评论数（控制 token 与上下文）
_BATCH_SIZE = 30


# ============================================================
# Prompt 设计（集中在此，便于审查）
# ============================================================

# 系统提示：定义角色、输出约束、防幻觉要求
SYSTEM_PROMPT = """你是一位资深的产品评论分析师，擅长从 App Store 用户评论中
发现真实的产品问题与诉求。

【输出要求】
1. 必须返回严格的 JSON，不要附加任何解释性文字。
2. 主题必须是动态发现的（5-10 个），不可使用固定类别。
   主题命名应来自评论中实际出现的具体问题（例如"订阅定价过高"，
   而非笼统的"价格"）。
3. 每个主题必须包含以下字段：
   - topic: 主题名称（简短）
   - description: 问题描述（基于评论原文概括）
   - sentiment: positive | negative | neutral
   - review_ids: 支撑该主题的评论 ID 列表
   - sample_count: review_ids 的数量
   - confidence: high | medium | low（证据置信度）
   - conflicting_evidence: 是否存在矛盾反馈（如有则列出，无则空数组）

【防幻觉要求】
1. 你只能基于"用户实际提供的评论数据"作答。
2. review_ids 必须严格来自输入的评论 ID，禁止编造任何 ID。
3. 任何无法从评论中得到充分证据的判断，confidence 必须标为 low。
4. 若证据不足以支撑某结论，宁可少给主题，也不要凑数。
5. 若同一主题在不同评论中出现矛盾反馈（如既有人称赞又有人抱怨），
   必须在 conflicting_evidence 中如实记录。
"""

# 用户提示：注入评论数据
def build_user_prompt(reviews_batch: list[dict], analysis_goal: str) -> str:
    """构造用户提示，将评论数据注入。"""
    lines = [f"【分析目标】\n{analysis_goal}\n"]
    lines.append("【评论数据】（注意 review_id 必须来自下方真实 ID）")
    for r in reviews_batch:
        rid = r.get("review_id", "")
        rating = r.get("rating", "")
        version = r.get("version", "")
        content = r.get("content", "").replace("\n", " ")
        lines.append(
            f'- {{id: "{rid}", rating: {rating}, version: "{version}", '
            f'text: "{content}"}}'
        )
    lines.append("")
    lines.append("【输出 JSON 结构】")
    lines.append(
        '{"topics": [{"topic":"","description":"","sentiment":"",'
        '"review_ids":[],"sample_count":0,"confidence":"","conflicting_evidence":[]}], '
        '"per_review": [{"review_id":"","primary_topic":"","sentiment":"","note":""}]}'
    )
    lines.append("请严格按上述结构输出 JSON。")
    return "\n".join(lines)


# ============================================================
# 数据结构
# ============================================================
@dataclass
class Topic:
    """单个分析主题。"""

    topic: str
    description: str
    sentiment: str
    review_ids: list[str]
    sample_count: int
    confidence: str
    conflicting_evidence: list[str]

    def as_dict(self) -> dict:
        return {
            "topic": self.topic,
            "description": self.description,
            "sentiment": self.sentiment,
            "review_ids": self.review_ids,
            "sample_count": self.sample_count,
            "confidence": self.confidence,
            "conflicting_evidence": self.conflicting_evidence,
        }


@dataclass
class ReviewTag:
    """单条评论的分类标签。"""

    review_id: str
    primary_topic: str
    sentiment: str
    note: str

    def as_dict(self) -> dict:
        return {
            "review_id": self.review_id,
            "primary_topic": self.primary_topic,
            "sentiment": self.sentiment,
            "note": self.note,
        }


@dataclass
class AnalysisReport:
    """分析过程报告。"""

    topics: list[Topic] = field(default_factory=list)
    per_review: list[ReviewTag] = field(default_factory=list)
    review_count: int = 0
    batches: int = 0
    notes: list[str] = field(default_factory=list)
    evidence_evaluation: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "topics": [t.as_dict() for t in self.topics],
            "per_review": [r.as_dict() for r in self.per_review],
            "review_count": self.review_count,
            "batches": self.batches,
            "notes": self.notes,
            "evidence_evaluation": self.evidence_evaluation,
        }


# ============================================================
# 主分析入口
# ============================================================
def analyze(
    reviews: list[dict],
    analysis_goal: str,
    *,
    llm_client: LLMClient | None = None,
    progress_callback: Callable[[str, dict], None] | None = None,
) -> AnalysisReport:
    """对清洗后的评论执行 AI 动态分析。

    Args:
        reviews: 清洗后的评论列表
        analysis_goal: 用户输入的分析目标
        llm_client: 注入 LLM 客户端（便于测试 mock）
        progress_callback: 进度回调

    Returns:
        AnalysisReport

    Raises:
        LLMCallError: LLM 调用失败
        ValueError: 输入为空
    """
    if not reviews:
        raise ValueError("无评论可分析，请先抓取/导入数据。")

    if llm_client is None:
        llm_client = LLMClient()

    report = AnalysisReport(review_count=len(reviews))
    # 全部评论 ID 集合，用于事后校验 LLM 是否编造了 ID
    valid_ids: set[str] = {str(r.get("review_id")) for r in reviews}

    batches = [
        reviews[i : i + _BATCH_SIZE]
        for i in range(0, len(reviews), _BATCH_SIZE)
    ]
    report.batches = len(batches)

    merged_topics: dict[str, Topic] = {}
    per_review_all: list[ReviewTag] = []

    for idx, batch in enumerate(batches, start=1):
        if progress_callback:
            progress_callback(
                "analyze_batch",
                {"batch": idx, "total_batches": len(batches), "size": len(batch)},
            )

        messages = LLMClient.build_messages(
            SYSTEM_PROMPT,
            build_user_prompt(batch, analysis_goal),
        )
        try:
            data = llm_client.chat_json(
                messages,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
            )
        except LLMCallError as err:
            report.notes.append(f"第 {idx} 批 LLM 调用失败: {err}")
            logger.error("第 %d 批 LLM 调用失败: %s", idx, err)
            raise

        # 解析主题
        topics_raw = data.get("topics", []) if isinstance(data, dict) else []
        if isinstance(topics_raw, dict):
            topics_raw = [topics_raw]
        for t in topics_raw:
            if not isinstance(t, dict):
                continue
            name = str(t.get("topic", "")).strip()
            if not name:
                continue
            # 过滤编造的 review_id（防幻觉）
            rids = [str(x) for x in (t.get("review_ids") or []) if str(x) in valid_ids]
            topic = Topic(
                topic=name,
                description=str(t.get("description", "")).strip(),
                sentiment=str(t.get("sentiment", "neutral")).strip().lower(),
                review_ids=rids,
                sample_count=len(rids),
                confidence=str(t.get("confidence", "low")).strip().lower(),
                conflicting_evidence=[
                    str(x) for x in (t.get("conflicting_evidence") or [])
                ],
            )
            # 跨批次合并同名主题
            if name in merged_topics:
                existing = merged_topics[name]
                existing.review_ids.extend(
                    rid for rid in rids if rid not in existing.review_ids
                )
                existing.sample_count = len(existing.review_ids)
                for ce in topic.conflicting_evidence:
                    if ce not in existing.conflicting_evidence:
                        existing.conflicting_evidence.append(ce)
                # 置信度降级规则：任一批为 low 则整体降为 low
                if topic.confidence == "low" or existing.confidence == "low":
                    existing.confidence = "low"
                elif topic.confidence == "medium" or existing.confidence == "medium":
                    existing.confidence = "medium"
            else:
                merged_topics[name] = topic

        # 解析 per_review
        per_raw = data.get("per_review", []) if isinstance(data, dict) else []
        if isinstance(per_raw, dict):
            per_raw = [per_raw]
        for pr in per_raw:
            if not isinstance(pr, dict):
                continue
            rid = str(pr.get("review_id", "")).strip()
            if rid and rid in valid_ids:
                per_review_all.append(
                    ReviewTag(
                        review_id=rid,
                        primary_topic=str(pr.get("primary_topic", "")).strip(),
                        sentiment=str(pr.get("sentiment", "neutral")).strip().lower(),
                        note=str(pr.get("note", "")).strip(),
                    )
                )

    report.topics = list(merged_topics.values())
    # 按样本数降序
    report.topics.sort(key=lambda t: t.sample_count, reverse=True)
    report.per_review = per_review_all

    # 证据评估汇总
    _evaluate_evidence(report)

    if not report.topics:
        report.notes.append("LLM 未能从评论中发现明确主题，可能评论数据不足或内容过短。")
    else:
        report.notes.append(
            f"共发现 {len(report.topics)} 个主题，覆盖 {len(per_review_all)} 条评论标签。"
        )

    return report


def _evaluate_evidence(report: AnalysisReport) -> None:
    """对每个主题生成证据评估说明。"""
    for t in report.topics:
        if t.sample_count == 0:
            report.evidence_evaluation[t.topic] = (
                "无证据支撑（review_id 已被过滤），判定为假设，建议移除。"
            )
            continue
        parts = [f"样本数 {t.sample_count}"]
        if t.conflicting_evidence:
            parts.append(f"存在 {len(t.conflicting_evidence)} 条矛盾反馈")
        parts.append(f"置信度 {t.confidence}")
        if t.sample_count >= 5 and not t.conflicting_evidence:
            parts.append("证据充分")
        elif t.sample_count >= 3:
            parts.append("证据中等")
        else:
            parts.append("证据偏少")
        report.evidence_evaluation[t.topic] = "；".join(parts) + "。"
