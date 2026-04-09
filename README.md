# Contract Review POC (Python Controller + Dify Workflows)

这个项目将**确定性流程**放在 Python，本地可 debug；将**需要 LLM 语义理解**的步骤放在 Dify：
- Dify Workflow A: 条款切分
- Dify Workflow B: 风险识别与分级

## 当前能力
- 读取 DOCX 正文和表格文本
- 文本清洗
- 顶层章节分段
- 调用 Dify 条款切分 workflow（逐段）
- 合并条款结果
- **条款规范化**：稳定 `clause_uid`、统一 `clause_id`、模板说明标记
- 调用 Dify 风险识别 workflow
- **风险规范化**：依据规范化、全量人工复核、去重归并
- 风险结果校验

## 目录结构

```text
contract_review_poc/
  app.py
  config.py
  requirements.txt
  .env.example
  data/
    input/
    runs/
  dify/
    clause_splitter.md
    risk_reviewer.md
  src/
    extract_docx.py
    clean_text.py
    split_segments.py
    dify_client.py
    workflow_runner.py
    parse_outputs.py
    merge_clauses.py
    normalize_clauses.py
    normalize_risks.py
    validate_risks.py
    checkpoint.py
```

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 配置
复制 `.env.example` 为 `.env`，填写：

```env
DIFY_BASE_URL=http://your-dify-host/v1
DIFY_CLAUSE_WORKFLOW_API_KEY=app-xxxxx
DIFY_RISK_WORKFLOW_API_KEY=app-legacy-risk
DIFY_ANCHORED_RISK_WORKFLOW_API_KEY=app-anchored
DIFY_MISSING_MULTI_RISK_WORKFLOW_API_KEY=app-missing-multi
DIFY_FAST_SCREEN_WORKFLOW_API_KEY=app-fast-screen
REVIEW_SIDE=supplier
CONTRACT_TYPE_HINT=service_agreement
REQUEST_TIMEOUT_SECONDS=900
RUN_ROOT=data/runs
DEBUG_SAVE_INTERMEDIATE=1
FAST_SCREEN_ENABLED=1
FAST_SCREEN_MAX_CANDIDATES=12
```

说明：
- 仍支持旧变量 `DIFY_RISK_WORKFLOW_API_KEY`（兼容回退）。
- Anchored 风险工作流优先使用 `DIFY_ANCHORED_RISK_WORKFLOW_API_KEY`，缺失时回退到旧变量。
- Missing / Multi-Clause 风险工作流优先使用 `DIFY_MISSING_MULTI_RISK_WORKFLOW_API_KEY`，缺失时回退到旧变量。
- Fast Screen 工作流使用 `DIFY_FAST_SCREEN_WORKFLOW_API_KEY`，`FAST_SCREEN_ENABLED=1` 时必填。

## 运行

### 只做本地预处理（不调用 Dify）
```bash
python app.py /path/to/contract.docx --dry-run
```

### 运行完整流程
```bash
python app.py /path/to/contract.docx --run-id live_test_001
```

### 失败后从已有 segment 结果断点续跑
```bash
python app.py /path/to/contract.docx --run-id live_test_001 --resume
```

## 输出说明
每次运行会在 `data/runs/<run_id>/` 下生成：

- `extracted_text.txt`
- `cleaned_text.txt`
- `segments.json`
- `clauses/segment_x.json`
- `merged_clauses_raw.json`
- `merged_clauses.json`
- `risk_result_raw.json`
- `risk_result_normalized.json`
- `risk_result_validated.json`

### `merged_clauses.json` 中新增字段
- `clause_uid`: 稳定唯一主键
- `source_clause_id`: 模型原始条款编号
- `clause_kind`: `contract_clause` / `placeholder_clause` / `note_clause`（兼容旧值 `template_instruction`）
- `source_excerpt`: 原文摘录（默认等于 `clause_text`）
- `numbering_confidence`: 条款编号置信度（可为空）
- `title_confidence`: 标题识别置信度（可为空）
- `is_boilerplate_instruction`: 是否模板说明/填写提示
- `text_hash`: 条款文本摘要 hash

