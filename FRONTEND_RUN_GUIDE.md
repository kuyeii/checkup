# Web UI 运行说明（前端 + 轻量 Web API）

> 目标：复刻类似 WPS 的“三栏审查”体验：左侧 DOCX 原文可编辑（POC）、发生修改后自动生成修订批注、右侧展示风险点并支持一键定位原文。

## 1) 启动后端（FastAPI）

在项目根目录：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动 API
./run_backend_web.sh
```

默认地址：`http://localhost:8000`

## 2) 启动前端（Vite + React）

另开一个终端，进入 `frontend/`：

```bash
cd frontend
npm install
npm run dev
```

默认地址：`http://localhost:5173`

前端已配置代理：`/api/*` 会自动转发到 `http://localhost:8000`。

## 3) 使用方式

- 点击 **“选择DOCX”** 上传合同原件（仅支持 `.docx`）。
- 点击 **“发起审查”**，后端会执行全流程并生成风险点。
- 右侧 **“风险点”** 列表可筛选（高/中/低）、搜索，并支持 **“定位原文”**。
- 左侧原文支持直接编辑，任何增删改会触发 **修订批注**（右侧灰底的批注气泡 + 文本左侧红条）。
- 审查完成后，可在顶栏点击 **“下载带批注DOCX”**（后端导出的 Word/WPS 批注）。

## 4) 演示数据

前端提供 **“加载演示”**：

- 会加载 `frontend/public/demo/1.docx` 并渲染
- 会调用 `/api/demo/result`（读取 `frontend/public/demo/review_payload.json`）

便于不跑模型也能直接看 UI。
