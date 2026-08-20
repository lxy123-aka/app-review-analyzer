# App Store 评论分析与版本规划工具

> 从一条 App Store 链接出发，自动完成：评论抓取 → 清洗 → AI 动态分析 →
> 证据评估 → PRD 生成 → 测试用例生成 → 可追溯性验证，并在 Web UI 上
> 展示完整流程与所有中间结果。

## 项目简介

本工具面向产品/QA 团队与面试场景，演示了"从用户真实评论到可执行需求
与测试用例"的完整闭环。核心特点：

- **AI 动态分析**：不预设固定分类，由 LLM 从评论中动态发现主题
- **证据可追溯**：每个结论都关联到具体 review_id，可在 UI 中点击查看原文
- **防幻觉设计**：Prompt 约束 + 代码层 ID 过滤 + 确定性验证三重保障
- **完全本地运行**：Python + Streamlit，单条命令启动，无需部署

## 功能特性

1. **评论抓取**：解析 App Store URL → 调用官方 RSS Feed → 分页循环抓取
2. **数据清洗**：去重、空值过滤、字段标准化、轻量语言检测
3. **AI 动态分析**（核心）：
   - LLM 动态发现 5-10 个问题主题
   - 每条评论归类 + 情感标记
   - 评估证据充分性、矛盾反馈、置信度
4. **PRD 生成**：基于分析结果生成需求列表、优先级、版本规划
5. **测试用例生成**：每条用例关联需求 ID 与来源评论 ID
6. **可追溯性验证**：纯规则校验，输出通过率与未通过项

## 技术栈

| 层 | 技术 | 用途 |
|---|---|---|
| 前端 | Streamlit ≥ 1.32 | Web UI（支持 `st.status` 进度展示） |
| LLM | DeepSeek / OpenAI 兼容 | 语义分析（动态主题、PRD、测试用例） |
| 抓取 | requests | Apple RSS Feed 调用 |
| 数据 | pandas | 评论表格展示与统计 |
| 配置 | python-dotenv | API Key 等敏感信息管理 |

## 项目结构

```
app-review-analyzer/
├── app.py                       # Streamlit 主入口
├── requirements.txt             # 依赖与版本约束
├── .env.example                 # 环境变量模板
├── .gitignore
├── README.md
├── config.py                    # 配置管理（读取 .env）
├── pipeline/
│   ├── __init__.py
│   ├── orchestrator.py          # 工作流编排
│   ├── collector.py             # 评论抓取
│   ├── cleaner.py               # 数据清洗
│   ├── analyzer.py             # AI 动态分析（核心）
│   ├── prd_generator.py        # PRD 生成
│   ├── testcase_generator.py   # 测试用例生成
│   └── validator.py            # 可追溯性验证
├── models/
│   ├── __init__.py
│   └── llm_client.py           # LLM 调用封装
├── utils/
│   ├── __init__.py
│   ├── url_parser.py           # URL 解析
│   └── helpers.py              # 通用工具
├── data/
│   └── sample_reviews.json     # 离线样本数据
└── docs/
    ├── MODEL_CONFIG.md         # 模型配置说明
    └── DATA_SOURCE.md          # 数据源说明
```

## 安装步骤

> 需要 Python 3.9+

```bash
# 1. 克隆项目
git clone <repo-url>
cd app-review-analyzer

# 2. 创建虚拟环境（推荐）
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY 等真实值
```

## 运行方法

```bash
streamlit run app.py
```

浏览器会自动打开 `http://localhost:8501`。

### 三种使用方式

1. **在线抓取**：在顶部输入 App Store 美国区链接 + 分析目标 → 点击「开始分析」
2. **加载样本**：点击「📦 加载样本数据」按钮，使用内置 50 条真实缓存评论快速演示
3. **导入文件**：上传本地 JSON/CSV 文件（结构见 [docs/DATA_SOURCE.md](docs/DATA_SOURCE.md)）

### 离线缓存结果（供面试官审查）

