# 小肥鱼 AI 公司 v4

一个人的 AI 公司：自己建岗位、按项目招人、派活给流水线、跟每个员工单独聊，最后把成品导出成 Word 或 Markdown；每一步花了多少钱都写在明面上。

![任务页](docs/screen-task.png)

## 怎么启动

**方式一（推荐）**：双击 `小肥鱼AI公司.exe`（或桌面快捷方式），黑窗口起来后浏览器自动打开。关掉黑窗口就是关公司。

**方式二**：双击 `start.bat`（需要本机有 Python）。

浏览器打开 `http://127.0.0.1:8811`；端口被占会自动往后挪（8812、8813…）。

## 五个页面

| 页面 | 干什么 |
|---|---|
| **我的团队** | 当前项目在岗的员工。每张卡上有「聊天」「修改」「开除」。 |
| **招募市场** | 两个页签：**我的员工库**（你自己写过的岗位，随时再招一次，可删）／**系统模板**（内置 25 个，5 族）。按项目类型排前。 |
| **任务** | 左任务列表，中逐句对话流（谁说的、多少 token、多少钱、多久），右看板（本单花费/预算/每句话的钱/步骤/谁花的钱）。 |
| **老板看板** | 全公司视角：累计花费、token、产出条数、谁最烧钱、按项目、任务状态、最近任务；可切「当前项目 / 全部项目」。 |
| **找人聊天** | 一对一私聊，不走流水线。按他的职责和记忆回你，气泡上标 token 与费用。 |

![老板看板](docs/screen-dash.png)

## 员工库（自己写的岗位）

- 招人时勾「同时存进我的员工库」→ 这个岗位进库，以后换项目也能一键再招。
- 库里每个岗位旁有「删掉」，只删模板，不影响已在团队里的人。
- 空库时点右上「写一个新员工」从零写。

![招募市场](docs/screen-hire.png)

## 派一个活

1. 填任务名、目标、验收标准、预算（默认 5 元）。
2. 选协作方式：接力 / 并排 / 审稿。
3. 想省事就选一条**流水线模板**（下拉里「论文三连」之类），自动按顺序排好；也可以手动点员工。
4. 需要你点头的那步勾「等老板批」；点「当前顺序存成模板」可把这次的排法存下来复用。
5. 点「派下去」，再点「开始」。跑到审批步会停，去右侧「步骤」点通过/退回。

## 员工记忆

点员工卡「修改」，弹窗里有「他记住的事」：每次加一条（例：`不要用"随着…"开头；结尾给可执行建议`），以后每次任务和聊天都会带上。可单条删除。

![找人聊天](docs/screen-chat.png)

## 导出成品

任务详情右上角：

- **Word** → 下载 `.docx`（含目标、验收、花费、步骤、成品、审稿意见；标题按 `#`/`##` 分级）
- **MD** → 下载 `.md`

服务端现拼文件，不依赖 Word、不依赖第三方库。

## 钱怎么算

- 模型 `deepseek-chat`（员工里可改 `deepseek-reasoner`）。
- 计价：输入未命中 2 元/百万 token、输出 3 元/百万 token（`app.py` 顶部 `PRICE_IN_MISS` / `PRICE_OUT`）。
- 每单累计，超出预算自动停；侧栏「今日花费」= 当前项目今天所有产出（含聊天）。

## 数据在哪

- 数据库 `company.db`（SQLite，直接复制即备份；跟 exe 放一起）
- 前端 `ui/index.html` 单文件，改完刷新即生效
- 后端 `app.py`，标准库 HTTP + SQLite，无第三方依赖
- key：优先环境变量 `DEEPSEEK_API_KEY`，其次同目录 `key.txt`

## 接口（想接别的界面时用）

- `GET /api/state?pid=` 项目、员工、任务、统计
- `GET /api/presets?pid=` → `{presets, mine, system, kind}`
- `POST /api/projects` / `POST /api/employees` / `POST /api/employees/{id}`（`_delete` 删；`save_preset` 存库）
- `POST /api/presets/{id}`（`_delete`，仅 mine）
- `GET|POST /api/chat/{eid}` 一对一聊天
- `GET|POST /api/memory/{eid}` 员工记忆（`text` 追加 / `del_id` 删 / `items` 全量替换）
- `GET|POST /api/pipelines`、`POST /api/pipelines/{id}`（删除）流水线模板
- `GET /api/tasks/{id}/export?fmt=md|docx` 导出成品
- `GET /api/dashboard?scope=project|all&pid=` 老板看板数据
- `POST /api/tasks`、`/api/tasks/{id}/start|delete`、`/api/steps/{sid}/approve|reject`
- `GET /api/events/{tid}` SSE 实时流

## 已知限制

- 只有 DeepSeek 真接口；通义/豆包/Kimi 位置留着，有 key 再加。
- 「并排」目前仍是同进程排队，界面按并行展示；真并发以后改。
- 记忆是「最近若干条 + 关键词」，不是向量检索。
- 导出只有 Word / Markdown，没有 PDF（要 PDF 先用 Word 打开另存）。

## 从源码跑 / 打包

```
pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --console ^
  --name "ai-company" --add-data "ui;ui" --exclude-module tkinter app.py
```

产物在 `dist/ai-company.exe`，改名后放回项目根目录即可（数据库跟着 exe 走）。
