# ============================================================
# app.py
# 职责：Streamlit 主入口，构建 Web UI
#   - 顶部输入区：App Store 链接 + 分析目标 + 开始按钮
#   - 提供"导入本地数据"按钮（上传 JSON/CSV）
#   - 使用 st.status 展示每步执行进度（Streamlit 1.32+）
#   - 每步完成后展示中间结果（可折叠）
#   - 最终结果分区展示：原始评论 / 清洗报告 / 分析结果 /
#     PRD / 测试用例 / 验证报告
#   - 分析结果中每条结论旁显示来源 review_id，可点击查看原文
# 依赖：streamlit、pandas、pipeline 全部模块、config
# 运行：streamlit run app.py
# ============================================================
"""App Store 评论分析与版本规划工具 - Streamlit 前端。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# 确保项目根目录在 sys.path 中（支持 `streamlit run app.py` 直接运行）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings  # noqa: E402
from pipeline import (  # noqa: E402
    analyzer, cleaner, collector, orchestrator,
    prd_generator, testcase_generator, validator,
)

# 缓存样本数据路径
SAMPLE_FILE = Path(__file__).parent / "data" / "sample_reviews.json"
# 离线完整管线缓存（笔试指定 App 839285684）
CACHED_RESULT_FILE = Path(__file__).parent / "data" / "cached_result_app_839285684.json"


# ============================================================
# 页面配置
# ============================================================
st.set_page_config(
    page_title="App Store 评论分析与版本规划工具",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.title("🛒 App Store 评论分析与版本规划工具")
st.caption(
    "评论抓取 → 清洗 → AI 动态分析 → 证据评估 → PRD 生成 → "
    "测试用例 → 可追溯性验证"
)

# 显示当前模型与配置摘要
with st.expander("⚙️ 当前配置（来自 .env）", expanded=False):
    cols = st.columns(4)
    cols[0].metric("LLM 模型", settings.llm_model or "(未配置)")
    cols[1].metric("Base URL", settings.llm_base_url or "(未配置)")
    cols[2].metric("最大抓取页数", settings.max_review_pages)
    cols[3].metric("请求间隔(s)", settings.request_interval)
    api_key_masked = (settings.llm_api_key[:6] + "***") if settings.llm_api_key else "(未配置)"
    st.caption(f"API Key: `{api_key_masked}`")
    missing = settings.validate_llm()
    if missing:
        st.warning(
            f"⚠️ LLM 配置缺失：{', '.join(missing)}。请复制 `.env.example` 为 `.env` 并填入。"
        )
        if CACHED_RESULT_FILE.exists():
            st.info(
                "💡 当前未配置 API Key，无法进行实时 LLM 分析。"
                "可点击下方「📊 加载离线缓存结果」按钮查看笔试指定 App (839285684) 的完整分析示例。"
            )


# ============================================================
# 输入区
# ============================================================
st.subheader("1. 输入与分析目标")

with st.container(border=True):
    col1, col2 = st.columns([3, 2])
    with col1:
        app_url = st.text_input(
            "App Store 链接（美国区）",
            value="",
            placeholder="https://apps.apple.com/us/app/workout-for-women-home-gym/id839285684",
            help="也支持直接输入纯数字 App ID，例如 839285684",
        )
    with col2:
        max_pages = st.number_input(
            "抓取页数上限",
            min_value=1,
            max_value=20,
            value=settings.max_review_pages,
            step=1,
            help="每页 ≤50 条，page=1 仅返回应用信息，评论从 page=2 开始",
        )

    analysis_goal = st.text_area(
        "分析目标",
        value="识别用户最关心的功能问题与改进诉求，输出后续版本规划",
        height=80,
        help="描述你希望从评论中得到什么结论，会影响 LLM 的分析侧重",
    )

    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    with c1:
        start_btn = st.button("🚀 开始分析", type="primary", use_container_width=True)
    with c2:
        sample_btn = st.button("📦 加载样本数据", use_container_width=True,
                               help="使用内置的缓存样本评论演示")
    with c3:
        cache_btn = st.button("📊 加载离线缓存结果", use_container_width=True,
                               help="加载笔试指定 App (839285684) 的完整分析缓存，无需 API Key")
    with c4:
        uploaded = st.file_uploader(
            "或上传本地 JSON/CSV 文件",
            type=["json", "csv"],
            label_visibility="collapsed",
            help="支持 JSON（list[dict] 或 RSS 原始结构）与 CSV（含 review_id,content 列）",
        )


# ============================================================
# 状态初始化
# ============================================================
def _reset_state() -> None:
    st.session_state["pipeline_result"] = None
    st.session_state["selected_review"] = None
    st.session_state["is_cached"] = False


if "pipeline_result" not in st.session_state:
    _reset_state()


# ============================================================
# 进度回调 → 写入 st.status
# ============================================================
class UIProgress:
    """将 orchestrator 的进度事件转发到 Streamlit st.status 容器。"""

    def __init__(self, status_container) -> None:
        self.status = status_container
        self.lines: list[str] = []

    def __call__(self, stage: str, payload: dict) -> None:
        # 不同阶段不同展示
        if stage == "fetch_page":
            line = f"🌐 请求 page={payload.get('page')} ..."
        elif stage == "page_done":
            line = (
                f"✅ page={payload.get('page')} 获取 "
                f"{payload.get('page_count', 0)} 条，累计 {payload.get('total', 0)} 条"
            )
        elif stage == "analyze_batch":
            line = (
                f"🤖 分析批次 {payload.get('batch')}/{payload.get('total_batches')} "
                f"(本批 {payload.get('size')} 条)"
            )
        elif stage == "prd_llm":
            line = f"📝 调用 LLM 生成 PRD（模型 {payload.get('model')}）"
        elif stage == "testcase_llm":
            line = (
                f"🧪 调用 LLM 生成测试用例（{payload.get('req_count')} 条需求）"
            )
        elif stage == "step_done":
            line = (
                f"☑️ 步骤完成: {payload.get('name')} "
                f"({payload.get('elapsed')}s)"
            )
        elif stage == "step_failed":
            line = f"❌ 步骤失败: {payload.get('name')} - {payload.get('error','')[:80]}"
        else:
            line = f"{stage}: {payload}"
        self.lines.append(line)
        self.status.update(label="分析进行中...", state="running",
                          expanded=True)
        self.status.write("\n".join(self.lines[-8:]))


# ============================================================
# 结果渲染
# ============================================================
def render_reviews_section(reviews: list[dict], title: str, *,
                            max_rows: int = 50) -> None:
    """渲染评论表格（可分页/折叠）。"""
    if not reviews:
        st.info("无评论数据。")
        return
    df = pd.DataFrame(reviews)
    keep_cols = [c for c in ["review_id", "rating", "version", "language",
                             "updated", "author", "title", "content"]
                 if c in df.columns]
    df = df[keep_cols]
    st.caption(f"共 {len(reviews)} 条，此处最多展示 {max_rows} 条")
    st.dataframe(df.head(max_rows), use_container_width=True, height=320)


def render_clean_report(report: dict) -> None:
    if not report:
        return
    cols = st.columns(5)
    cols[0].metric("原始数量", report.get("raw_count", 0))
    cols[1].metric("去重数量", report.get("duplicate_count", 0))
    cols[2].metric("空内容", report.get("empty_count", 0))
    cols[3].metric("过短/符号", report.get("too_short_count", 0))
    cols[4].metric("最终数量", report.get("final_count", 0))
    if report.get("language_distribution"):
        st.caption("语言分布: " + ", ".join(
            f"{k}: {v}" for k, v in report["language_distribution"].items()
        ))
    if report.get("rating_distribution"):
        st.caption("评分分布: " + ", ".join(
            f"{k}★: {v}" for k, v in sorted(report["rating_distribution"].items())
        ))
    if report.get("notes"):
        for n in report["notes"]:
            st.info(n)


def render_analysis_section(analysis: dict, reviews: list[dict]) -> None:
    """渲染分析结果，每条主题旁显示来源 review_id 可点击查看。

    UI 标注：
      - 🤖 模型归纳：主题名称、描述、矛盾反馈、置信度（LLM 生成）
      - 📊 确定性统计：样本数、review_ids、情感分布（从数据直接算出）
    """
    topics = analysis.get("topics", [])
    if not topics:
        st.warning("未发现主题。")
        return
    reviews_by_id = {str(r.get("review_id")): r for r in reviews}

    st.caption(f"共发现 {len(topics)} 个主题，覆盖 "
               f"{analysis.get('review_count', 0)} 条评论")
    st.caption("图例: 🤖 模型生成结论 | 📊 确定性统计（从数据直接算出）")

    for i, t in enumerate(topics, 1):
        sentiment = t.get("sentiment", "")
        emoji = {"positive": "👍", "negative": "👎", "neutral": "😐"}.get(
            sentiment, "•"
        )
        conf = t.get("confidence", "")
        conf_color = {"high": "green", "medium": "orange", "low": "red"}.get(
            conf, "gray"
        )
        with st.container(border=True):
            head_cols = st.columns([6, 2, 2, 2])
            # 主题标题旁标注"模型归纳"徽章
            head_cols[0].markdown(
                f"**{emoji} 主题 {i}: {t.get('topic','')}** "
                f"`🤖 模型归纳`"
            )
            # 情感标注为确定性统计（从评论评分/关键词归类得出）
            head_cols[1].markdown(f"📊 情感: `{sentiment}`")
            # 置信度标注为模型生成
            head_cols[2].markdown(
                f"🤖 置信度: :{conf_color}[`{conf}`]"
            )
            # 样本数标注为确定性统计
            head_cols[3].markdown(f"📊 样本数: `{t.get('sample_count',0)}`")
            # 描述标注为模型生成
            st.markdown(f"`🤖 模型生成` {t.get('description', '')}")
            if t.get("conflicting_evidence"):
                st.warning("`🤖 模型归纳` 存在矛盾反馈：")
                for ce in t["conflicting_evidence"]:
                    st.markdown(f"- {ce}")
            ev = analysis.get("evidence_evaluation", {}).get(t.get("topic",""), "")
            if ev:
                st.caption(f"📊 证据评估: {ev}")

            rids = t.get("review_ids", [])
            if rids:
                st.markdown("📊 **来源评论**（点击按钮查看原文）：")
                rcols = st.columns(min(len(rids), 5))
                for j, rid in enumerate(rids[:10]):
                    with rcols[j % 5]:
                        if st.button(rid, key=f"t{i}_r{j}", help="查看评论原文"):
                            st.session_state["selected_review"] = rid
                sel = st.session_state.get("selected_review")
                if sel and sel in reviews_by_id:
                    rv = reviews_by_id[sel]
                    with st.container(border=True):
                        st.caption(f"评论 {sel} · 评分: {rv.get('rating')} · "
                                   f"版本: {rv.get('version')} · 作者: {rv.get('author')}")
                        st.write(rv.get("content", ""))


def render_prd_section(prd: dict) -> None:
    if not prd:
        st.info("无 PRD 数据。")
        return
    st.markdown(f"### 问题概述\n{prd.get('problem_summary','')}")
    reqs = prd.get("requirements", [])
    if reqs:
        df = pd.DataFrame(reqs)
        keep = [c for c in ["req_id", "title", "priority", "source_topic",
                            "confidence", "acceptance_criteria",
                            "source_review_ids"]
                if c in df.columns]
        st.dataframe(df[keep], use_container_width=True, height=320)
    vps = prd.get("version_plan", [])
    if vps:
        st.markdown("#### 版本规划")
        for v in vps:
            with st.container(border=True):
                st.markdown(f"**{v.get('version','')}** - {v.get('goal','')}")
                st.caption(f"范围: {v.get('scope','')}")
                st.caption(f"包含需求: {', '.join(v.get('req_ids',[]))}")
                st.caption(f"拆分理由: {v.get('rationale','')}")
    # 完整 Markdown 渲染
    with st.expander("📄 PRD Markdown"):
        st.code(prd_generator.to_markdown(
            _prd_dict_to_obj(prd)), language="markdown")


def _prd_dict_to_obj(prd: dict):
    """将 dict 转回 PRD 对象以便调用 to_markdown。"""
    from pipeline.prd_generator import PRD, Requirement, VersionPlan
    return PRD(
        problem_summary=prd.get("problem_summary", ""),
        requirements=[Requirement(
            req_id=r.get("req_id",""), title=r.get("title",""),
            description=r.get("description",""), priority=r.get("priority","P2"),
            source_topic=r.get("source_topic",""),
            source_review_ids=r.get("source_review_ids",[]),
            acceptance_criteria=r.get("acceptance_criteria",""),
            confidence=r.get("confidence","low"),
        ) for r in prd.get("requirements", [])],
        version_plan=[VersionPlan(
            version=v.get("version",""), goal=v.get("goal",""),
            scope=v.get("scope",""), req_ids=v.get("req_ids",[]),
            rationale=v.get("rationale",""),
        ) for v in prd.get("version_plan", [])],
    )


def render_test_suite_section(test_suite: dict) -> None:
    if not test_suite:
        st.info("无测试用例数据。")
        return
    cases = test_suite.get("test_cases", [])
    if not cases:
        st.warning("未生成测试用例。")
        return
    st.caption(f"共 {len(cases)} 条测试用例")
    with st.expander("🧪 测试用例 Markdown"):
        from pipeline.testcase_generator import TestSuite, TestCase
        obj = TestSuite(test_cases=[
            TestCase(
                case_id=c.get("case_id",""), req_id=c.get("req_id",""),
                title=c.get("title",""), preconditions=c.get("preconditions",""),
                steps=c.get("steps",[]),
                expected_result=c.get("expected_result",""),
                source_review_ids=c.get("source_review_ids",[]),
                type=c.get("type","functional"),
                confidence=c.get("confidence","low"),
            ) for c in cases
        ])
        st.code(testcase_generator.to_markdown(obj), language="markdown")
    # 表格快速预览
    df = pd.DataFrame([{
        "case_id": c.get("case_id"),
        "req_id": c.get("req_id"),
        "title": c.get("title"),
        "type": c.get("type"),
        "confidence": c.get("confidence"),
        "expected": c.get("expected_result"),
        "source_reviews": ", ".join(c.get("source_review_ids",[])),
    } for c in cases])
    st.dataframe(df, use_container_width=True, height=320)


def render_validation_section(validation: dict) -> None:
    if not validation:
        st.info("无验证报告。")
        return
    cols = st.columns(4)
    cols[0].metric("总项", validation.get("total", 0))
    cols[1].metric("通过", validation.get("passed", 0))
    cols[2].metric("未通过", validation.get("failed", 0))
    cols[3].metric("通过率%", validation.get("pass_rate", 0))

    # 无证据支撑的结论用红色突出显示
    if validation.get("assumed_conclusions"):
        st.error(
            "🚫 **模型生成但无证据支撑的结论（已标记为假设）**\n\n"
            "⚠️ 以下需求/用例的 source_review_ids 在数据中不存在或为空，"
            "标记为假设，建议移除或补充证据：\n"
            + ", ".join(validation["assumed_conclusions"])
        )

    items = validation.get("items", [])
    if items:
        # 语义校验未通过的项单独列出
        semantic_fails = [
            it for it in items
            if it.get("target_type") == "requirement_semantic"
        ]
        if semantic_fails:
            st.error("🤖 **语义校验未通过**（LLM 判断需求无法从评论中合理推导）：")
            for it in semantic_fails:
                st.markdown(
                    f"- **{it.get('target')}**: {it.get('reason')}"
                )

        # 全部验证项表格
        df = pd.DataFrame([{
            "对象": it.get("target"),
            "类型": it.get("target_type"),
            "是否通过": "通过" if it.get("passed") else "未通过",
            "原因": it.get("reason"),
        } for it in items])
        st.dataframe(df, use_container_width=True, height=320)
    if validation.get("notes"):
        for n in validation["notes"]:
            st.caption(n)


def render_steps_section(steps: list[dict]) -> None:
    df = pd.DataFrame([{
        "步骤": s.get("name"),
        "状态": s.get("status"),
        "耗时(s)": s.get("elapsed"),
        "错误": (s.get("error") or "")[:120],
    } for s in steps])
    st.dataframe(df, use_container_width=True, height=240)


# ============================================================
# 主流程
# ============================================================
def run_pipeline_from_url(url: str, goal: str, pages: int) -> None:
    orch = orchestrator.Orchestrator(
        progress_callback=None  # 将在下面包装
    )
    with st.status("正在执行流水线...", expanded=True) as status:
        ui = UIProgress(status)
        orch.progress = ui
        # 重置 session
        _reset_state()
        try:
            result = orch.run_from_url(url, goal, max_pages=pages)
        except Exception as err:  # noqa: BLE001
            status.update(label="流水线异常", state="error")
            st.error(f"执行失败: {err}")
            return
        if result.success:
            status.update(label="分析完成", state="complete")
        else:
            failed = [s.name for s in result.steps if s.status == "failed"]
            status.update(
                label=f"分析结束（有失败步骤：{', '.join(failed) or '见步骤明细'}）",
                state="error",
            )
        st.session_state["pipeline_result"] = result


def run_pipeline_from_reviews(reviews: list[dict], goal: str,
                                source_label: str) -> None:
    """从已有评论（导入/样本）启动后续步骤。"""
    orch = orchestrator.Orchestrator()
    with st.status("正在执行流水线...", expanded=True) as status:
        ui = UIProgress(status)
        orch.progress = ui
        result = orchestrator.PipelineResult()
        # 注入抓取报告（说明数据来源）
        from pipeline.collector import AppMeta, CollectReport
        app = AppMeta(app_id="imported", name=source_label)
        rep = CollectReport(app=app, raw_count=len(reviews), data_source="local")
        rep.notes.append(f"数据来源: {source_label}")
        result.reviews_raw = reviews
        result.collect_report = rep.as_dict()
        result.steps.append(orchestrator.StepResult(
            name="load_data", status="success", elapsed=0.0,
            data={"source": source_label, "count": len(reviews)}
        ))
        ui("step_done", {"name": "load_data", "elapsed": 0.0})

        result = orch._run_from_reviews(reviews, goal, result)
        if result.success:
            status.update(label="分析完成", state="complete")
        else:
            status.update(label="分析结束（有失败步骤）", state="error")
        st.session_state["pipeline_result"] = result


def load_cached_result() -> dict | None:
    """读取离线缓存 JSON 文件并返回解析后的 dict。

    文件不存在或解析失败时返回 None，不报错不崩溃。
    返回的 dict 结构与 PipelineResult 的字段一致，可直接用于渲染。
    """
    if not CACHED_RESULT_FILE.exists():
        return None
    try:
        with open(CACHED_RESULT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data


def _build_pipeline_result_from_cache(data: dict) -> "orchestrator.PipelineResult":
    """将缓存 dict 构造为 PipelineResult 对象，供结果页渲染复用。"""
    result = orchestrator.PipelineResult()
    result.success = True

    meta = data.get("meta", {})
    result.steps.append(orchestrator.StepResult(
        name="load_cache", status="success", elapsed=0.0,
        data={"source": "cached_result_app_839285684.json",
              "app_id": meta.get("app_id", "839285684"),
              "generated_at": meta.get("generated_at", "")},
    ))
    result.steps.append(orchestrator.StepResult(
        name="collect_reviews", status="success", elapsed=0.0,
        data={"cached": True, "count": len(data.get("reviews_raw", []))},
    ))
    result.steps.append(orchestrator.StepResult(
        name="clean_reviews", status="success", elapsed=0.0,
        data={"cached": True, "count": len(data.get("reviews_clean", []))},
    ))
    result.steps.append(orchestrator.StepResult(
        name="analyze_reviews", status="success", elapsed=0.0,
        data={"cached": True, "topics": len(data.get("analysis", {}).get("topics", []))},
    ))
    result.steps.append(orchestrator.StepResult(
        name="generate_prd", status="success", elapsed=0.0,
        data={"cached": True, "reqs": len(data.get("prd", {}).get("requirements", []))},
    ))
    result.steps.append(orchestrator.StepResult(
        name="generate_test_cases", status="success", elapsed=0.0,
        data={"cached": True, "cases": len(data.get("test_suite", {}).get("test_cases", []))},
    ))
    result.steps.append(orchestrator.StepResult(
        name="validate_traceability", status="success", elapsed=0.0,
        data={"cached": True, "pass_rate": data.get("validation", {}).get("pass_rate", 0)},
    ))

    # 填充各分区数据（结构与 PipelineResult 字段一致）
    result.reviews_raw = data.get("reviews_raw", [])
    result.reviews_clean = data.get("reviews_clean", [])
    result.collect_report = data.get("collect_report", {})
    result.clean_report = data.get("clean_report", {})
    result.analysis = data.get("analysis", {})
    result.prd = data.get("prd", {})
    result.test_suite = data.get("test_suite", {})
    result.validation = data.get("validation", {})
    return result


# ============================================================
# 触发
# ============================================================
if start_btn and app_url:
    st.session_state["is_cached"] = False
    run_pipeline_from_url(app_url, analysis_goal, max_pages)

if sample_btn:
    st.session_state["is_cached"] = False
    if not SAMPLE_FILE.exists():
        st.error(f"样本文件不存在: {SAMPLE_FILE}")
    else:
        try:
            with open(SAMPLE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 支持两种结构：list[已标准化dict] 或 RSS 原始 {"feed":{"entry":[...]}}
            if isinstance(data, list):
                raw_entries = data
            elif isinstance(data, dict) and "feed" in data:
                raw_entries = data.get("feed", {}).get("entry", [])
                if isinstance(raw_entries, dict):
                    raw_entries = [raw_entries]
            else:
                raw_entries = []
            # 判断每条是否为 RSS 原始结构（含 im:rating 键），是则走标准化
            from pipeline.collector import _normalize_raw_review
            reviews = []
            for r in raw_entries:
                if not isinstance(r, dict):
                    continue
                if "im:rating" in r or ("id" in r and "label" in r.get("id", {})):
                    n = _normalize_raw_review(r, "us")
                    if n:
                        reviews.append(n)
                elif r.get("review_id") and r.get("content"):
                    reviews.append(r)  # 已标准化
            st.toast(f"已加载样本数据 {len(reviews)} 条")
            run_pipeline_from_reviews(reviews, analysis_goal,
                                       source_label="内置样本数据 sample_reviews.json")
        except Exception as err:  # noqa: BLE001
            st.error(f"样本加载失败: {err}")

if uploaded is not None:
    st.session_state["is_cached"] = False
    try:
        # 直接走 collector 的导入路径，再走后续步骤
        from io import BytesIO
        reviews, report = collector.import_from_file(
            BytesIO(uploaded.getvalue()), uploaded.name
        )
        st.toast(f"已导入 {len(reviews)} 条评论（来源 {uploaded.name}）")
        run_pipeline_from_reviews(reviews, analysis_goal,
                                   source_label=f"上传文件 {uploaded.name}")
    except Exception as err:  # noqa: BLE001
        st.error(f"文件导入失败: {err}")

if cache_btn:
    cache_data = load_cached_result()
    if cache_data is not None:
        result_obj = _build_pipeline_result_from_cache(cache_data)
        st.session_state["is_cached"] = True
        st.session_state["pipeline_result"] = result_obj
        meta = cache_data.get("meta", {})
        st.toast(f"已加载离线缓存结果（App {meta.get('app_id', '839285684')}）")
    else:
        st.error("缓存文件不存在或加载失败")


# ============================================================
# 自动加载离线缓存（LLM 配置缺失时）
# ============================================================
# 仅在首次加载（无 pipeline_result）且无按钮点击时自动加载
_llm_missing = bool(settings.validate_llm())
_no_result_yet = st.session_state.get("pipeline_result") is None
_no_button_clicked = not (start_btn or sample_btn or cache_btn or uploaded is not None)

if _llm_missing and _no_result_yet and _no_button_clicked:
    cache_data = load_cached_result()
    if cache_data is not None:
        result_obj = _build_pipeline_result_from_cache(cache_data)
        st.session_state["is_cached"] = True
        st.session_state["pipeline_result"] = result_obj


# ============================================================
# 结果展示
# ============================================================
result = st.session_state.get("pipeline_result")
if result:
    # 缓存模式提示（显示在结果区最顶部）
    if st.session_state.get("is_cached"):
        st.warning(
            "⚠️ **当前显示缓存示例数据（App ID: 839285684），非实时分析结果。**\n"
            "请在 .env 中配置 LLM_API_KEY 后即可处理任意 App Store 链接。"
        )
    st.divider()
    st.subheader("2. 执行步骤总览")
    render_steps_section([s.as_dict() for s in result.steps])

    st.subheader("3. 抓取报告与原始评论")
    with st.expander("抓取报告", expanded=True):
        st.json(result.collect_report or {})
    with st.expander("原始评论", expanded=False):
        render_reviews_section(result.reviews_raw, "原始评论")

    st.subheader("4. 清洗报告与清洗后评论")
    render_clean_report(result.clean_report or {})
    with st.expander("清洗后评论", expanded=True):
        render_reviews_section(result.reviews_clean, "清洗后评论")

    st.subheader("5. AI 动态分析结果")
    render_analysis_section(result.analysis or {}, result.reviews_clean)

    st.subheader("6. 产品需求文档 (PRD)")
    render_prd_section(result.prd or {})

    st.subheader("7. 测试用例")
    render_test_suite_section(result.test_suite or {})

    st.subheader("8. 可追溯性验证报告")
    render_validation_section(result.validation or {})

    st.divider()
    st.caption("💡 数据源说明：Apple RSS Feed 接口不保证返回所有历史评论，"
               "数量可能有限。结果中已透明标注数据来源与限制。")
else:
    st.info("👆 请输入 App Store 链接或上传文件后点击「开始分析」"
            "，或点击「加载样本数据」快速体验。")
