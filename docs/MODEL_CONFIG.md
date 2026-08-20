# 模型配置说明 (MODEL_CONFIG)

本文档详细说明项目中使用的 LLM 模型与 Provider、Prompt 设计思路、
参数配置、失败处理策略以及防幻觉措施，便于审查与维护。

## 1. 使用的模型与 Provider

本项目**不绑定任何特定模型**，采用 OpenAI 兼容协议，可通过 `.env` 切换。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_KEY` | (无默认) | API Key，需自行申请 |
| `LLM_BASE_URL` | `https://api.deepseek.com` | 默认指向 DeepSeek，可改为 `https://api.openai.com/v1` |
| `LLM_MODEL` | `deepseek-chat` | DeepSeek 通用对话模型；OpenAI 可选 `gpt-4o-mini` 等 |
| `LLM_TEMPERATURE` | `0.3` | 分析任务偏低温度以保证稳定 |
| `LLM_MAX_TOKENS` | `4096` | 单次输出上限 |
| `LLM_TIMEOUT` | `60` | 单次请求超时（秒） |
| `LLM_MAX_RETRIES` | `3` | 失败重试次数（指数退避 1s/2s/4s） |

### 推荐 Provider

- **DeepSeek**（默认）：`deepseek-chat` 性价比高，支持 JSON 模式，国内访问稳定。
- **OpenAI**：`gpt-4o-mini` 速度更快，质量更稳，但需海外网络。
- 任何兼容 OpenAI SDK 的服务（智谱、Moonshot、自建 vLLM 等）均可。

## 2. LLM 调用架构

所有 LLM 调用都经过统一封装：[`models/llm_client.py`](../models/llm_client.py)

- `LLMClient.chat(messages, ...)`：返回纯文本响应
- `LLMClient.chat_json(messages, ...)`：自动尝试 JSON 模式 → 容错解析 → 返回 dict/list
- 失败统一抛出 `LLMCallError`，附带可读原因，**绝不静默失败**
- `LLMClient.total_usage`：累计 prompt/completion/total token 用量，便于成本审计

## 3. 主要 Prompt 设计思路

所有 Prompt 全部写在代码中，便于审查。涉及 LLM 的模块：

| 模块 | 位置 | 目标 |
|---|---|---|
| 动态分析 | [`pipeline/analyzer.py`](../pipeline/analyzer.py) | 主题发现、归类、情感、证据评估 |
| PRD 生成 | [`pipeline/prd_generator.py`](../pipeline/prd_generator.py) | 需求列表、优先级、版本规划 |
| 测试用例 | [`pipeline/testcase_generator.py`](../pipeline/testcase_generator.py) | 用例步骤、预期结果、关联评论 |

### 3.1 分析模块 Prompt 摘要

**System Prompt 要点**（详见 `analyzer.py::SYSTEM_PROMPT`）：

1. 角色定位：资深产品评论分析师
2. **动态主题发现**：明确要求 5-10 个主题，且"必须是动态发现的，不可使用固定类别"
3. **结构化输出**：强制 JSON，字段包括
   `topic / description / sentiment / review_ids / sample_count / confidence / conflicting_evidence`
4. **per_review 字段**：要求每条评论归属一个主主题，便于追溯
5. 防幻觉约束（见 §5）

**User Prompt 要点**：

- 注入"分析目标"，让 LLM 有侧重地发现主题
- 以结构化行注入评论：`{id, rating, version, text}`
- 末尾给出**期望的 JSON 模板**，强化输出格式

### 3.2 PRD 生成 Prompt 摘要

- 输入：分析目标 + 主题分析结果 + 评论原文索引（用于核对 ID）
- 输出 JSON：`problem_summary / requirements[] / version_plan[]`
- 每个需求必须携带 `source_review_ids`，便于追溯

### 3.3 测试用例 Prompt 摘要

- 输入：需求列表 + 评论原文索引
- 输出 JSON：`case_id / req_id / title / preconditions / steps[] /
  expected_result / source_review_ids / type / confidence`
