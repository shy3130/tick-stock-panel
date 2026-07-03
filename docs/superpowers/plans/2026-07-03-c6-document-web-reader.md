# C6：文档 / 网页 Reader 实现计划

> **面向 AI 代理的工作者：** 先做文本层 reader；OCR 依赖重，另开评估。不得让 reader 变成任意文件/内网网页读取器。

**目标：** 用户可把公告、研报、网页、csv/xlsx 摘要作为 AI 分析上下文。统一抽取成 `DocumentEnvelope`，带来源、截断、warning。

**现状证据：**
- AI 分析入口已有 stock/financial/market recap service，但主要吃行情/财务/复盘结构化数据。
- 之前代理问题出现过 `httpx` 代理污染报错；网页读取必须 `trust_env=False`。
- Vibe C6 里 OCR 依赖为 `rapidocr_onnxruntime`，较重，本计划不引入。

**范围：** `.txt/.md/.csv/.xlsx` 稳定支持；`.pdf` 若现有依赖可文本抽取则支持，否则明确 unsupported。网页只抓公网 `http/https`。

## 文件

| 文件 | 动作 |
|---|---|
| `backend/app/services/document_reader.py` | 创建 reader |
| `backend/app/api/documents.py` | 创建上传/URL API |
| `backend/app/main.py` | 注册 router |
| `backend/tests/services/test_document_reader.py` | 创建 |
| `backend/tests/api/test_documents.py` | 创建 |

## 任务 1：DocumentEnvelope 契约

- [ ] dataclass 或 Pydantic model：

```python
class DocumentEnvelope(BaseModel):
    source: str              # filename or url
    kind: str                # text/markdown/csv/xlsx/pdf/html
    title: str
    text: str
    char_count: int
    truncated: bool
    warnings: list[str] = []
```

- [ ] 常量：
  - `MAX_BYTES = 5 * 1024 * 1024`
  - `MAX_CHARS = 20000`
  - `TABLE_PREVIEW_ROWS = 50`
- [ ] 超限截断时设置 `truncated=True`，不抛 500。

## 任务 2：先写失败测试

- [ ] `test_read_txt_truncates()`：长文本被截断，warning 存在。
- [ ] `test_read_csv_preview()`：csv 输出 Markdown table，包含 row_count。
- [ ] `test_read_xlsx_picks_first_sheet()`：用 Polars/fastexcel 读 xlsx。
- [ ] `test_reject_private_url()`：`http://127.0.0.1`、`http://10.0.0.1`、`file:///x` 被拒绝。
- [ ] `test_fetch_url_uses_trust_env_false()`：monkeypatch httpx client，断言不走环境代理。

## 任务 3：本地文件读取

- [ ] `read_document(filename: str, data: bytes) -> DocumentEnvelope`
- [ ] txt/md：`data.decode("utf-8", errors="replace")`
- [ ] csv/xlsx：`pl.read_csv/read_excel(BytesIO(...), infer_schema_length=0)`，只取前 50 行转 Markdown table。
- [ ] pdf：先检查项目是否已有 PDF 文本依赖；没有则返回 `kind="pdf", text="", warnings=["pdf text extraction unsupported"]`。
- [ ] 不长期保存原文件。

## 任务 4：网页读取

- [ ] `read_url(url: str) -> DocumentEnvelope`
- [ ] URL 校验：
  - scheme 只允许 `http/https`
  - hostname 解析到 IP 后拒绝 private/loopback/link-local/multicast
  - 拒绝无 hostname
- [ ] 请求：`httpx.Client(trust_env=False, timeout=10, follow_redirects=False)`。
- [ ] 手动逐跳处理 30x：每一跳请求前都校验 URL host/IP，限制最大跳数 5；禁止先请求内网跳转再事后检查最终 URL。
- [ ] 残留风险：DNS rebind 无法用 httpx 高层 API 完全固定解析 IP；本功能仅面向本机可信环境，后续若开放给多用户需下沉到连接层固定 IP。
- [ ] HTML 转文本：stdlib `html.parser` 或简单去 script/style，不加 heavy 依赖。

## 任务 5：API

- [ ] `POST /api/documents/read`：multipart upload，返回 envelope，不落盘。
- [ ] `POST /api/documents/read-url`：body `{url}`，返回 envelope。
- [ ] 两个接口都需要 auth 现有依赖保持一致。
- [ ] 响应只追加字段，不影响现有 AI API。

## 任务 6：AI 接入

- [ ] stock/financial/market recap 请求先只接受 `document_text` 或 envelope 列表参数，避免做 document store。
- [ ] prompt 段落前缀：`用户附件摘要（非行情事实）`。
- [ ] 超过 prompt 预算时只传 envelope.text 前 N 字。

## 任务 7：验证

```bash
cd backend
uv run --extra dev pytest tests/services/test_document_reader.py tests/api/test_documents.py -q
```

## 非目标

- 不做 OCR。
- 不抓内网/本机 URL。
- 不保存原文。
- 不做网页登录、JS 渲染或浏览器自动化。
