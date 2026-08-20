# ============================================================
# pipeline/orchestrator.py
# 职责：工作流编排
#   - 串联所有步骤：抓取 → 清洗 → 分析 → PRD → 测试用例 → 验证
#   - 每步完成后通过回调通知 UI 更新进度
#   - 步骤级错误处理：某步失败不影响已完成的步骤
#   - 记录每步耗时与状态（success / failed / skipped）
# 依赖：collector / cleaner / analyzer / prd_generator /
#       testcase_generator / validator / models.LLMClient
# ============================================================
"""工作流编排模块。"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import settings
from models import LLMClient
from utils.helpers import Timer
from utils.url_parser import parse_app_store_url

from . import analyzer, cleaner, collector, prd_generator, testcase_generator, validator

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict], None]


@dataclass
class StepResult:
    """单个步骤的执行结果。"""

    name: str
    status: str  # success | failed | skipped
    elapsed: float = 0.0
    error: str = ""
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "elapsed": round(self.elapsed, 2),
            "error": self.error,
            "data": self.data,
        }


@dataclass
class PipelineResult:
    """完整流水线运行结果。"""

    steps: list[StepResult] = field(default_factory=list)
    reviews_raw: list[dict] = field(default_factory=list)
    reviews_clean: list[dict] = field(default_factory=list)
    collect_report: dict = field(default_factory=dict)
    clean_report: dict = field(default_factory=dict)
    analysis: dict = field(default_factory=dict)
    prd: dict = field(default_factory=dict)
    test_suite: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    success: bool = True

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "steps": [s.as_dict() for s in self.steps],
            "collect_report": self.collect_report,
            "clean_report": self.clean_report,
            "analysis": self.analysis,
            "prd": self.prd,
            "test_suite": self.test_suite,
            "validation": self.validation,
            "reviews_raw_count": len(self.reviews_raw),
            "reviews_clean_count": len(self.reviews_clean),
        }


class Orchestrator:
    """流水线编排器。

    用法::

        orch = Orchestrator(progress_callback=ui_update)
        result = orch.run_from_url(url, goal)
    """

    def __init__(
        self,
        *,
        llm_client: Optional[LLMClient] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> None:
        self.llm_client = llm_client  # 懒初始化：仅用到 LLM 时才创建
        self.progress = progress_callback or (lambda stage, payload: None)

    # ----------------------------------------------------------
    # 内部工具
    # ----------------------------------------------------------
    def _emit(self, stage: str, payload: dict) -> None:
        try:
            self.progress(stage, payload)
        except Exception as err:  # noqa: BLE001  UI 回调失败不影响主流程
            logger.warning("进度回调失败: %s", err)

    def _record(
        self, results: list[StepResult], name: str, fn: Callable[[], dict]
    ) -> Optional[dict]:
        """执行单步并记录结果，失败时记录但不抛出（保留已完成结果）。"""
        timer = Timer(name)
        sr = StepResult(name=name, status="running")
        try:
            with timer:
                data = fn()
            sr.status = "success"
            sr.elapsed = timer.elapsed
            sr.data = data or {}
            self._emit("step_done", sr.as_dict())
            results.append(sr)
            return data
        except Exception as err:  # noqa: BLE001
            sr.status = "failed"
            sr.elapsed = timer.elapsed
            sr.error = f"{err}"
            sr.data = {"traceback": traceback.format_exc(limit=3)}
            self._emit("step_failed", sr.as_dict())
            results.append(sr)
            return None

    # ----------------------------------------------------------
    # 公共入口
    # ----------------------------------------------------------
    def run_from_url(
        self,
        url_or_id: str,
        analysis_goal: str,
        *,
        max_pages: int | None = None,
    ) -> PipelineResult:
        """从 App Store URL 启动完整流水线。"""
        result = PipelineResult()
        # 校验链接格式
        try:
            parse_app_store_url(url_or_id)
        except Exception as err:
            result.success = False
            result.steps.append(
                StepResult(name="parse_url", status="failed", error=str(err))
            )
            return result

        # 1) 抓取
        collect = self._record(
            result.steps,
            "collect_reviews",
            lambda: self._step_collect(url_or_id, max_pages),
        )
        if collect is None:
            result.success = False
            return result
        result.reviews_raw = collect.get("reviews", [])
        result.collect_report = collect.get("report", {})
        if not result.reviews_raw:
            result.success = False
            result.steps.append(
                StepResult(name="skipped_no_data", status="skipped",
                           error="未抓取到评论，后续步骤跳过")
            )
            return result

        return self._run_from_reviews(result.reviews_raw, analysis_goal, result)

    def run_from_import(self, file_obj, filename: str, analysis_goal: str) -> PipelineResult:
        """从本地导入文件启动完整流水线。"""
        result = PipelineResult()
        collect = self._record(
            result.steps,
            "import_reviews",
            lambda: self._step_import(file_obj, filename),
        )
        if collect is None:
            result.success = False
            return result
        result.reviews_raw = collect.get("reviews", [])
        result.collect_report = collect.get("report", {})
        if not result.reviews_raw:
            result.success = False
            result.steps.append(
                StepResult(name="skipped_no_data", status="skipped",
                           error="导入数据为空，后续步骤跳过")
            )
            return result
        return self._run_from_reviews(result.reviews_raw, analysis_goal, result)

    # ----------------------------------------------------------
    # 步骤实现
    # ----------------------------------------------------------
    def _step_collect(self, url_or_id: str, max_pages: int | None) -> dict:
        def _on_page(stage: str, payload: dict) -> None:
            self._emit(stage, payload)
        reviews, report = collector.collect_from_rss(
            url_or_id, max_pages=max_pages, progress_callback=_on_page
        )
        return {"reviews": reviews, "report": report.as_dict()}

    def _step_import(self, file_obj, filename: str) -> dict:
        reviews, report = collector.import_from_file(file_obj, filename)
        return {"reviews": reviews, "report": report.as_dict()}

    def _run_from_reviews(
        self, raw_reviews: list[dict], analysis_goal: str, result: PipelineResult
    ) -> PipelineResult:
        """从原始评论继续后续步骤。"""
        # 2) 清洗
        clean = self._record(
            result.steps, "clean_reviews",
            lambda: self._step_clean(raw_reviews),
        )
        if clean is None:
            result.success = False
            return result
        result.reviews_clean = clean.get("reviews", [])
        result.clean_report = clean.get("report", {})
        if not result.reviews_clean:
            result.success = False
            result.steps.append(
                StepResult(name="skipped_no_valid", status="skipped",
                           error="清洗后无有效评论，后续步骤跳过")
            )
            return result

        # 3) 分析（核心）
        analysis = self._record(
            result.steps, "analyze_reviews",
            lambda: self._step_analyze(result.reviews_clean, analysis_goal),
        )
        if analysis is None:
            result.success = False
            return result
        result.analysis = analysis

        # 4) PRD
        prd = self._record(
            result.steps, "generate_prd",
            lambda: self._step_prd(analysis_goal, analysis, result.reviews_clean),
        )
        if prd is None:
            # PRD 失败，无后续可做
            result.success = False
            return result
        result.prd = prd

        # 5) 测试用例
        test_suite = self._record(
            result.steps, "generate_testcases",
            lambda: self._step_testcases(prd, result.reviews_clean),
        )
        if test_suite is None:
            result.success = False
            return result
        result.test_suite = test_suite

        # 6) 可追溯性验证
        validation = self._record(
            result.steps, "validate_traceability",
            lambda: self._step_validate(prd, test_suite, result.reviews_clean,
                                         result.analysis),
        )
        if validation is None:
            result.success = False
            return result
        result.validation = validation

        return result

    def _step_clean(self, raw: list[dict]) -> dict:
        reviews, report = cleaner.clean(raw)
        return {"reviews": reviews, "report": report.as_dict()}

    def _step_analyze(self, reviews: list[dict], analysis_goal: str) -> dict:
        if self.llm_client is None:
            self.llm_client = LLMClient()
        report = analyzer.analyze(
            reviews,
            analysis_goal,
            llm_client=self.llm_client,
            progress_callback=self.progress,
        )
        return report.as_dict()

    def _step_prd(self, analysis_goal: str, analysis: dict, reviews: list[dict]) -> dict:
        if self.llm_client is None:
            self.llm_client = LLMClient()
        topics = analysis.get("topics", [])
        prd = prd_generator.generate(
            analysis_goal, topics, reviews,
            llm_client=self.llm_client,
            progress_callback=self.progress,
        )
        return prd.as_dict()

    def _step_testcases(self, prd: dict, reviews: list[dict]) -> dict:
        if self.llm_client is None:
            self.llm_client = LLMClient()
        reqs = prd.get("requirements", [])
        suite = testcase_generator.generate(
            reqs, reviews,
            llm_client=self.llm_client,
            progress_callback=self.progress,
        )
        return suite.as_dict()

    def _step_validate(
        self, prd: dict, test_suite: dict,
        reviews: list[dict], analysis: dict,
    ) -> dict:
        report = validator.validate(prd, test_suite, reviews, analysis)

        # 可选：LLM 语义一致性抽查（不影响核心验证）
        try:
            if self.llm_client is None:
                self.llm_client = LLMClient()
            self._emit("semantic_check", {"status": "starting", "sample_size": 3})
            failed_items = validator.semantic_consistency_check(
                prd, reviews, self.llm_client, sample_size=3,
            )
            if failed_items:
                for item in failed_items:
                    report.items.append(item)
                    report.total += 1
                    report.failed += 1
                    report.assumed_conclusions.append(item.target)
                report.notes.append(
                    f"LLM 语义一致性抽查: {len(failed_items)} 条需求未通过语义校验。"
                )
            else:
                report.notes.append("LLM 语义一致性抽查: 抽样需求全部通过。")
            self._emit("semantic_check", {"status": "done", "failed": len(failed_items)})
        except Exception as err:
            logger.warning("语义一致性校验跳过: %s", err)
            report.notes.append("语义一致性校验跳过（LLM 不可用）。")

        return report.as_dict()
