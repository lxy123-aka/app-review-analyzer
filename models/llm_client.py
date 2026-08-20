# ============================================================
# models/llm_client.py
# 职责：封装 LLM API 调用，提供统一接口
#   - 兼容 OpenAI SDK 调用 DeepSeek 等服务
#   - 实现重试（指数退避）与超时
#   - 统一错误处理（不静默失败，抛出 LLMCallError）
#   - 记录 token 消耗
#   - 提供 chat() 与 chat_json() 两种调用方式
# 依赖：openai (>=1.12)、config.settings、utils.helpers.safe_json_loads
# ============================================================
"""LLM 调用客户端封装。

使用方式::

    from models import LLMClient
    client = LLMClient()           # 自动读取 config.settings
    reply = client.chat(messages=[...])         # 返回纯文本
    data  = client.chat_json(messages=[...])    # 返回解析后的 dict/list
    print(client.total_usage)                   # 累计 token 用量
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from config import settings
from utils.helpers import safe_json_loads

logger = logging.getLogger(__name__)

# 延迟导入 OpenAI：避免在未安装/旧版本时整个模块无法 import
# 正确版本：openai >= 1.12.0（使用 from openai import OpenAI）
# 旧版本 openai 会在实际调用 LLMClient() 时给出明确错误提示
_OpenAI_CLS = None
_import_err = None

try:
    from openai import OpenAI as _OpenAI_CLS  # type: ignore
except Exception as _e:  # noqa: BLE001
    _import_err = _e
    _OpenAI_CLS = None


class LLMCallError(RuntimeError):
    """LLM 调用失败统一异常，附带可读的错误原因。"""


@dataclass
class TokenUsage:
    """累计 token 用量统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0

    def add(self, usage: Any) -> None:
        """将一次调用的 usage 累加进统计。"""
        if not usage:
            return
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        self.total_tokens += getattr(usage, "total_tokens", 0) or 0
        self.call_count += 1

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "call_count": self.call_count,
        }


@dataclass
class LLMResponse:
    """单次调用结果，便于上层透传。"""

    content: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: Any = None


class LLMClient:
    """LLM 调用统一客户端。

    设计要点：
      * 构造时即校验 API Key，缺失直接报错而非运行期崩溃；
      * 重试采用指数退避（1s, 2s, 4s），仅重试可恢复错误；
      * 失败抛 ``LLMCallError``，明确告知调用方；
      * Prompt 应在调用方构造，本类只负责传输与解析。
    """

    def __init__(self) -> None:
        missing = settings.validate_llm()
        if missing:
            raise LLMCallError(
                "LLM 配置不完整，缺少环境变量: "
                + ", ".join(missing)
                + "。请在 .env 中配置（参考 .env.example）。"
            )
        # 检查 OpenAI SDK 是否已正确安装（>=1.12.0 版本）
        if _OpenAI_CLS is None:
            raise LLMCallError(
                "openai SDK 导入失败，请确认已安装正确版本（要求 >= 1.12.0）。\n"
                f"  建议执行: pip install 'openai>=1.12.0,<2.0.0'\n"
                f"  原始错误: {_import_err}"
            )
        # OpenAI SDK 兼容 DeepSeek：只需把 base_url 指向 DeepSeek
        self._client = _OpenAI_CLS(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout,
        )
        self.total_usage = TokenUsage()

    # ----------------------------------------------------------
    # 基础调用
    # ----------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> LLMResponse:
        """发送对话请求，返回 LLMResponse。

        Args:
            messages: OpenAI 风格消息列表
            temperature: 覆盖默认温度
            max_tokens: 覆盖默认 max_tokens
            response_format: 如 {"type": "json_object"} 启用 JSON 模式（provider 支持时）

        Raises:
            LLMCallError: 重试耗尽或不可恢复错误
        """
        last_err: Exception | None = None
        max_retries = max(1, settings.llm_max_retries)

        for attempt in range(1, max_retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": settings.llm_model,
                    "messages": messages,
                    "temperature": (
                        settings.llm_temperature if temperature is None else temperature
                    ),
                    "max_tokens": (
                        settings.llm_max_tokens if max_tokens is None else max_tokens
                    ),
                }
                if response_format is not None:
                    kwargs["response_format"] = response_format

                resp = self._client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or ""

                # 累计 token
                usage = TokenUsage()
                usage.add(resp.usage)
                self.total_usage.add(resp.usage)

                return LLMResponse(content=content, usage=usage, raw=resp)

            except Exception as err:  # noqa: BLE001
                last_err = err
                logger.warning(
                    "LLM 调用失败 (尝试 %d/%d): %s", attempt, max_retries, err
                )
                # 非最后一次重试前进行指数退避
                if attempt < max_retries:
                    backoff = 2 ** (attempt - 1)  # 1, 2, 4...
                    time.sleep(backoff)
                continue

        # 全部失败
        raise LLMCallError(
            f"LLM 调用 {max_retries} 次后仍失败: {last_err}"
        ) from last_err

    # ----------------------------------------------------------
    # JSON 调用（自动解析 + 容错）
    # ----------------------------------------------------------
    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """发送对话请求并解析为 JSON 对象。

        优先尝试 JSON 模式（部分 provider 不支持会自动降级）；
        解析失败时调用 ``utils.helpers.safe_json_loads`` 容错修复；
        仍失败则抛出 ``LLMCallError``。
        """
        # 先尝试 JSON 模式（OpenAI/DeepSeek 多支持）
        try:
            resp = self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except LLMCallError:
            # provider 不支持 JSON 模式时降级为普通文本
            resp = self.chat(messages, temperature=temperature, max_tokens=max_tokens)

        try:
            return safe_json_loads(resp.content)
        except Exception as err:  # noqa: BLE001
            raise LLMCallError(
                f"LLM 返回内容无法解析为 JSON: {err}\n原始内容片段: "
                f"{resp.content[:300]}..."
            ) from err

    # ----------------------------------------------------------
    # 便捷构造 system+user
    # ----------------------------------------------------------
    @staticmethod
    def build_messages(system: str, user: str) -> list[dict[str, str]]:
        """便捷构造 OpenAI 风格消息列表。"""
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
