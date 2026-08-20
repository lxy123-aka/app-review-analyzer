"""
为笔试指定 App (id=839285684) 生成完整管线输出缓存。
用于离线生成指定 App 的分析缓存，便于无网络环境下查看结果。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline import collector, cleaner, analyzer, prd_generator, testcase_generator, validator

APP_ID = "839285684"
COUNTRY = "us"
MAX_PAGES = 5
CACHE_FILE = Path("data") / "cached_result_app_839285684.json"

ANALYSIS_GOAL = "识别用户最关心的功能问题与改进诉求，输出后续版本规划"


def main() -> None:
    print(f"=== 为 App {APP_ID} 生成离线缓存 ===\n")

    print("1. 从 Apple RSS 抓取评论...")
    reviews_raw, collect_report = collector.collect_from_rss(
        APP_ID, country=COUNTRY, max_pages=MAX_PAGES
    )
    print(f"   抓取 {collect_report.raw_count} 条评论（{collect_report.pages_attempted} 页）")

    if not reviews_raw:
        print("   ⚠️ 未抓到评论，跳过后续步骤")
        return

    print("2. 数据清洗...")
    cleaned, clean_report = cleaner.clean(reviews_raw)
    print(f"   清洗后 {len(cleaned)} 条（移除 {clean_report.raw_count - clean_report.final_count} 条）")

    if not cleaned:
        print("   ⚠️ 清洗后无评论，跳过后续步骤")
        return

    print("3. AI 动态分析...")
    from models import LLMClient
    client = LLMClient()
    analysis_report = analyzer.analyze(cleaned, ANALYSIS_GOAL, llm_client=client)
    analysis_dict = analysis_report.as_dict()
    topics = analysis_dict["topics"]
    print(f"   发现 {len(topics)} 个主题")

    print("4. PRD 生成...")
    prd = prd_generator.generate(ANALYSIS_GOAL, topics, cleaned, llm_client=client)
    print(f"   生成 {len(prd.requirements)} 条需求，{len(prd.version_plan)} 个版本规划")

    print("5. 测试用例生成...")
    reqs_list = [r.as_dict() for r in prd.requirements]
    suite = testcase_generator.generate(reqs_list, cleaned, llm_client=client)
    print(f"   生成 {len(suite.test_cases)} 条测试用例")

    print("6. 可追溯性验证...")
    report = validator.validate(prd.as_dict(), suite.as_dict(), cleaned, analysis_dict)
    print(f"   通过率 {report.pass_rate:.1f}%（{report.passed}/{report.total}）")

    # 组合完整结果
    import datetime
    result = {
        "meta": {
            "generated_at": datetime.datetime.now().isoformat(),
            "source": f"Apple RSS Feed (app_id={APP_ID}, country={COUNTRY})",
            "app_id": APP_ID,
            "app_url": f"https://apps.apple.com/{COUNTRY}/app/id{APP_ID}",
            "analysis_goal": ANALYSIS_GOAL,
            "note": "此文件为离线演示用缓存结果（笔试指定 App）。"
                    "可通过此文件查看完整交付物质量。"
                    "实际使用时可在 UI 中输入任意 App Store 链接重新运行。",
        },
        "collect_report": collect_report.as_dict(),
        "clean_report": clean_report.as_dict(),
        "reviews_raw": reviews_raw[:50],
        "reviews_clean": cleaned,
        "analysis": analysis_dict,
        "prd": prd.as_dict(),
        "test_suite": suite.as_dict(),
        "validation": report.as_dict(),
    }

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    size_kb = CACHE_FILE.stat().st_size / 1024
    print(f"\n[OK] 缓存结果已保存到: {CACHE_FILE}")
    print(f"   文件大小: {size_kb:.1f} KB")


if __name__ == "__main__":
    main()
