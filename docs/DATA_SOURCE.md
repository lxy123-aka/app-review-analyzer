# 数据源说明 (DATA_SOURCE)

本文档说明本工具所用的数据源（Apple App Store 官方 RSS Feed 接口）、
其使用方式、限制与局限性。**所有这些限制都会在结果中透明说明**。

## 1. 数据源

本项目使用 Apple 官方公开的 **RSS Customer Reviews Feed** 接口抓取美国区
App Store 评论。接口地址：

```
https://itunes.apple.com/{country}/rss/customerreviews/id={appId}/sortBy=mostRecent/page={page}/json
```

参数说明：

| 参数 | 含义 | 取值 |
|---|---|---|
| `country` | 区域代码 | 本工具默认 `us`（美国区） |
| `appId` | App 数字 ID | 从 App Store URL 中的 `/id<数字>` 解析 |
| `sortBy` | 排序方式 | 固定 `mostRecent`（最新优先） |
| `page` | 页码 | 从 1 开始，但 page=1 通常只返回应用信息，评论从 page=2 开始 |
| 返回格式 | JSON | 末尾 `/json` 强制 JSON（默认为 XML） |

### 示例请求

```
https://itunes.apple.com/us/rss/customerreviews/id=1357527742/sortBy=mostRecent/page=2/json
```

返回结构（节选）：

```json
{
  "feed": {
    "author": {...},
    "entry": [
      {
        "id": {"label": "11875321001"},
        "title": {"label": "Great app but pricing kills it"},
        "content": {"label": "I've been using this app for 6 months..."},
        "im:rating": {"label": "3"},
        "im:version": {"label": "6.42.0"},
        "author": {"name": {"label": "jrunner1987"}},
        "updated": {"label": "2026-08-15T08:30:00Z"}
      },
      ...
    ]
  }
}
```

注意 Apple RSS 结构嵌套较深（多数字段包在 `{"label": "..."}` 中），
[`pipeline/collector.py::_normalize_raw_review`](../pipeline/collector.py)
负责将其扁平化为内部统一 dict：

```json
{
  "review_id": "11875321001",
  "title": "...",
  "content": "...",
  "rating": 3,
  "version": "6.42.0",
  "author": "...",
  "updated": "2026-08-15T08:30:00Z",
  "country": "us"
}
```

## 2. 应用元信息接口

为获取应用名、开发者、分类等元信息，使用 lookup 接口：

```
https://itunes.apple.com/lookup?id={appId}&country={country}
```

该接口返回 `results[0]` 含 `trackName` / `artistName` / `primaryGenreName`。
元信息获取失败不影响评论抓取（参见 `collector.fetch_app_meta`）。

## 3. 分页策略

**关键事实**：Apple RSS 接口每次最多返回 50 条评论，且：

- `page=1` 通常只返回应用信息，**不含评论**
- 评论从 `page=2` 开始
- 抓到空 `entry` 列表即认为没有更多评论，停止翻页
- 超过 `MAX_REVIEW_PAGES` 上限也会停止（默认 10 页，最多 ~500 条）

`pipeline/collector.collect_from_rss` 的实现：

1. 从 `page=2` 开始循环到 `page=2+max_pages-1`
2. 每页请求后调用 `time.sleep(REQUEST_INTERVAL)` 限速（默认 1.5s）
3. 基于 `review_id` 去重，避免同一评论跨页重复
4. 单页失败不致命，记录失败页并继续下一页

## 4. 速率限制

- 每页请求间隔至少 1 秒（`.env` 中 `REQUEST_INTERVAL`，默认 1.5s）
- HTTP 超时 15s（`HTTP_TIMEOUT`）
- 设置 User-Agent 标识，遵守爬取礼仪
- 失败页不重试（避免短时间内对同一页反复请求）

## 5. 数据源局限性（重要！）

> ⚠️ 以下限制都会在结果中**透明说明**（`CollectReport.notes` + UI 提示）

| 局限 | 影响 | 应对措施 |
|---|---|---|
| **不保证返回所有历史评论** | RSS 只返回最近若干页，无法获取全量历史 | 在 UI 与报告中明确标注"评论数量受限是接口限制" |
| **单页 ≤50 条，page=1 无评论** | 评论从 page=2 起算 | 抓取循环从 page=2 开始 |
| **无更多评论时返回空** | 抓取自然停止，无需报错 | 空列表即停止翻页 |
| **区域限制** | 仅默认美国区，其他区需传 `country` | URL 解析时自动提取国家代码 |
| **应用下架/ID 错误** | 接口返回空或 404 | 抛出 `CollectorError`，UI 明确报错 |
| **评论条数偏少时** | AI 分析置信度低 | 清洗报告中提示"有效评论 < 5 条，置信度可能低" |

## 6. 本地导入（离线/演示）

为支持离线演示与快速验证，本工具提供两种本地数据导入方式：

### 6.1 JSON 文件

支持两种结构：

- **标准化 list[dict]**：每项含 `review_id` / `content` 字段
- **RSS 原始结构**：`{"feed": {"entry": [...]}}`，由 collector 自动扁平化

参考样本：[`data/sample_reviews.json`](../data/sample_reviews.json)

### 6.2 CSV 文件

期望列：

| 列名 | 必填 | 说明 |
|---|---|---|
| `review_id` | ✅ | 评论唯一 ID（或 `id`） |
| `content` | ✅ | 评论正文（或 `review`） |
| `rating` | 可选 | 评分 1-5 |
| `version` | 可选 | App 版本号 |
| `title` | 可选 | 评论标题 |
| `author` | 可选 | 作者 |
| `updated` | 可选 | 日期 |

### 6.3 在 UI 中使用

- 直接点击「📦 加载样本数据」按钮，使用内置 18 条样本
- 或通过「上传本地 JSON/CSV 文件」上传自定义数据

## 7. 抓取报告字段

每次抓取/导入都会生成 `CollectReport`，字段如下：

```json
{
  "app": {"app_id": "...", "name": "...", "developer": "...", ...},
  "pages_attempted": 10,
  "pages_succeeded": 8,
  "pages_failed": 2,
  "raw_count": 312,
  "per_page_counts": [50, 50, 50, 50, 50, 50, 12, 0],
  "data_source": "rss | json_file | csv_file",
  "notes": ["数据源: Apple RSS Feed...", "page=4 抓取失败: ..."]
}
```

该报告会作为「抓取报告」分区在 UI 中展示，确保用户对数据来源完全可见。