项目已预置两份完整管线输出缓存，面试官即使无网络/无 API Key 也能审查交付物质量：

| 文件 | App | 内容 |
|---|---|---|
| `data/cached_result.json` | 1357527742（Workout For Women Fit At Home） | 原始评论 + 清洗报告 + 分析结果（17 主题）+ PRD（10 需求/3 版本）+ 测试用例（17 条）+ 验证报告（100% 通过） |
| `data/cached_result_app_839285684.json` | **839285684（笔试指定 App）** | 250 条评论 + 246 条清洗后 + 70 主题 + 20 需求/4 版本 + 23 测试用例 + 验证报告（100% 通过） |

面试官能直接打开这些 JSON 文件审查完整的 PRD、测试用例、验证报告等交付物。

### 示例 App Store 链接

```
https://apps.apple.com/us/app/workout-for-women-home-gym/id839285684
```

也支持直接输入纯数字 App ID：`839285684`

## 数据源说明

使用 Apple 官方 **RSS Customer Reviews Feed** 接口：

```
https://itunes.apple.com/us/rss/customerreviews/id={appId}/sortBy=mostRecent/page={page}/json
```

**重要限制**：

- RSS 接口**不保证返回所有历史评论**，通常仅返回最近若干页
- 单页最多 50 条，`page=1` 通常只返回应用信息，评论从 `page=2` 开始
- 评论数量受限是接口限制，结果中已**透明说明**

详见 [docs/DATA_SOURCE.md](docs/DATA_SOURCE.md)。

## 技术选型说明

### 为什么用 LLM 做动态分类而不是关键词匹配？

关键词匹配预设固定类别（如"价格"、"崩溃"），无法适应不同 App 的评论特征。
LLM 可针对任意 App 的真实评论动态发现主题（如"订阅定价过高"、"启动闪退"、
"Apple Watch 支持缺失"），泛化能力更强。

### 为什么用 DeepSeek 作为默认？

- 完全兼容 OpenAI SDK，切换 provider 仅需改 `.env`，零代码改动
- 国内访问稳定，价格友好，支持 JSON 模式
- 详见 [docs/MODEL_CONFIG.md](docs/MODEL_CONFIG.md)

### 为什么验证模块不用 LLM？

可追溯性验证需要**确定性、可复现**的结论。若用 LLM 校验，自身可能再次
幻觉，无法作为"兜底"。因此 `validator.py` 使用纯规则校验：
- 检查每个需求/用例的 `source_review_ids` 是否在真实评论中存在
- 检查测试用例是否关联了有效需求 ID
- 无证据支撑的结论被标记为"假设"并建议移除

## 模型配置摘要

| 项 | 默认 | 说明 |
|---|---|---|
| Provider | DeepSeek | 可改 OpenAI 等 |
| 模型 | `deepseek-chat` | JSON 模式支持 |
| 温度 | 0.3 | 偏低保证稳定 |
| 重试 | 3 次（指数退避） | 1s/2s/4s |
| 防幻觉 | Prompt + 代码过滤 + 验证 | 三重保障 |

详见 [docs/MODEL_CONFIG.md](docs/MODEL_CONFIG.md)。

## 防幻觉措施速览

1. **Prompt 层**：要求 LLM "review_ids 必须严格来自输入，禁止编造"，
   "证据不足时 confidence 必须标为 low"
2. **代码层**：`analyzer/prd_generator/testcase_generator` 都维护 `valid_ids`
   集合，事后过滤掉 LLM 编造的 ID
3. **验证层**：`validator` 用纯规则再校验一遍，无证据结论标记为"假设"

## 输出示例

UI 最终分 8 个分区展示：

1. 执行步骤总览（每步耗时与状态）
2. 抓取报告与原始评论
3. 清洗报告与清洗后评论
4. AI 动态分析结果（主题列表，每条可点击查看来源评论原文）
5. 产品需求文档 PRD（含版本规划）
6. 测试用例（关联需求与评论）
7. 可追溯性验证报告（通过率与未通过项）

## 许可证

MIT
