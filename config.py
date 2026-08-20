# ============================================================
# config.py
# 职责：集中管理项目配置，从 .env 文件读取环境变量
#       提供默认值兜底，避免未配置时崩溃
# 依赖：python-dotenv（加载 .env 文件）
# ============================================================
"""配置管理模块。

所有敏感信息（API Key、Base URL）必须通过本模块读取，绝不硬编码。
其他模块通过 `from config import settings` 获取配置实例。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# 加载项目根目录下的 .env 文件（若存在）
# 优先级: 真实环境变量 > .env 文件 > 代码默认值
load_dotenv()


def _get_bool(key: str, default: bool = False) -> bool:
    """从环境变量读取布尔值（接受 true/1/yes 等大小写形式）。"""
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def _get_int(key: str, default: int) -> int:
    """从环境变量读取整数，解析失败时返回默认值。"""
    raw = os.getenv(key, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    """从环境变量读取浮点数，解析失败时返回默认值。"""
    raw = os.getenv(key, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    """全局配置容器。

    所有字段在模块加载时一次性求值，运行期只读。
    """

    # ---------- LLM ----------
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096
    llm_timeout: int = 60
    llm_max_retries: int = 3

    # ---------- 数据抓取 ----------
    max_review_pages: int = 10
    request_interval: float = 1.5
    http_timeout: int = 15
    default_country: str = "us"

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构造配置实例。

        兼容多种命名习惯：
          - LLM_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY
          - LLM_BASE_URL / BASE_URL / DEEPSEEK_BASE_URL / OPENAI_BASE_URL
          - LLM_MODEL / MODEL / DEEPSEEK_MODEL / OPENAI_MODEL
        """
        # API Key：多别名兼容，优先级 LLM_* > DEEPSEEK_* > OPENAI_*
        api_key = (
            os.getenv("LLM_API_KEY", "").strip()
            or os.getenv("DEEPSEEK_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        # Base URL：多别名兼容
        base_url = (
            os.getenv("LLM_BASE_URL", "").strip()
            or os.getenv("BASE_URL", "").strip()
            or os.getenv("DEEPSEEK_BASE_URL", "").strip()
            or os.getenv("OPENAI_BASE_URL", "").strip()
            or "https://api.deepseek.com"
        )
        # Model：多别名兼容
        model = (
            os.getenv("LLM_MODEL", "").strip()
            or os.getenv("MODEL", "").strip()
            or os.getenv("DEEPSEEK_MODEL", "").strip()
            or os.getenv("OPENAI_MODEL", "").strip()
            or "deepseek-chat"
        )
        return cls(
            llm_api_key=api_key,
            llm_base_url=base_url,
            llm_model=model,
            llm_temperature=_get_float("LLM_TEMPERATURE", 0.3),
            llm_max_tokens=_get_int("LLM_MAX_TOKENS", 4096),
            llm_timeout=_get_int("LLM_TIMEOUT", 60),
            llm_max_retries=_get_int("LLM_MAX_RETRIES", 3),
            max_review_pages=_get_int("MAX_REVIEW_PAGES", 10),
            request_interval=_get_float("REQUEST_INTERVAL", 1.5),
            http_timeout=_get_int("HTTP_TIMEOUT", 15),
            default_country=os.getenv("DEFAULT_COUNTRY", "us").strip() or "us",
        )

    def validate_llm(self) -> list[str]:
        """校验 LLM 必填项，返回缺失项列表（空列表表示通过）。"""
        missing: list[str] = []
        if not self.llm_api_key:
            missing.append("LLM_API_KEY")
        if not self.llm_base_url:
            missing.append("LLM_BASE_URL")
        if not self.llm_model:
            missing.append("LLM_MODEL")
        return missing


# 模块级单例，其他模块直接 import 使用
settings = Settings.from_env()