### `risk_result_normalized.json` / `risk_result_validated.json` 中新增字段
- `clause_uid`
- `risk_source_type`: `anchored` / `missing_clause` / `multi_clause`
- `basis_rule_id`
- `basis_summary`
- `review_required_reason`
- `auto_apply_allowed`（固定为 false）
- `is_boilerplate_related`
- `merged_from_risk_ids`
- `suggestion_minimal` / `suggestion_optimized`
- `evidence_confidence`
- `quality_flags`
- `related_clause_ids` / `related_clause_uids`

## Dify Workflow 输入输出约定

### Workflow A: Clause Splitter
输入：
- `segment_id`
- `segment_title`
- `segment_text`

输出：
- `clauses`（建议绑定到 LLM 节点 `text`）
- 每个 clause 支持字段：`clause_id`、`clause_title`、`clause_text`、`segment_id`、`segment_title`、
  `clause_kind`、`source_excerpt`、`numbering_confidence`、`title_confidence`
- 默认值约定：
  - `clause_kind`: `contract_clause`
  - `source_excerpt`: 回退到 `clause_text`
  - `numbering_confidence` / `title_confidence`: `null`

### Workflow B: Risk Reviewer
输入：
- `clauses_json`
- `review_side`
- `contract_type_hint`

输出：
- `text`（建议绑定到 LLM 节点 `text`）

### Phase 3 风险双路架构（代码侧）
- 风险识别链路已按代码接口拆成两路：
  - Anchored 风险工作流（仅 anchored）
  - Missing / Multi-Clause 风险工作流（仅 missing_clause / multi_clause）
- Python 聚合层负责：
  - normalize（统一补默认值、条款映射）
  - validate（统一 schema 校验）
  - dedupe（anchored/missing/multi 的确定性去重）
  - 轻量规则回调（风险等级基线修正）
- 本阶段尚未包含“风险精炼重写工作流”，后续阶段再接入。
- Anchored 路径新增本地 Python 预处理：
  - 对单条 clause 做确定性过滤（placeholder / note / 空白正文跳过）
  - 构造标准化 anchored payload（含 `clause_context`）
  - 仅当 `should_review=true` 才调用 Dify Anchored 风险工作流

## 说明
- 当前版本默认**所有风险项都必须人工复核**。
- 当前版本不会自动修改合同，也不会自动采纳风险建议。
- 风险结果会做本地去重归并，并补充规范化依据字段。

## Export reviewed DOCX with Word comments
After you have `merged_clauses.json` and `risk_result_validated.json`, you can write real Word comments back into the original DOCX:

```bash
python -m src.docx_comments ./1.docx \
  data/runs/live_test_001/merged_clauses.json \
  data/runs/live_test_001/risk_result_validated.json \
  --out data/runs/live_test_001/reviewed_comments.docx \
  --author "合同审查系统"
```

The exporter will:
- add true Word/WPS comments into the DOCX
- anchor comments to the best-matching paragraph using `anchor_text`, `evidence_text`, and clause text
- duplicate a comment across each related clause for multi-clause risks
- only export comments for `pending` and `accepted` risks by default

You can control included statuses with `--statuses`, for example:

```bash
python -m src.docx_comments ./1.docx \
  data/runs/live_test_001/merged_clauses.json \
  data/runs/live_test_001/risk_result_validated.json \
  --out reviewed_comments.docx \
  --statuses accepted
```


## DOCX 批注导出说明

当前导出的 comments 默认仅包含：风险等级、风险标签、问题、依据、建议。
系统内部的复核规则（例如 `review_required_reason`、`needs_human_review`、`auto_apply_allowed`）不会写入 DOCX comment，以避免污染最终批注可读性。
若命中的条款属于模板说明或留白提示，则会额外写入一条精简提示：`当前条款仍含模板说明或留白内容，建议在定稿前补全或删除。`

## Web UI（新增）

本仓库新增了一个可直接运行的前端与轻量 Web API：

- 前端目录：`frontend/`
- Python Web API：`web_api.py`
- 运行说明：`FRONTEND_RUN_GUIDE.md`

前端支持：上传 DOCX、发起审查、轮询状态、展示三栏审查结果页、下载带批注 DOCX。
