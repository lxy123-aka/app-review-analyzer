# ============================================================
# utils/helpers.py
# 职责：通用工具函数（JSON 容错解析、语言检测、日期标准化、计时、ID 生成等）
# 依赖：标准库；可选 json-repair 用于容错解析 LLM 输出
# ============================================================
"""通用辅助函数集合。

本模块刻意保持无外部重型依赖，仅使用标准库与轻量第三方库，
方便在不同环境下复用。
"""

from __future__ import annotations

import json
import re
import string
import time
import uuid
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# JSON 容错解析
# ---------------------------------------------------------------------------
def safe_json_loads(text: str | None) -> Any:
    """容错解析 LLM 返回的 JSON 文本。

    LLM 经常在 JSON 外面裹一层 ```json ... ``` 代码块，或带额外说明文字。
    本函数按优先级尝试：
      1. 抽取 ```json ... ``` 代码块
      2. 抽取首个 { 到末尾 } 或 [ 到 ] 的片段
      3. 使用 json-repair 修复（若安装）
      4. 上述都失败时抛出原异常
    """
    if not text:
        raise ValueError("待解析的 JSON 文本为空")

    raw = text.strip()

    # 1) 抽取 ```json ... ``` 代码块
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
    if fence_match:
        raw = fence_match.group(1).strip()

    # 2) 尝试直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 3) 截取首个 { 到末尾 } 或首个 [ 到末尾 ]
    candidate = _extract_balanced(raw)
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 4) 使用 json-repair（容错）
    try:
        from json_repair import repair_json  # type: ignore

        repaired = repair_json(raw, return_objects=True)
        if repaired is not None:
            return repaired
    except Exception:  # noqa: BLE001
        pass

    # 5) 全部失败
    raise ValueError(f"无法解析为 JSON，原始文本片段: {raw[:200]}...")


def _extract_balanced(text: str) -> str | None:
    """从文本中截取第一个完整的 JSON 对象/数组（按括号配对）。"""
    start_obj = text.find("{")
    start_arr = text.find("[")
    if start_obj == -1 and start_arr == -1:
        return None

    # 选择更靠前的起始括号
    starts = [s for s in (start_obj, start_arr) if s != -1]
    start = min(starts)
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# ---------------------------------------------------------------------------
# 语言检测（轻量启发式，无需额外依赖）
# ---------------------------------------------------------------------------
# 常见英文单词与拉丁字母范围
_ASCII_LETTERS = set(string.ascii_letters)
# 部分常见英文停用词，出现则判定为英文
_EN_HINT_WORDS = {
    "the", "and", "is", "it", "this", "that", "with", "for", "you", "your",
    "have", "has", "but", "not", "are", "was", "were", "been", "good", "bad",
    "love", "great", "app", "update", "crash", "fix", "please", "would",
    "could", "should", "very", "much", "use", "using", "used", "time",
}


def detect_language(text: str) -> str:
    """轻量语言检测，返回 'en' 或 'other'。

    策略：
      - 含较多拉丁字母且英文停用词命中数 >= 2 → en
      - 否则若明显为 CJK 等非拉丁字符 → other
      - 信息不足时保守判定为 other
    """
    if not text:
        return "other"
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "other"
    ascii_letters = [c for c in letters if c in _ASCII_LETTERS]
    ascii_ratio = len(ascii_letters) / len(letters)
    if ascii_ratio < 0.5:
        return "other"

    tokens = re.findall(r"[a-zA-Z]+", text.lower())
    hit = sum(1 for t in tokens if t in _EN_HINT_WORDS)
    return "en" if hit >= 2 else ("en" if ascii_ratio > 0.9 else "other")


# ---------------------------------------------------------------------------
# 日期标准化
# ---------------------------------------------------------------------------
_ISO_PATTERN = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z$"
)


def normalize_date(date_str: str | None) -> str | None:
    """将多种日期格式标准化为 ISO8601 (YYYY-MM-DD) 字符串。

    支持输入：'2024-01-15T08:30:00Z'、'2024-01-15 08:30:00'、
             epoch 秒/毫秒（数字或字符串）。
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s:
        return None

    # 数字 → epoch
    if s.isdigit():
        ts = int(s)
        # 毫秒兼容（13 位）
        if len(s) >= 13:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return None

    m = _ISO_PATTERN.match(s)
    if m:
        try:
            dt = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(m.group(6)),
                tzinfo=timezone.utc,
            )
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    # 退化为前 10 位日期
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


# ---------------------------------------------------------------------------
# 计时上下文管理器
# ---------------------------------------------------------------------------
class Timer:
    """简易计时器，作为上下文管理器使用。

    用法::

        with Timer("抓取评论") as t:
            ...
        print(t.elapsed)
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.start: float = 0.0
        self.end: float = 0.0
        self.elapsed: float = 0.0

    def __enter__(self) -> "Timer":
        self.start = time.time()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.end = time.time()
        self.elapsed = self.end - self.start


# ---------------------------------------------------------------------------
# 唯一 ID 生成
# ---------------------------------------------------------------------------
def gen_id(prefix: str = "ID") -> str:
    """生成带前缀的短唯一 ID（用于需求/测试用例标识）。"""
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def generate_review_id(app_id: str = "", author: str = "",
                       content: str = "", updated: str = "") -> str:
    """生成评论唯一 ID。

    当原始数据缺少 review_id 时，基于 (app_id, author, content, updated)
    的哈希生成稳定的伪 ID，保证同一条内容重复导入时得到相同 ID。
    """
    import hashlib
    raw = f"{app_id}|{author}|{content}|{updated}".encode("utf-8")
    digest = hashlib.sha1(raw).hexdigest()[:12].upper()
    return f"RV-{digest}"


# ---------------------------------------------------------------------------
# 别名（兼容不同命名习惯）
# ---------------------------------------------------------------------------
def safe_json_parse(text: str | None) -> Any:
    """safe_json_loads 的别名（安全解析 LLM 返回的 JSON）。"""
    return safe_json_loads(text)


# ---------------------------------------------------------------------------
# 文本清洗小工具
# ---------------------------------------------------------------------------
def strip_noise(text: str) -> str:
    """去除首尾空白与多余空白，但保留句中结构。"""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def is_meaningful(text: str, min_len: int = 5) -> bool:
    """判断评论是否有实际内容（非空、非纯符号、非过短）。"""
    if not text:
        return False
    cleaned = strip_noise(text)
    if len(cleaned) < min_len:
        return False
    # 去除标点后是否还有字母数字
    alnum = [c for c in cleaned if c.isalnum()]
    return len(alnum) >= min_len