- 强调用例要"能验证需求是否解决了评论中提出的具体问题"

## 4. 模型参数配置

| 参数 | 值 | 理由 |
|---|---|---|
| temperature | 0.3 | 偏低以保证结构化输出稳定，避免发散 |
| max_tokens | 4096 | 主题分析/PRD/测试用例输出体量适中，留足余量 |
| response_format | `{"type": "json_object"}` | 优先启用 JSON 模式；不支持时自动降级为文本+容错解析 |
| timeout | 60s | 单次请求容许较长推理时间 |
| 重试 | 3 次，指数退避 | 应对瞬时网络抖动与限流 |

## 5. 失败处理策略

`LLMClient` 实现的关键容错策略：

1. **指数退避重试**：1s → 2s → 4s，最多 3 次（见 `llm_max_retries`）
2. **JSON 模式降级**：若 provider 不支持 `response_format=json_object`
   （部分模型/服务报错），自动退回普通文本模式，再走容错解析
3. **容错 JSON 解析**：`utils/helpers.safe_json_loads` 依次尝试
   - 抽取 ```json ... ``` 代码块
   - 括号配对截取
   - `json-repair` 库修复
   - 全部失败抛 `LLMCallError` 并附原始内容片段
4. **不静默失败**：任何重试耗尽的失败都抛 `LLMCallError`，
   orchestrator 会将该步骤标记为 `failed` 但保留已完成步骤结果
5. **UI 透传**：失败原因通过进度回调透传到 Streamlit，用户可见

## 6. 防幻觉措施

这是项目设计中的核心要求，落实在多个层面：

### 6.1 Prompt 层约束（写在 `SYSTEM_PROMPT`）

- 明确告知 LLM："你只能基于用户实际提供的评论数据作答"
- **`review_ids` 必须严格来自输入的评论 ID，禁止编造任何 ID**
- "证据不足以支撑某结论时，confidence 必须标为 low"
- "宁可少给主题，也不要凑数"

### 6.2 代码层过滤（事后校验，不信任 LLM 自报）

在 `pipeline/analyzer.py::analyze` 中：

- 维护 `valid_ids` 集合（来自真实输入的 review_id）
- 对每个主题的 `review_ids` 做过滤：`[x for x in rids if x in valid_ids]`
- 即使 LLM 编造 ID，也会被静默剔除，不会污染下游

同样的过滤在 `prd_generator.py` 与 `testcase_generator.py` 中执行：
需求/用例的 `source_review_ids` 与 `req_id` 都会与真实集合核对，不存在的直接丢弃。

### 6.3 验证层兜底（`pipeline/validator.py`）

- 验证模块**不调用 LLM**，使用纯规则确定性校验
- 检查每个需求/用例的 `source_review_ids` 是否在真实评论中存在
- 无证据支撑的结论被标记为"假设"并建议移除
- 输出 `pass_rate` 与未通过项及原因

### 6.4 证据评估（`analyzer._evaluate_evidence`）

对每个主题自动生成证据评估说明：
- 样本数 0 → "无证据支撑，判定为假设，建议移除"
- 样本数 ≥5 且无矛盾反馈 → "证据充分"
- 矛盾反馈 → 如实记录到 `conflicting_evidence`

## 7. Token 用量统计

`LLMClient.total_usage` 累计每次调用的 token 消耗，结构：

```json
{
  "prompt_tokens": 0,
  "completion_tokens": 0,
  "total_tokens": 0,
  "call_count": 0
}
```

可在 orchestrator 返回结果中附加展示，便于成本审计。

## 8. 切换模型示例

切到 OpenAI `gpt-4o-mini`：

```env
LLM_API_KEY=sk-your-openai-key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

切到 DeepSeek 推理增强模型：

```env
LLM_API_KEY=sk-your-deepseek-key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-reasoner
LLM_MAX_TOKENS=8192
```
