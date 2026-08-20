# ============================================================
# pipeline/testcase_generator.py
# 职责：基于 PRD 中的每个需求调用 LLM 生成测试用例
#   - 每个测试用例必须关联：需求 ID、来源评论 ID、测试步骤、预期结果
#   - 用例要能验证需求是否解决了评论中提出的具体问题
#   - Prompt 全部写在代码中；防幻觉要求 review_id 真实
# 依赖：models.LLMClient、config.settings、utils.helpers.gen_id
# ============================================================
"""测试用例生成模块。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from config import settings
from models import LLMClient, LLMCallError
from utils.helpers import gen_id

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是一位资深 QA 工程师，擅长基于产品需求撰写可执行的测试用例。

【输出要求】严格 JSON，禁止附加文字。
【防幻觉要求】
1. source_review_ids 必须来自下方提供的真实评论 ID。
2. 测试用例必须能验证需求是否解决了评论中提出的具体问题。
3. 不确定的需求/评论关联时，标注 confidence=low。
"""


def build_user_prompt(reqs: list[dict], reviews: list[dict]) -> str:
    """构造用户提示。"""
    lines = ["【需求列表】"]
    for r in reqs:
        lines.append(
            f'- {{req_id: "{r.get("req_id")}", title: "{r.get("title")}", '
            f'description: "{r.get("description")}", '
            f'acceptance_criteria: "{r.get("acceptance_criteria")}", '
            f'source_review_ids: {r.get("source_review_ids")}}}'
        )
    lines.append("\n【评论原文索引】（用于核对 review_id）")
    for rv in reviews:
        lines.append(
            f'- id={rv.get("review_id")}: {str(rv.get("content",""))[:120]}'
        )
    lines.append(
        "\n【输出 JSON 结构】\n"
        '{\n'
        '  "test_cases": [\n'
        '    {\n'
        '      "case_id": "用例ID",\n'
        '      "req_id": "需求ID",\n'
        '      "title": "用例标题",\n'
        '      "preconditions": "前置条件",\n'
        '      "steps": ["步骤1", "步骤2"],\n'
        '      "expected_result": "预期结果",\n'
        '      "source_review_ids": ["评论ID"],\n'
        '      "type": "functional|regression|edge_case",\n'
        '      "confidence": "high|medium|low"\n'
        '    }\n'
        '  ]\n'
        '}'
    )
    lines.append("请严格按结构输出 JSON。")
    return "\n".join(lines)


@dataclass
class TestCase:
    case_id: str
    req_id: str
    title: str
    preconditions: str
    steps: list[str]
    expected_result: str
    source_review_ids: list[str]
    type: str
    confidence: str

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "req_id": self.req_id,
            "title": self.title,
            "preconditions": self.preconditions,
            "steps": self.steps,
            "expected_result": self.expected_result,
            "source_review_ids": self.source_review_ids,
            "type": self.type,
            "confidence": self.confidence,
        }


@dataclass
class TestSuite:
    test_cases: list[TestCase] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "test_cases": [t.as_dict() for t in self.test_cases],
            "notes": self.notes,
        }


def generate(
    requirements: list[dict],
    reviews: list[dict],
    *,
    llm_client: LLMClient | None = None,
    progress_callback: Callable[[str, dict], None] | None = None,
) -> TestSuite:
    """生成测试用例。

    Args:
        requirements: PRD 中的需求（list[dict]，每项含 req_id / source_review_ids 等）
        reviews: 清洗后评论列表（用于核对 ID）
        llm_client: LLM 客户端
        progress_callback: 进度回调
    """
    if not requirements:
        return TestSuite(notes=["PRD 无需求，跳过测试用例生成。"])

    if llm_client is None:
        llm_client = LLMClient()

    if progress_callback:
        progress_callback(
            "testcase_llm",
            {"req_count": len(requirements), "model": settings.llm_model},
        )

    valid_ids = {str(r.get("review_id")) for r in reviews}
    valid_req_ids = {str(r.get("req_id")) for r in requirements}

    messages = LLMClient.build_messages(
        SYSTEM_PROMPT,
        build_user_prompt(requirements, reviews),
    )
    try:
        data = llm_client.chat_json(
            messages,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
    except LLMCallError as err:
        logger.error("测试用例生成 LLM 调用失败: %s", err)
        raise

    suite = TestSuite()
    cases_raw = data.get("test_cases", []) if isinstance(data, dict) else []
    if isinstance(cases_raw, dict):
        cases_raw = [cases_raw]
    for c in cases_raw:
        if not isinstance(c, dict):
            continue
        req_id = str(c.get("req_id", "")).strip()
        if req_id and req_id not in valid_req_ids:
            # 跳过关联到不存在需求的用例
            continue
        rids = [
            str(x) for x in (c.get("source_review_ids") or [])
            if str(x) in valid_ids
        ]
        steps = c.get("steps") or []
        if isinstance(steps, str):
            steps = [steps]
        suite.test_cases.append(
            TestCase(
                case_id=str(c.get("case_id", "")) or gen_id("TC"),
                req_id=req_id,
                title=str(c.get("title", "")).strip(),
                preconditions=str(c.get("preconditions", "")).strip(),
                steps=[str(s) for s in steps],
                expected_result=str(c.get("expected_result", "")).strip(),
                source_review_ids=rids,
                type=str(c.get("type", "functional")).strip().lower(),
                confidence=str(c.get("confidence", "low")).strip().lower(),
            )
        )

    suite.notes.append(f"共生成 {len(suite.test_cases)} 条测试用例。")
    if not suite.test_cases:
        suite.notes.append("未能生成有效测试用例，请检查 PRD 是否完整。")
    return suite


def to_markdown(suite: TestSuite) -> str:
    """将测试用例套件渲染为 Markdown。"""
    lines = ["# 测试用例", ""]
    for t in suite.test_cases:
        lines.append(f"## {t.case_id} - {t.title}")
        lines.append(f"- 关联需求: {t.req_id}")
        lines.append(f"- 类型: {t.type} / 置信度: {t.confidence}")
        lines.append(f"- 前置条件: {t.preconditions}")
        lines.append("- 测试步骤:")
        for i, s in enumerate(t.steps, 1):
            lines.append(f"  {i}. {s}")
        lines.append(f"- 预期结果: {t.expected_result}")
        lines.append(f"- 来源评论: {', '.join(t.source_review_ids)}\n")
    return "\n".join(lines)
