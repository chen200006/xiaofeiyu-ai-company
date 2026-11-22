# -*- coding: utf-8 -*-
"""小肥鱼 AI 公司 v2 —— 项目 / 招募 / 编辑员工 / 更漂亮的界面"""
import io
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from urllib.parse import parse_qs, urlparse

import sys
import webbrowser

if getattr(sys, "frozen", False):
    # 打包成 exe：数据放 exe 旁边，静态资源在临时解包目录
    BASE = os.path.dirname(os.path.abspath(sys.executable))
    RES = getattr(sys, "_MEIPASS", BASE)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    RES = BASE

DB_PATH = os.path.join(BASE, "company.db")
UI_PATH = os.path.join(RES, "ui", "index.html")
IMAGE_DIR = os.path.join(BASE, "images")
PORT = int(os.environ.get("AC_PORT", "8811"))

PRICE_IN = 2.0
PRICE_OUT = 3.0
# 深度思考：所有模型调用默认走 reasoner（可用 AC_DEEP=0 关闭）
DEEP_THINK = os.environ.get("AC_DEEP", "1") != "0"
MODEL_CHAT = "deepseek-chat"
MODEL_DEEP = "deepseek-reasoner"
PRICES = {MODEL_CHAT: (2.0, 3.0), MODEL_DEEP: (4.0, 16.0)}
_TLS = threading.local()
API_BASE = "https://api.deepseek.com/chat/completions"

SUBS = {}
APPROVALS = {}

# 桌面版会把自己「显示窗口」的函数挂到这里，/api/show 就能把主窗口叫回来
SHOW_HOOK = None

# 桌面外壳：页面请求「把这个项目另开一个窗口」时，把 pid 排进这个队列
OPEN_REQUESTS = []

LOCK = threading.Lock()


def _find_key():
    """key 只从两处来：环境变量，或 exe 同目录的 key.txt（不写进代码、不进仓库）。"""
    env = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env.startswith("sk-"):
        return env
    for name in ("key.txt", ".key"):
        fp = os.path.join(BASE, name)
        if not os.path.exists(fp):
            continue
        try:
            txt = io.open(fp, encoding="utf-8-sig", errors="ignore").read()
        except Exception:
            continue
        m = re.search(r"sk-[A-Za-z0-9]{20,}", txt)
        if m:
            return m.group(0)
    return ""


API_KEY = _find_key()

def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, kind TEXT DEFAULT '论文报告',
  goal TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS employees(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INT, name TEXT NOT NULL, title TEXT NOT NULL,
  emoji TEXT DEFAULT '🧑',
  duty TEXT, input_spec TEXT, output_spec TEXT,
  model TEXT DEFAULT 'deepseek-chat', memory INTEGER DEFAULT 1,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS presets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  family TEXT, emoji TEXT, title TEXT, tag TEXT,
  duty TEXT, input_spec TEXT, output_spec TEXT,
  mine INTEGER DEFAULT 0, src_emp INT
);
CREATE TABLE IF NOT EXISTS chats(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INT, emp_id INT, role TEXT, content TEXT,
  tokens_in INT DEFAULT 0, tokens_out INT DEFAULT 0,
  cost REAL DEFAULT 0, ms INT DEFAULT 0, ts TEXT
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INT, title TEXT NOT NULL, goal TEXT, accept TEXT,
  budget REAL DEFAULT 5.0, mode TEXT DEFAULT 'relay', status TEXT DEFAULT 'pending',
  total_cost REAL DEFAULT 0, total_in INTEGER DEFAULT 0, total_out INTEGER DEFAULT 0,
  created_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS steps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INT, idx INT, emp_id INT, status TEXT DEFAULT 'pending',
  needs_approval INT DEFAULT 0, input_text TEXT, output TEXT,
  tokens_in INT DEFAULT 0, tokens_out INT DEFAULT 0, cost REAL DEFAULT 0,
  started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INT, step_id INT, sender TEXT, receiver TEXT, kind TEXT,
  content TEXT, tokens_in INT DEFAULT 0, tokens_out INT DEFAULT 0,
  cost REAL DEFAULT 0, ms INT DEFAULT 0, ts TEXT
);
CREATE TABLE IF NOT EXISTS memory(
  id INTEGER PRIMARY KEY AUTOINCREMENT, emp_id INT, task_id INT, content TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS kb(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INT, task_id INT, emp_id INT,
  title TEXT, content TEXT, kind TEXT, tags TEXT,
  source TEXT DEFAULT 'auto', ts TEXT
);
CREATE TABLE IF NOT EXISTS pipelines(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INT, name TEXT, spec TEXT, created_at TEXT
);
"""


def _safe_name(t):
    return re.sub(r'[\\/:*?"<>|]+', "_", (t or "task")).strip()[:60] or "task"


def _task_markdown(tid):
    c = db()
    t = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        return None, None
    msgs = c.execute("SELECT * FROM messages WHERE task_id=? ORDER BY id", (tid,)).fetchall()
    steps = c.execute(
        "SELECT s.*,e.title emp_title FROM steps s LEFT JOIN employees e ON e.id=s.emp_id"
        " WHERE s.task_id=? ORDER BY s.idx", (tid,)).fetchall()
    c.close()
    lines = ["# " + t["title"], ""]
    lines += ["- 目标：" + (t["goal"] or "—"), "- 验收：" + (t["accept"] or "—"),
              "- 花费：%.4f 元　用时：%s" % (t["total_cost"] or 0, t["created_at"] or "")]
    lines += ["", "## 流水线", ""]
    for st in steps:
        lines.append("%d. %s　%s" % (st["idx"] + 1, st["emp_title"] or "员工",
                                     {"done": "完成", "running": "进行中",
                                      "pending": "未开始", "waiting": "等老板批"}.get(
                                         st["status"], st["status"])))
    final = [m for m in msgs if m["kind"] == "deliverable"]
    for m in final[-1:]:
        lines += ["", "## 成品", "", m["content"] or ""]
    notes = [m for m in msgs if m["kind"] == "reply" and "问题清单" in (m["content"] or "")]
    if notes:
        lines += ["", "## 审稿意见", "", notes[-1]["content"] or ""]
    lines += ["", "---", "", "由「小肥鱼 AI 公司」生成，共 %d 条产出。" % len(msgs)]
    return t["title"], "\n".join(lines)


def _docx_bytes(title, text):
    """不依赖第三方库，直接拼一个最小可用的 .docx。"""
    import zipfile
    from xml.sax.saxutils import escape

    def para(txt, bold=False, size=21, space_after=80):
        rpr = ('<w:rPr><w:rFonts w:ascii="Segoe UI" w:eastAsia="宋体" w:hAnsi="Segoe UI"/>'
               + ('<w:b/>' if bold else '') +
               '<w:sz w:val="%d"/><w:szCs w:val="%d"/></w:rPr>' % (size, size))
        pr = ('<w:pPr><w:spacing w:after="%d" w:line="330" w:lineRule="auto"/></w:pPr>'
              % space_after)
        return ('<w:p>' + pr + '<w:r>' + rpr +
                '<w:t xml:space="preserve">' + escape(txt) + '</w:t></w:r></w:p>')

    body = [para(title, bold=True, size=34, space_after=240)]
    for line in text.split("\n"):
        raw = line.rstrip()
        if not raw.strip():
            body.append(para("", space_after=60))
        elif raw.startswith("### "):
            body.append(para(raw[4:], bold=True, size=24, space_after=120))
        elif raw.startswith("## "):
            body.append(para(raw[3:], bold=True, size=28, space_after=160))
        elif raw.startswith("# "):
            body.append(para(raw[2:], bold=True, size=32, space_after=200))
        elif raw.startswith(("- ", "* ")):
            body.append(para("· " + raw[2:]))
        else:
            body.append(para(raw))

    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>' + "".join(body) +
           '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="1440" w:right="1247" w:bottom="1440" w:left="1247"'
           ' w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>'
           '</w:body></w:document>')

    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
          'officedocument.wordprocessingml.document.main+xml"/></Types>')

    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def task_export(tid, fmt):
    title, md = _task_markdown(tid)
    if md is None:
        return None, None, None
    if fmt == "docx":
        data = _docx_bytes(title, md.split("\n", 1)[1] if "\n" in md else md)
        return title + ".docx", data, ("application/vnd.openxmlformats-officedocument"
                                       ".wordprocessingml.document")
    return title + ".md", md.encode("utf-8"), "text/markdown; charset=utf-8"

PRESET_SEED = [
    ("论文报告", "🎯", "选题策划", "策划", "把宽泛的题目收窄成一个能写的论文题目，给出 3 个候选，说明每个的可行性与风险，并推荐一个。", "课程要求与兴趣方向", "3 个候选题目 + 推荐理由（每个≤150字）"),
    ("论文报告", "📚", "文献猎手", "研究", "列出该主题下应该读的 8—12 篇文献类型与检索关键词（中文/英文各半），指出哪些观点是主流、哪些有争议。不编造具体文献标题。", "已定的论文题目", "检索关键词表 + 应读文献类型清单 + 争议点"),
    ("论文报告", "🔍", "研究员", "研究", "把题目拆成 4—6 个能写的要点，每个要点给 2 条具体材料或数据，标出哪条需要作者自己核实。不写正文。", "任务目标与验收标准", "条目式要点清单，每条≤80字，附材料来源说明"),
    ("论文报告", "✍️", "写手", "写作", "按要点的顺序写成正文：有小标题，先结论后论证，每段只讲一件事，不用“随着…的发展”这类套话，允许用第一人称表达判断。", "研究员的要点清单", "Markdown 正文，含 4—6 个小标题"),
    ("论文报告", "🧱", "结构编辑", "写作", "只调结构与衔接：检查每段是否只讲一件事、小标题是否平行、段落之间是否有过渡句。输出修改后的完整正文，不改观点。", "写手的正文", "修订后的完整正文"),
    ("论文报告", "🧐", "审稿", "审核", "负责挑错：找出空话、无依据的断言、结构重复、字数不足的问题，逐条指出并给出改法，最后给出评分（内容/结构/语言 各 10 分）。", "写手的正文", "问题清单（按严重程度排序）+ 三项评分"),
    ("论文报告", "🔖", "引用校对", "审核", "检查文中的数据与引用口径：哪些是断言、哪些有出处、哪些需要标注“待核实”，列出需要补注的句子清单。", "正文", "需补出处清单"),
    ("图像", "🎯", "画面构思", "策划", "把老板要的画面拆成 3 个可选方向（主体 / 场景 / 风格），每个方向写一句能直接出图的英文提示词，说明各适合什么用途。", "任务的画面要求", "3 个方向 + 英文提示词 + 用途建议"),
    ("图像", "🎬", "出图师", "执行", "你只做一件事：把画面要求写成一段英文提示词（主体、风格、构图、配色、光线、背景），只输出提示词本身，不要解释。", "画面要求或中文描述", "一段英文提示词"),
    ("短视频", "🎬", "钩子编剧", "策划", "为 30 秒短视频写 5 个不同方向的开场前三秒（反差、提问、反常识、现场声、结果前置），每个配一句理由。", "产品/主题简介", "5 个开场方案 + 推荐"),
    ("短视频", "🎞", "分镜师", "策划", "把选定脚本拆成 8—12 个镜头，标注时长、画面内容、运镜、字幕、声音；总时长控制准确。", "脚本或开场方案", "分镜表（Markdown 表格）"),
    ("短视频", "💬", "文案", "写作", "写口播文案：短句为主，一句一个信息点，避免书面语；给出 3 个不同语气的版本（真诚/犀利/轻松）。", "主题与卖点", "3 版口播稿"),
    ("短视频", "✂️", "剪辑脚本", "执行", "把分镜翻译成剪辑指令：每段的素材类型、转场方式、字幕出现时间、音乐节点。", "分镜表", "剪辑清单"),
    ("方案提案", "🧩", "需求分析师", "研究", "把客户的模糊要求拆成：必须满足、最好满足、明确不做三类，指出其中互相矛盾的地方。", "客户原话与背景", "需求三分类清单 + 矛盾点"),
    ("方案提案", "📊", "商业画布师", "策划", "用一句话说清价值主张，再填九宫格的其余八格，每格不超过两行，避免空话。", "需求分析", "九宫格（Markdown 表格）"),
    ("方案提案", "🧮", "财务测算", "分析", "给出成本结构、单价、盈亏平衡点与三种情景（保守/基准/乐观）的测算，所有假设写清楚。", "方案要点", "测算表 + 假设清单"),
    ("方案提案", "📝", "提案撰写", "写作", "写成可直接给客户看的提案：先结论、再方案、再预算、最后风险与退出条件；每页只讲一件事。", "画布与测算", "提案正文"),
    ("方案提案", "⚔️", "反方质询", "审核", "站在客户与竞争者的角度挑刺：最可能被问倒的 8 个问题，每个给出一句回应或承认的边界。", "提案正文", "质询清单 + 回应"),
    ("代码", "🗂", "需求拆解", "策划", "把需求拆成可验收的任务清单，标注依赖关系与风险点，给出最小可跑版本的范围。", "需求描述", "任务清单 + MVP 范围"),
    ("代码", "🏗", "架构师", "设计", "给出目录结构、模块职责、数据流与关键技术选型，说明为什么不用另外两个方案。", "任务清单", "架构说明 + 目录树"),
    ("代码", "⚙️", "实现工程师", "执行", "按架构写出可运行的代码，只写必要注释；先给完整文件，再给运行方式。", "架构与任务", "完整代码 + 运行说明"),
    ("代码", "🧪", "测试", "审核", "列出该功能最该测的 10 个用例（含边界与异常），指出当前实现最可能出错的三处。", "代码", "用例清单 + 风险点"),
    ("代码", "🔎", "代码审阅", "审核", "审阅代码：找出隐藏 bug、重复逻辑、命名问题与安全隐患，按严重程度排序，每条给出改法。", "代码", "问题清单"),
    ("数据调研", "📋", "问卷设计", "策划", "按研究目的设计 10—15 题：问过去不问将来，选项穷尽，避免双管问题；标注每题想验证的假设。", "研究目的", "问卷题目 + 每题目的"),
    ("数据调研", "🧹", "数据清洗", "执行", "说明清洗规则：缺失值、重复、异常值、口径不一致分别怎么处理，并给出可执行的步骤。", "原始数据结构", "清洗规则 + 步骤"),
    ("数据调研", "📈", "数据分析", "分析", "给出该数据能回答的三个问题、不能回答的两个问题，以及对应的分析方法与结论口径。", "数据与问题", "分析方案"),
    ("数据调研", "📉", "图表叙事", "写作", "把结论写成图表说明：每张图一句话标题 + 一句结论，避免把数据复述一遍。", "分析结果", "图表标题与结论清单"),
]


FAM_EMOJI = {"论文报告": "📚", "短视频": "🎬", "方案提案": "🧩",
             "代码": "🧩", "数据调研": "📈"}

EMOJI_BY_TAG = {"策划": "🎯", "研究": "🔍", "写作": "📝", "审核": "🔎",
                "执行": "🧩", "分析": "📊", "设计": "📋", "汇总": "📈"}


def normalize_emoji(c):
    """按「族 + 角色」给每个岗位发一个能画出来的图标。"""
    rows = c.execute("SELECT id,tag,family FROM presets").fetchall()
    for r in rows:
        em = EMOJI_BY_TAG.get(r["tag"]) or FAM_EMOJI.get(r["family"]) or "📚"
        c.execute("UPDATE presets SET emoji=? WHERE id=?", (em, r["id"]))
    c.execute("UPDATE employees SET emoji=? WHERE emoji IS NULL OR emoji=''", ("🧑",))
    c.commit()


def init_db():
    c = db()
    c.executescript(SCHEMA)

    def cols(table):
        return {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()}

    if "project_id" not in cols("employees"):
        c.execute("ALTER TABLE employees ADD COLUMN project_id INT")
    if "emoji" not in cols("employees"):
        c.execute("ALTER TABLE employees ADD COLUMN emoji TEXT DEFAULT '🧑'")
    if "project_id" not in cols("tasks"):
        c.execute("ALTER TABLE tasks ADD COLUMN project_id INT")
    if "mine" not in cols("presets"):
        c.execute("ALTER TABLE presets ADD COLUMN mine INT DEFAULT 0")
    if "src_emp" not in cols("presets"):
        c.execute("ALTER TABLE presets ADD COLUMN src_emp INT")
    if "model" not in [r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()]:
        c.execute("ALTER TABLE messages ADD COLUMN model TEXT DEFAULT ''")
    if "reason" not in [r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()]:
        c.execute("ALTER TABLE messages ADD COLUMN reason TEXT DEFAULT ''")
    if "boundary" not in cols("employees"):
        c.execute("ALTER TABLE employees ADD COLUMN boundary TEXT DEFAULT ''")
    if "kind" not in cols("employees"):
        c.execute("ALTER TABLE employees ADD COLUMN kind TEXT DEFAULT 'text'")
    if "kind" not in cols("presets"):
        c.execute("ALTER TABLE presets ADD COLUMN kind TEXT DEFAULT 'text'")

    if c.execute("SELECT COUNT(*) n FROM presets").fetchone()["n"] == 0:
        c.executemany("INSERT INTO presets(family,emoji,title,tag,duty,input_spec,output_spec)"
                      " VALUES(?,?,?,?,?,?,?)", PRESET_SEED)
        c.execute("UPDATE presets SET kind='image' WHERE title IN ('出图师')")
        c.execute("UPDATE presets SET emoji='🖼' WHERE family='图像' AND title='画面构思'")
    if c.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"] == 0:
        c.execute("INSERT INTO projects(name,kind,goal,created_at) VALUES(?,?,?,?)",
                  ("我的第一个项目", "论文报告", "先拿一个真实任务试试", now()))
    c.commit()
    normalize_emoji(c)
    c.close()
    try:
        kb_backfill()
    except Exception as _e:  # noqa: BLE001
        _log("kb backfill failed: %r" % (_e,))


# ---------------------------------------------------------------- 事件


def publish(task_id, ev):
    with LOCK:
        q = SUBS.get(task_id)
    if q:
        try:
            q.put_nowait(ev)
        except Exception:
            pass


KB_KINDS = ("reply", "deliverable", "image")


def _kb_title(text):
    """取第一行像标题的文字：跳过空行、短句、以及「假设：」「注：」这类前缀行。"""
    for line in (text or "").splitlines():
        t = line.strip().lstrip("#").strip().strip("*").strip()
        if len(t) < 6:
            continue
        if re.match(r"^(假设|注|说明|备注|前言)[:：]", t):
            continue
        return t[:42]
    head = (text or "").strip().replace("\n", " ")
    return head[:42] if len(head) >= 6 else "未命名"


def kb_add(project_id, task_id, emp_id, title, content, kind="reply",
           tags="", source="auto"):
    if not (content or "").strip():
        return None
    c = db()
    cur = c.execute(
        "INSERT INTO kb(project_id,task_id,emp_id,title,content,kind,tags,source,ts)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (project_id, task_id, emp_id, title or _kb_title(content), content,
         kind, tags or "", source, now()))
    c.commit()
    kid = cur.lastrowid
    c.close()
    return kid


def kb_auto(task_id, emp_id, kind, content):
    """员工产出的正文/成品自动进知识库。太短的、纯提示性的不收。"""
    txt = (content or "").strip()
    if kind not in KB_KINDS or len(txt) < 120:
        return None
    c = db()
    t = c.execute("SELECT project_id,title FROM tasks WHERE id=?", (task_id,)).fetchone()
    e = c.execute("SELECT title FROM employees WHERE id=?", (emp_id,)).fetchone()
    c.close()
    tags = (e["title"] if e and e["title"] else "")
    c = db()
    dup = c.execute(
        "SELECT id FROM kb WHERE task_id=? AND emp_id=? AND content=?",
        (task_id, emp_id, txt)).fetchone()
    c.close()
    if dup:
        return dup["id"]
    return kb_add(t["project_id"] if t else None, task_id, emp_id,
                  _kb_title(txt), txt, kind, tags, "auto")


def kb_backfill():
    """把历史消息里够长的产出补进知识库（只补一次）。"""
    c = db()
    n = c.execute("SELECT COUNT(*) n FROM kb").fetchone()["n"]
    if n:
        c.close()
        return 0
    rows = c.execute(
        "SELECT m.id,m.task_id,m.sender,m.kind,m.content,t.project_id,e.id eid,e.title etitle"
        " FROM messages m LEFT JOIN tasks t ON t.id=m.task_id"
        " LEFT JOIN employees e ON e.title=m.sender"
        " WHERE m.kind IN ('reply','deliverable') AND LENGTH(m.content)>=120"
        " ORDER BY m.id").fetchall()
    added = 0
    for r in rows:
        c.execute(
            "INSERT INTO kb(project_id,task_id,emp_id,title,content,kind,tags,source,ts)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (r["project_id"], r["task_id"], r["eid"], _kb_title(r["content"]),
             r["content"], r["kind"], (r["etitle"] or r["sender"] or ""), "auto", now()))
        added += 1
    c.commit()
    c.close()
    return added


def kb_search(project_id, q="", emp_id=None, kind=None, tag=None, limit=80):
    c = db()
    sql = ("SELECT k.id,k.project_id,k.task_id,k.emp_id,k.title,k.kind,k.tags,k.source,k.ts,"
           " e.title AS emp_title, t.title AS task_title, LENGTH(k.content) AS n"
           " FROM kb k LEFT JOIN employees e ON e.id=k.emp_id"
           " LEFT JOIN tasks t ON t.id=k.task_id WHERE 1=1")
    args = []
    if project_id:
        sql += " AND (k.project_id=? OR k.project_id IS NULL)"
        args.append(project_id)
    if q:
        sql += " AND (k.title LIKE ? OR k.content LIKE ? OR k.tags LIKE ?)"
        args += ["%" + q + "%"] * 3
    if emp_id:
        sql += " AND k.emp_id=?"
        args.append(emp_id)
    if kind:
        sql += " AND k.kind=?"
        args.append(kind)
    if tag:
        sql += " AND k.tags LIKE ?"
        args.append("%" + tag + "%")
    sql += " ORDER BY k.id DESC LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    return rows


def kb_context(project_id, text, k=3):
    """给员工干活前取的「公司知识库摘录」：按关键词粗筛。"""
    words = [w for w in re.split(r"[\s，。、；：（）()\[\]【】]+", text or "") if len(w) >= 2][:6]
    if not words:
        return ""
    seen, picked = set(), []
    for w in words:
        for r in kb_search(project_id, q=w, limit=2):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            picked.append(r)
            if len(picked) >= k:
                break
        if len(picked) >= k:
            break
    if not picked:
        return ""
    c = db()
    out = []
    for r in picked:
        row = c.execute("SELECT content FROM kb WHERE id=?", (r["id"],)).fetchone()
        if row:
            out.append("· [" + (r["emp_title"] or "同事") + "] " + row["content"][:320])
    c.close()
    return "\n".join(out)


def log_msg(task_id, step_id, sender, receiver, kind, content,
            tin=0, tout=0, cost=0.0, ms=0, reason=""):
    c = db()
    cur = c.execute(
        "INSERT INTO messages(task_id,step_id,sender,receiver,kind,content,"
        "tokens_in,tokens_out,cost,ms,ts,model,reason)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, step_id, sender, receiver, kind, content, tin, tout, cost, ms, now(),
         getattr(_TLS, "model", ""), reason or ""))
    mid = cur.lastrowid
    c.commit()
    c.close()
    try:
        if kind in KB_KINDS:
            c2 = db()
            e = c2.execute("SELECT id FROM employees WHERE title=?", (sender,)).fetchone()
            c2.close()
            if e:
                kb_auto(task_id, e["id"], kind, content)
    except Exception as _e:
        _log("kb auto failed: %r" % (_e,))
    publish(task_id, {"type": "message", "data": {
        "id": mid, "task_id": task_id, "step_id": step_id, "sender": sender,
        "receiver": receiver, "kind": kind, "content": content, "tokens_in": tin,
        "tokens_out": tout, "cost": round(cost, 4), "ms": ms, "ts": now(),
        "model": getattr(_TLS, "model", "")}})
    return mid


# ---------------------------------------------------------------- 模型


def call_model(model, messages, temperature=0.7, max_tokens=4096, deep=None):
    used = model if model in PRICES else MODEL_CHAT
    if deep is None:
        deep = DEEP_THINK and used != MODEL_DEEP
    if deep:
        used = MODEL_DEEP
    body = {"model": used, "messages": messages, "stream": False,
            "max_tokens": max(max_tokens, 2048) if used == MODEL_DEEP else max_tokens}
    if used != MODEL_DEEP:
        body["temperature"] = temperature
    data = json.dumps(body).encode("utf-8")

    def _post(mdl):
        payload_body = dict(body, model=mdl)
        req = urllib.request.Request(API_BASE, data=json.dumps(payload_body).encode("utf-8"),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + API_KEY)
        with urllib.request.urlopen(req, timeout=900) as resp:
            return json.loads(resp.read().decode("utf-8"))

    t0 = time.time()
    try:
        payload = _post(used)
    except urllib.error.HTTPError as exc:
        if used != MODEL_DEEP:
            raise
        # reasoner 不可用时回落到普通模型，不让整单失败
        used = MODEL_CHAT
        payload = _post(used)
    ms = int((time.time() - t0) * 1000)
    msg = (payload.get("choices") or [{}])[0].get("message") or {}
    text = msg.get("content") or ""
    _TLS.model = used
    _TLS.reason = msg.get("reasoning_content") or ""
    u = payload.get("usage", {}) or {}
    return (text, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)), ms)

def _img_ext(data):
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return ""


def _fetch(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ai-company/4.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def gen_image(prompt, size="1024x1024", seed=0):
    """出图：先试 a0.dev（免 key），失败再试 pollinations。返回文件名。"""
    prompt = re.sub(r"```", "", (prompt or "")).strip()[:900]
    if not prompt:
        raise RuntimeError("提示词是空的")
    try:
        w, h = [int(x) for x in str(size).lower().split("x")]
    except Exception:  # noqa: BLE001
        w, h = 1024, 1024
    aspect = "1:1"
    if w > h * 1.2:
        aspect = "16:9"
    elif h > w * 1.2:
        aspect = "9:16"

    errs = []
    data = b""
    # 通道 1：a0.dev
    try:
        u1 = "https://api.a0.dev/assets/image?text=" + urllib.parse.quote(prompt) + "&aspect=" + aspect
        data = _fetch(u1, 120)
        if not _img_ext(data):
            errs.append("a0 返回的不是图片")
            data = b""
    except Exception as exc:  # noqa: BLE001
        errs.append("a0: %s" % str(exc)[:120])

    # 通道 2：pollinations（不带 seed，带了会被判付费）
    if not data:
        try:
            q = urllib.parse.urlencode({"width": w, "height": h, "nologo": "true"})
            u2 = ("https://image.pollinations.ai/prompt/"
                  + urllib.parse.quote(prompt, safe="") + "?" + q)
            data = _fetch(u2, 180)
            if not _img_ext(data):
                errs.append("pollinations 返回的不是图片")
                data = b""
        except Exception as exc:  # noqa: BLE001
            errs.append("pollinations: %s" % str(exc)[:120])

    if not data:
        raise RuntimeError("出图失败 → " + "；".join(errs or ["未知原因"]))
    if len(data) < 2048:
        raise RuntimeError("图片太小（%d 字节），判定失败" % len(data))

    ext = _img_ext(data)
    name = "img_%s_%s.%s" % (time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:6], ext)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    with open(os.path.join(IMAGE_DIR, name), "wb") as f:
        f.write(data)
    return name


def cost_of(tin, tout):
    model = getattr(_TLS, "model", MODEL_CHAT)
    pi, po = PRICES.get(model, (PRICE_IN, PRICE_OUT))
    return tin / 1e6 * pi + tout / 1e6 * po


def is_prose(emp):
    txt = " ".join([emp["duty"] or "", emp["output_spec"] or "", emp["title"] or ""])
    if re.search(r"清单|评分|审稿|挑错|核对|检查|质询|校对", txt):
        return False
    return bool(re.search(r"正文|写成|写作|报告|文章|稿|文案", txt))


def boundary_of(emp):
    try:
        return (emp["boundary"] or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def emp_system(emp, memories):
    """岗位提示词：先把边界写死，再讲职责。越界即停，不许替别人干活。"""
    parts = [
        "你是这家公司的%s（岗位名：%s）。" % (emp["title"], emp["name"]),
        "",
        "【你的岗位边界 —— 最高优先级，违反即算失败】",
        "1. 你只交付「%s」这一件东西，别的一律不做。" % (emp["output_spec"] or "本岗位的产出物"),
        "2. 不属于你的活，哪怕你写得出来也不许写：不替别的岗位写正文、不改稿、不算价、不做别的岗位的产出。",
        "3. 上游材料不够，就列「我还缺什么」，不要自己编、不要顺手替上游补做。",
        "4. 你的每一段文字都必须服务于你的产出物；写完后自查一句：这段话是不是别的岗位该说的？是，就删。",
    ]
    bd = boundary_of(emp)
    if bd:
        parts.append("5. 这个岗位明令不做的事：" + bd)
    parts += [
        "",
        "【职责】" + (emp["duty"] or "按岗位名自行理解。"),
        "【输入】" + (emp["input_spec"] or "上游交来的材料。"),
        "【输出】" + (emp["output_spec"] or "直接给出可用的工作成果。"),
        "",
        "【遇到模糊需求时】老板的要求可能不完整。不要停工、不要反问，先按最合理的假设继续做，"
        "并在成果最前面单独一行写「假设：…」，把你的假设列清楚（最多 3 条）。宁可先交付再被纠正，也不要交白卷。",
        "",
        "【格式】只输出工作成果本身：不寒暄、不复述任务、不写「好的我来」、不解释你在干什么。中文作答。",
    ]
    if memories:
        parts.append("【你记得的事】\n" + "\n".join("- " + m for m in memories[-5:]))
    return "\n".join(parts)


OUT_OF_SCOPE_HINTS = {
    "审核": ["以下是完整的正文", "## 一、", "我重写了一版", "修改后的完整正文"],
    "校对": ["以下是完整的正文", "## 一、"],
    "研究": ["## 一、", "综上所述", "本文认为"],
}


def role_tag(emp):
    """从岗位名 + 职责里猜角色类型（employees 表没有 tag 列）。"""
    txt = (emp["title"] or "") + " " + (emp["duty"] or "")
    if any(k in txt for k in ("审核", "审稿", "校对", "挑错", "挑空话")):
        return "审核"
    if any(k in txt for k in ("研究", "检索", "要点清单", "拆题")):
        return "研究"
    return ""


def scope_warn(emp, text):
    """粗查越界：审核岗写出整篇正文、研究岗写出成稿等。返回提示语或空串。"""
    tag = role_tag(emp)
    hints = OUT_OF_SCOPE_HINTS.get(tag) or []
    if not hints:
        return ""
    body = text or ""
    hits = [h for h in hints if h in body]
    if not hits:
        return ""
    if tag in ("审核", "校对") and len(body) > 1500:
        return "疑似越界：这是审核岗，却给出了成篇正文（命中「%s」）。" % hits[0]
    if tag == "研究" and len(body) > 2500:
        return "疑似越界：这是研究岗，却写出了成稿（命中「%s」）。" % hits[0]
    return ""


def task_brief(task):
    return "任务：%s\n目标：%s\n验收标准：%s" % (
        task["title"], task["goal"] or "", task["accept"] or "能直接用")


# ---------------------------------------------------------------- 引擎


def run_task(task_id):
    c = db()
    task = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    steps = c.execute("SELECT * FROM steps WHERE task_id=? ORDER BY idx", (task_id,)).fetchall()
    emps = {r["id"]: dict(r) for r in c.execute("SELECT * FROM employees").fetchall()}
    c.close()
    if not task or not steps:
        return

    set_task(task_id, status="running")
    log_msg(task_id, None, "系统", "全体", "note",
            "开工：%s ｜ 预算 %.2f 元 ｜ 共 %d 步" % (task["title"], task["budget"], len(steps)))

    draft = ""
    best = {"text": "", "n": 0}
    spent = 0.0
    for st in steps:
        emp = emps.get(st["emp_id"])
        if not emp:
            set_step(st["id"], status="failed")
            continue

        if spent >= float(task["budget"] or 5):
            set_task(task_id, status="over_budget")
            log_msg(task_id, None, "系统", "全体", "note",
                    "已花 %.2f 元，到预算上限，停工。" % spent)
            return

        if st["needs_approval"]:
            set_step(st["id"], status="waiting")
            log_msg(task_id, st["id"], emp["title"], "老板", "approval",
                    "@老板 这一步想请你拍板：%s\n要交的东西：%s\n"
                    "回「通过」我就继续；回「驳回」我停下等你改要求。"
                    % (emp["title"], (emp.get("output_spec") or "按岗位要求交")[:220]))
            ev = threading.Event()
            APPROVALS[(task_id, st["id"])] = {"event": ev, "ok": None}
            set_task(task_id, status="waiting")
            ev.wait()
            ok = APPROVALS.pop((task_id, st["id"]), {}).get("ok")
            set_task(task_id, status="running")
            if not ok:
                set_step(st["id"], status="rejected")
                log_msg(task_id, st["id"], "老板", emp["title"], "note", "退回，先停下。")
                set_task(task_id, status="rejected")
                return
            log_msg(task_id, st["id"], "老板", emp["title"], "note", "通过，继续。")
            set_step(st["id"], status="running")
        else:
            set_step(st["id"], status="running")

        c = db()
        mems = []
        if emp["memory"]:
            mems = [r["content"] for r in c.execute(
                "SELECT content FROM memory WHERE emp_id=? ORDER BY id DESC LIMIT 5",
                (emp["id"],)).fetchall()]
        c.close()

        if task["mode"] == "parallel":
            user = task_brief(task) + "\n\n你只负责自己这一块，独立完成，不要等别人。"
        elif st["idx"] == 0:
            user = task_brief(task) + "\n\n请开始你的部分。"
        else:
            user = task_brief(task) + "\n\n上游交来的材料：\n" + draft + "\n\n请接着往下做。"
        set_step(st["id"], input_text=user)
        expanded = False
        log_msg(task_id, st["id"], "系统", emp["title"], "instruction", user)

        try:
            _kb = ""
            try:
                _kb = kb_context(task["project_id"], task["title"] + " " + user)
            except Exception:  # noqa: BLE001
                _kb = ""
            _sys = emp_system(emp, mems)
            if _kb:
                _sys += ("\n\n【公司知识库摘录 —— 里面别的同事做过的相关成果，"
                         "可以参考说法与口径，但不要照抄，也不要替他们改稿】\n" + _kb)
                log_msg(task_id, st["id"], "系统", emp["title"], "note",
                        "给这位同事翻了 %d 条公司知识库" % _kb.count("· ["))
            text, tin, tout, ms = call_model(emp["model"], [
                {"role": "system", "content": _sys},
                {"role": "user", "content": user}])
        except Exception as exc:  # noqa: BLE001
            set_step(st["id"], status="failed")
            log_msg(task_id, st["id"], "系统", emp["title"], "note",
                    "调用失败：%s" % str(exc)[:300])
            set_task(task_id, status="failed")
            return

        cc = cost_of(tin, tout)
        spent += cc
        if len(text) > best["n"]:
            best = {"text": text, "n": len(text)}
        log_msg(task_id, st["id"], emp["title"], "老板", "reply", text, tin, tout, cc, ms,
                reason=getattr(_TLS, "reason", "") or "")
        set_step(st["id"], status="done", output=text, tokens_in=tin,
                 tokens_out=tout, cost=cc, finished_at=now())
        bump_task(task_id, cc, tin, tout)

        warn = scope_warn(emp, text)
        if warn:
            log_msg(task_id, st["id"], "系统", emp["title"], "note", "⚠ " + warn)

        if (emp.get("kind") or "text") == "image":
            log_msg(task_id, st["id"], "系统", emp["title"], "note", "按提示词开始出图…")
            try:
                img = gen_image(text, task["image_size"] if "image_size" in task.keys() else "1024x1024")
                log_msg(task_id, st["id"], emp["title"], "老板", "image", img)
                set_step(st["id"], output=text + "\n\n![图](images/" + img + ")")
                text = text + "\n\n![图](images/" + img + ")"
            except Exception as exc:  # noqa: BLE001
                log_msg(task_id, st["id"], "系统", emp["title"], "note",
                        "出图失败：%s" % str(exc)[:200])

        want = 0
        m = re.search(r"(\d{3,5})\s*字", task["goal"] or "")
        if m:
            want = int(m.group(1))
        have = len(re.findall(r"[\u4e00-\u9fa5]", text))
        if want and is_prose(emp) and have < want * 0.9 and not expanded:
            expanded = True
            note = ("现在正文约 %d 字，目标 %d 字。请在不重复、不注水的前提下补足：\n"
                    "1) 每个要点补一个具体例子或数据口径；\n"
                    "2) 补一段反方观点与回应；\n"
                    "3) 结论补三条可执行建议。\n直接输出完整正文（不是补丁）。" % (have, want))
            log_msg(task_id, st["id"], "系统", emp["title"], "instruction", note)
            try:
                text2, tin2, tout2, ms2 = call_model(emp["model"], [
                    {"role": "system", "content": emp_system(emp, mems)},
                    {"role": "user", "content": user + "\n\n你上一版正文：\n" + text + "\n\n" + note},
                ], max_tokens=8000)
                c2 = cost_of(tin2, tout2)
                spent += c2
                log_msg(task_id, st["id"], emp["title"], "老板", "reply", text2,
                        tin2, tout2, c2, ms2,
                        reason=getattr(_TLS, "reason", "") or "")
                bump_task(task_id, c2, tin2, tout2)
                if len(text2) > len(text):
                    text = text2
            except Exception as exc:  # noqa: BLE001
                log_msg(task_id, st["id"], "系统", emp["title"], "note",
                        "补写失败：%s" % str(exc)[:200])
            if len(text) > best["n"]:
                best = {"text": text, "n": len(text)}
            set_step(st["id"], output=text)

        if task["mode"] == "parallel":
            draft = (draft + "\n\n---\n\n" + text) if draft else text
        else:
            draft = text

        if emp["memory"]:
            c = db()
            c.execute("INSERT INTO memory(emp_id,task_id,content,ts) VALUES(?,?,?,?)",
                      (emp["id"], task_id, "%s：%s" % (task["title"], text[:200]), now()))
            c.commit()
            c.close()

    prose = [r["text"] for r in reply_rows(task_id) if r["prose"]]
    deliver = max(prose, key=len) if prose else (best["text"] or draft)
    c = db()
    imgs = [r["content"] for r in c.execute(
        "SELECT content FROM messages WHERE task_id=? AND kind='image' ORDER BY id", (task_id,)).fetchall()]
    c.close()
    if imgs:
        deliver = deliver + "\n\n---\n\n## 配图\n" + "\n".join("![图](images/%s)" % i for i in imgs)
    reviews = [x["text"] for x in reply_rows(task_id) if "问题清单" in x["text"]]
    if reviews and deliver not in reviews:
        deliver = deliver + "\n\n---\n\n## 附：审稿意见\n" + reviews[0]
    set_task(task_id, status="done", finished_at=now())
    log_msg(task_id, None, "系统", "老板", "deliverable", deliver)
    log_msg(task_id, None, "系统", "老板", "note", "交付完成，共花 %.3f 元。" % spent)


def reply_rows(task_id):
    c = db()
    emps = {r["title"]: dict(r) for r in c.execute("SELECT * FROM employees").fetchall()}
    rows = c.execute("SELECT content,sender FROM messages WHERE task_id=? AND kind='reply' ORDER BY id",
                     (task_id,)).fetchall()
    c.close()
    out = []
    for r in rows:
        e = emps.get(r["sender"])
        out.append({"text": r["content"], "prose": bool(e and is_prose(e))})
    return out


def set_task(task_id, **kw):
    if not kw:
        return
    c = db()
    c.execute("UPDATE tasks SET %s WHERE id=?" % ",".join(k + "=?" for k in kw),
              list(kw.values()) + [task_id])
    c.commit()
    row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    c.close()
    publish(task_id, {"type": "task", "data": dict(row)})


def bump_task(task_id, cost, tin, tout):
    c = db()
    c.execute("UPDATE tasks SET total_cost=total_cost+?,total_in=total_in+?,total_out=total_out+?"
              " WHERE id=?", (cost, tin, tout, task_id))
    c.commit()
    row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    c.close()
    publish(task_id, {"type": "task", "data": dict(row)})


def set_step(step_id, **kw):
    if not kw:
        return
    c = db()
    c.execute("UPDATE steps SET %s WHERE id=?" % ",".join(k + "=?" for k in kw),
              list(kw.values()) + [step_id])
    c.commit()
    row = c.execute("SELECT s.*,e.title emp_title FROM steps s LEFT JOIN employees e ON e.id=s.emp_id"
                    " WHERE s.id=?", (step_id,)).fetchone()
    c.close()
    publish(row["task_id"], {"type": "step", "data": dict(row)})


# ---------------------------------------------------------------- HTTP


class H(BaseHTTPRequestHandler):
    server_version = "AICompany/2.0"

    def log_message(self, *a):
        pass

    def send(self, code, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- GET
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if path in ("/", "/index.html"):
            with open(UI_PATH, "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")

        if path == "/api/key":
            k = API_KEY or ""
            masked = (k[:7] + "…" + k[-4:]) if len(k) >= 14 else ("已填" if k else "")
            return self.send(200, {"has_key": bool(k), "masked": masked,
                                   "source": ("环境变量" if os.environ.get("DEEPSEEK_API_KEY") else
                                              ("key.txt" if os.path.exists(os.path.join(BASE, "key.txt")) else "无"))})

        if path == "/api/state":
            c = db()
            projects = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY id").fetchall()]
            pid = int(qs.get("pid", [0])[0] or 0) or (projects[0]["id"] if projects else 0)
            emps = [dict(r) for r in c.execute(
                "SELECT * FROM employees WHERE project_id=? ORDER BY id", (pid,)).fetchall()]
            tasks = [dict(r) for r in c.execute(
                "SELECT * FROM tasks WHERE project_id=? ORDER BY id DESC", (pid,)).fetchall()]
            for _t in tasks:
                last = c.execute(
                    "SELECT content,kind FROM messages WHERE task_id=? ORDER BY id DESC LIMIT 1",
                    (_t["id"],)).fetchone()
                _t["last_msg"] = ((last["content"] or "").replace("\n", " ")[:38]) if last else ""
                _t["grp_emoji"] = "💬"
            stats = [dict(r) for r in c.execute(
                "SELECT m.sender,COUNT(*) n,SUM(m.tokens_in) tin,SUM(m.tokens_out) tout,"
                "SUM(m.cost) cost FROM messages m JOIN tasks t ON t.id=m.task_id "
                "WHERE m.kind='reply' AND t.project_id=? GROUP BY m.sender ORDER BY cost DESC",
                (pid,)).fetchall()]
            c.close()
            return self.send(200, {"deep": DEEP_THINK, "model": MODEL_DEEP if DEEP_THINK else MODEL_CHAT,
                                   "projects": projects, "pid": pid, "employees": emps,
                                   "tasks": tasks, "stats": stats, "has_key": bool(API_KEY)})

        if path == "/api/kb":
            pid = int(qs.get("pid", ["0"])[0] or 0)
            rows = kb_search(pid,
                             q=(qs.get("q", [""])[0] or "").strip(),
                             emp_id=int(qs.get("emp", ["0"])[0] or 0) or None,
                             kind=(qs.get("kind", [""])[0] or None),
                             limit=int(qs.get("limit", ["80"])[0] or 80))
            c = db()
            n_all = c.execute("SELECT COUNT(*) n FROM kb").fetchone()["n"]
            by_kind = [dict(r) for r in c.execute(
                "SELECT kind,COUNT(*) n FROM kb GROUP BY kind").fetchall()]
            emps = [dict(r) for r in c.execute(
                "SELECT e.id,e.title,COUNT(k.id) n FROM kb k JOIN employees e ON e.id=k.emp_id"
                " GROUP BY e.id ORDER BY n DESC").fetchall()]
            c.close()
            return self.send(200, {"items": rows, "total": n_all,
                                   "by_kind": by_kind, "by_emp": emps})

        if path.startswith("/api/kb/"):
            try:
                kid = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self.send(400, {"error": "知识条目编号不对"})
            c = db()
            r = c.execute(
                "SELECT k.*, e.title emp_title, t.title task_title FROM kb k"
                " LEFT JOIN employees e ON e.id=k.emp_id"
                " LEFT JOIN tasks t ON t.id=k.task_id WHERE k.id=?", (kid,)).fetchone()
            c.close()
            if not r:
                return self.send(404, {"error": "没有这条知识"})
            return self.send(200, {"item": dict(r)})

        if path == "/api/presets":
            pid = int(qs.get("pid", [0])[0] or 0)
            c = db()
            kind = None
            if pid:
                row = c.execute("SELECT kind FROM projects WHERE id=?", (pid,)).fetchone()
                kind = row["kind"] if row else None
            rows = [dict(r) for r in c.execute("SELECT * FROM presets ORDER BY id").fetchall()]
            c.close()
            mine = [r for r in rows if r.get("mine")]
            sysrows = [r for r in rows if not r.get("mine")]
            if kind:
                sysrows.sort(key=lambda r: 0 if r["family"] == kind else 1)
            return self.send(200, {"presets": mine + sysrows, "mine": mine,
                                   "system": sysrows, "kind": kind})

        if path == "/api/dashboard":
            c = db()
            scope = qs.get("scope", ["project"])[0]
            pid = int(qs.get("pid", [0])[0] or 0)
            where = "WHERE t.project_id=?" if scope != "all" else ""
            args = (pid,) if scope != "all" else ()
            by_emp = [dict(r) for r in c.execute(
                "SELECT m.sender name, COUNT(*) n, SUM(m.tokens_in) tin, SUM(m.tokens_out) tout,"
                " SUM(m.cost) cost, AVG(m.ms) ms FROM messages m JOIN tasks t ON t.id=m.task_id "
                + where + (" AND " if where else " WHERE ") +
                " m.kind='reply' GROUP BY m.sender ORDER BY cost DESC", args).fetchall()]
            by_proj = [dict(r) for r in c.execute(
                "SELECT p.name, COUNT(DISTINCT t.id) tasks, COALESCE(SUM(t.total_cost),0) cost"
                " FROM projects p LEFT JOIN tasks t ON t.project_id=p.id GROUP BY p.id ORDER BY cost DESC"
            ).fetchall()]
            by_status = [dict(r) for r in c.execute(
                "SELECT status, COUNT(*) n FROM tasks t " + where + " GROUP BY status", args).fetchall()]
            recent = [dict(r) for r in c.execute(
                "SELECT t.id,t.title,t.status,t.total_cost,t.created_at,p.name pname FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id " + where +
                " ORDER BY t.id DESC LIMIT 10", args).fetchall()]
            chat = c.execute("SELECT COUNT(*) n, COALESCE(SUM(cost),0) cost FROM chats").fetchone()
            c.close()
            tot_cost = sum(r["cost"] or 0 for r in by_emp)
            tot_tok = sum((r["tin"] or 0) + (r["tout"] or 0) for r in by_emp)
            tot_n = sum(r["n"] for r in by_emp)
            return self.send(200, {"scope": scope, "by_emp": by_emp, "by_project": by_proj,
                                   "by_status": by_status, "recent": recent,
                                   "chat_n": chat["n"], "chat_cost": chat["cost"],
                                   "tot_cost": tot_cost, "tot_tok": tot_tok, "tot_n": tot_n})

        if path.startswith("/api/chat/"):
            eid = int(path.rstrip("/").split("/")[-1])
            c = db()
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM chats WHERE emp_id=? ORDER BY id", (eid,)).fetchall()]
            emp = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
            c.close()
            return self.send(200, {"messages": rows, "employee": dict(emp) if emp else None})


        if path.startswith("/api/tasks/") and path.endswith("/export"):
            tid = int(path.rstrip("/").split("/")[3])
            fmt = (qs.get("fmt", ["md"])[0] or "md").lower()
            name, data, mime = task_export(tid, "docx" if fmt in ("docx", "word") else "md")
            if data is None:
                return self.send(404, {"error": "no task"})
            from urllib.parse import quote
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Disposition",
                             "attachment; filename=\"export.%s\"; filename*=UTF-8''%s"
                             % ("docx" if fmt.startswith("docx") or fmt == "word" else "md",
                                quote(name)))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/pipelines":
            pid = int(qs.get("pid", [0])[0] or 0)
            c = db()
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM pipelines WHERE project_id=? OR project_id IS NULL ORDER BY id",
                (pid,)).fetchall()]
            c.close()
            for r in rows:
                try:
                    r["emp_ids"] = json.loads(r["spec"] or "[]")
                except Exception:
                    r["emp_ids"] = []
            return self.send(200, {"pipelines": rows})

        if path.startswith("/api/memory/"):
            eid = int(path.rstrip("/").split("/")[-1])
            c = db()
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM memory WHERE emp_id=? ORDER BY id DESC", (eid,)).fetchall()]
            c.close()
            return self.send(200, {"memory": rows})

        if path.startswith("/api/tasks/"):
            tid = int(path.rstrip("/").split("/")[-1])
            c = db()
            t = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            steps = [dict(r) for r in c.execute(
                "SELECT s.*,e.title emp_title,e.emoji emp_emoji FROM steps s "
                "LEFT JOIN employees e ON e.id=s.emp_id WHERE s.task_id=? ORDER BY s.idx",
                (tid,)).fetchall()]
            msgs = [dict(r) for r in c.execute(
                "SELECT * FROM messages WHERE task_id=? ORDER BY id", (tid,)).fetchall()]
            c.close()
            if not t:
                return self.send(404, {"error": "no task"})
            return self.send(200, {"task": dict(t), "steps": steps, "messages": msgs})

        if path.startswith("/api/events/"):
            tid = int(path.rstrip("/").split("/")[-1])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = Queue()
            with LOCK:
                SUBS[tid] = q
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                        chunk = "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                    except Exception:
                        chunk = ": ping\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with LOCK:
                    SUBS.pop(tid, None)
            return

        if path.startswith("/images/"):
            name = os.path.basename(path)
            fp = os.path.join(IMAGE_DIR, name)
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    data = f.read()
                low = name.lower()
                ctype = ("image/png" if low.endswith(".png") else
                         "image/webp" if low.endswith(".webp") else
                         "image/gif" if low.endswith(".gif") else "image/jpeg")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
        return self.send(404, {"error": "not found"})

    # ---------- POST
    def do_POST(self):
        global API_KEY
        path = urlparse(self.path).path
        b = self.body()

        if path.startswith("/api/tasks/") and path.endswith("/say"):
            try:
                tid = int(path.rstrip("/").split("/")[3])
            except Exception:  # noqa: BLE001
                return self.send(400, {"error": "任务号不对"})
            text = (b.get("text") or "").strip()
            if not text:
                return self.send(400, {"error": "说点什么"})
            log_msg(tid, None, "老板", "全体", "boss", text)
            acted = "chat"
            slot = None
            for (tt, sid), v in list(APPROVALS.items()):
                if tt == tid:
                    slot = (sid, v)
                    break
            if slot:
                sid, v = slot
                yes = re.search(r"通过|可以|继续|批准|同意|行|ok|OK|好", text)
                no = re.search(r"驳回|不行|重做|退回|停|不对|别", text)
                if yes and not no:
                    v["ok"] = True
                    v["event"].set()
                    acted = "approve"
                elif no:
                    v["ok"] = False
                    v["event"].set()
                    acted = "reject"
            return self.send(200, {"ok": True, "acted": acted})

        if path == "/api/kb":
            c = db()
            pid = int(b.get("project_id") or 1)
            cur = c.execute(
                "INSERT INTO kb(project_id,task_id,emp_id,title,content,kind,tags,source,ts)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (pid, b.get("task_id"), b.get("emp_id"),
                 (b.get("title") or "").strip() or _kb_title(b.get("content")),
                 (b.get("content") or "").strip(), b.get("kind") or "note",
                 (b.get("tags") or "").strip(), "manual", now()))
            c.commit()
            kid = cur.lastrowid
            c.close()
            return self.send(200, {"ok": True, "id": kid})

        if path.startswith("/api/kb/"):
            try:
                kid = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self.send(400, {"error": "知识条目编号不对"})
            if not b.get("_delete"):
                return self.send(400, {"error": "这个接口只支持删除"})
            c = db()
            c.execute("DELETE FROM kb WHERE id=?", (kid,))
            c.commit()
            c.close()
            return self.send(200, {"ok": True})

        if path == "/api/projects":
            c = db()
            cur = c.execute("INSERT INTO projects(name,kind,goal,created_at) VALUES(?,?,?,?)",
                            (b.get("name") or "新项目", b.get("kind") or "论文报告",
                             b.get("goal") or "", now()))
            c.commit()
            row = c.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone()
            c.close()
            return self.send(200, {"ok": True, "project": dict(row)})

        if path == "/api/employees":
            c = db()
            cur = c.execute(
                "INSERT INTO employees(project_id,name,title,emoji,duty,input_spec,output_spec,"
                "model,memory,created_at,kind,boundary) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (b.get("project_id"), b.get("name") or "新人", b.get("title") or "员工",
                 b.get("emoji") or "🧑", b.get("duty", ""), b.get("input_spec", ""),
                 b.get("output_spec", ""), b.get("model", "deepseek-chat"),
                 int(b.get("memory", 1)), now(), b.get("kind") or "text",
                 b.get("boundary") or ""))
            eid = cur.lastrowid
            if b.get("save_preset"):
                c.execute("INSERT INTO presets(family,emoji,title,tag,duty,input_spec,output_spec,"
                          "mine,src_emp) VALUES(?,?,?,?,?,?,?,1,?)",
                          (b.get("family") or "论文报告", b.get("emoji") or "🧑",
                           b.get("title") or "员工", b.get("tag") or "自定义",
                           b.get("duty", ""), b.get("input_spec", ""), b.get("output_spec", ""), eid))
            c.commit()
            row = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
            c.close()
            return self.send(200, {"ok": True, "employee": dict(row)})

        if path.startswith("/api/employees/"):
            eid = int(path.rstrip("/").split("/")[-1])
            c = db()
            if b.get("_delete"):
                c.execute("DELETE FROM employees WHERE id=?", (eid,))
            else:
                fields = ["name", "title", "emoji", "duty", "input_spec",
                          "output_spec", "model", "memory", "kind", "boundary"]
                sets, vals = [], []
                for f in fields:
                    if f in b:
                        sets.append(f + "=?")
                        vals.append(b[f])
                if sets:
                    c.execute("UPDATE employees SET %s WHERE id=?" % ",".join(sets), vals + [eid])
                if b.get("save_preset"):
                    row0 = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
                    if row0:
                        c.execute("INSERT INTO presets(family,emoji,title,tag,duty,input_spec,"
                                  "output_spec,mine,src_emp) VALUES(?,?,?,?,?,?,?,1,?)",
                                  (b.get("family") or "论文报告", row0["emoji"], row0["title"],
                                   "自定义", row0["duty"] or "", row0["input_spec"] or "",
                                   row0["output_spec"] or "", eid))
            c.commit()
            row = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
            c.close()
            return self.send(200, {"ok": True, "employee": dict(row) if row else None})

        if path == "/api/pipelines":
            c = db()
            cur = c.execute(
                "INSERT INTO pipelines(project_id,name,spec,created_at) VALUES(?,?,?,?)",
                (b.get("project_id"), b.get("name") or "流水线",
                 json.dumps(b.get("emp_ids") or []), now()))
            c.commit()
            c.close()
            return self.send(200, {"ok": True, "id": cur.lastrowid})

        if path.startswith("/api/pipelines/"):
            sid = int(path.rstrip("/").split("/")[-1])
            c = db()
            c.execute("DELETE FROM pipelines WHERE id=?", (sid,))
            c.commit()
            c.close()
            return self.send(200, {"ok": True})

        if path == "/api/gen_employee":
            brief = (b.get("brief") or "").strip()
            if not brief:
                return self.send(400, {"ok": False, "error": "先写一句你想让他干什么"})
            kind = b.get("kind") or "论文报告"
            meta = (
                "你是「一个人的公司」里的人力总监。老板原话：『%s』\n"
                "公司主业：%s。\n\n"
                "把老板这句话变成一个**只做这件事**的岗位。三条硬要求：\n"
                "① 岗位必须严格对应原话的动作（他说挑错，岗位就是挑错；他说写文案，岗位就是写文案），"
                "不许改成别的行业、别的职能，不许自作主张加职责；\n"
                "② duty 只写他在一段流水线里负责的那一环，并明确写出「不负责什么」；\n"
                "③ boundary 写他越界时最可能犯的 2—3 件事（分号分隔），例如替上游补做、替下游改稿。\n\n"
                "只输出 JSON，不要解释、不要代码块：\n"
                '{"title":"岗位名（≤6字，动词开头）","duty":"80-140字","input_spec":"一句话",'
                '"output_spec":"一句话，可验收","boundary":"2-3项，分号分隔"}'
            ) % (brief, kind)
            try:
                txt, tin, tout, ms = call_model("deepseek-chat", [
                    {"role": "system", "content": "你是招聘总监，只输出 JSON。"},
                    {"role": "user", "content": meta},
                ], temperature=0.35, max_tokens=900)
                m = re.search(r"\{.*\}", txt, re.S)
                obj = json.loads(m.group(0)) if m else {}
                if not obj.get("title"):
                    raise RuntimeError("模型没给出岗位名")
                return self.send(200, {"ok": True, "draft": obj})
            except Exception as exc:  # noqa: BLE001
                return self.send(500, {"ok": False, "error": str(exc)[:300]})

        if path == "/api/open_window":
            try:
                pid = int(b.get("pid") or 1)
            except Exception:  # noqa: BLE001
                pid = 1
            OPEN_REQUESTS.append(pid)
            return self.send(200, {"ok": True, "queued": pid})

        if path == "/api/show":
            if callable(SHOW_HOOK):
                try:
                    SHOW_HOOK()
                except Exception:  # noqa: BLE001
                    pass
            return self.send(200, {"ok": True})

        if path == "/api/agency/understand":
            text = (b.get("text") or "").strip()
            if not text:
                return self.send(400, {"error": "先写一句你要干什么"})
            if not API_KEY:
                return self.send(400, {"error": "没有 API key，先点右上角 🔑 Key"})
            try:
                return self.send(200, {"ok": True, "report": agency_understand(text),
                                       "model": getattr(_TLS, "model", "")})
            except Exception as exc:  # noqa: BLE001
                return self.send(500, {"ok": False, "error": str(exc)[:300]})

        if path == "/api/agency/ask":
            text = (b.get("text") or "").strip()
            answers = b.get("answers") or ""
            if not text:
                return self.send(400, {"error": "缺需求原文"})
            if not API_KEY:
                return self.send(400, {"error": "没有 API key"})
            try:
                return self.send(200, {"ok": True, "more": agency_ask(text, answers),
                                       "model": getattr(_TLS, "model", "")})
            except Exception as exc:  # noqa: BLE001
                return self.send(500, {"ok": False, "error": str(exc)[:300]})

        if path == "/api/agency/build":
            text = (b.get("text") or "").strip()
            pid = int(b.get("pid") or 1)
            if not text:
                return self.send(400, {"error": "先写一句你要干什么"})
            if not API_KEY:
                return self.send(400, {"error": "没有 API key"})
            ttype = str(b.get("task_type") or "other")
            budget = float(b.get("budget") or 2.0)
            try:
                obj, _cands = agency_draft(pid, text, b.get("answers") or "", ttype, budget)
            except Exception as exc:  # noqa: BLE001
                return self.send(500, {"ok": False, "error": str(exc)[:300]})
            team = [t for t in (obj.get("team") or []) if isinstance(t, dict)][:6]
            return self.send(200, {"ok": True, "model": getattr(_TLS, "model", ""), "draft": {
                "task": obj.get("task") or {},
                "team": team,
                "order": [i for i in (obj.get("order") or []) if isinstance(i, int)],
                "note": str(obj.get("note") or "")[:400],
                "cost_estimate": float(obj.get("cost_estimate") or 0),
                "task_type": ttype,
                "budget": budget,
            }}, )

        if path == "/api/agency/commit":
            pid = int(b.get("pid") or 1)
            try:
                out = agency_commit(pid, b)
            except Exception as exc:  # noqa: BLE001
                return self.send(500, {"ok": False, "error": str(exc)[:300]})
            return self.send(200, {"ok": True, **out})

        if path == "/api/key":
            raw = (b.get("key") or "").strip()
            fp = os.path.join(BASE, "key.txt")
            if not raw:
                # 清除
                try:
                    if os.path.exists(fp):
                        os.remove(fp)
                except Exception:
                    pass
                API_KEY = ""
                return self.send(200, {"ok": True, "has_key": False})
            m = re.search(r"sk-[A-Za-z0-9]{20,}", raw)
            if not m:
                return self.send(400, {"error": "这不像 DeepSeek 的 key（应该以 sk- 开头）"})
            key = m.group(0)
            try:
                with open(fp, "w", encoding="utf-8", newline="\n") as f:
                    f.write(key + "\n")
            except Exception as e:
                return self.send(500, {"error": "写不进 key.txt：%s" % e})
            API_KEY = key
            return self.send(200, {"ok": True, "has_key": True})

        if path == "/api/image":
            try:
                name = gen_image(b.get("prompt") or "", b.get("size") or "1024x1024",
                                 int(b.get("seed") or 0))
                return self.send(200, {"ok": True, "file": name, "url": "/images/" + name})
            except Exception as exc:  # noqa: BLE001
                return self.send(500, {"ok": False, "error": str(exc)[:300]})

        if path.startswith("/api/memory/"):
            eid = int(path.rstrip("/").split("/")[-1])
            c = db()
            items = b.get("items")
            if items is not None:
                c.execute("DELETE FROM memory WHERE emp_id=?", (eid,))
                for it in items:
                    it = (it or "").strip()
                    if it:
                        c.execute("INSERT INTO memory(emp_id,task_id,content,ts)"
                                  " VALUES(?,?,?,?)", (eid, b.get("task_id"), it, now()))
            elif b.get("del_id"):
                c.execute("DELETE FROM memory WHERE id=? AND emp_id=?", (b["del_id"], eid))
            else:
                txt = (b.get("text") or "").strip()
                if txt:
                    c.execute("INSERT INTO memory(emp_id,task_id,content,ts) VALUES(?,?,?,?)",
                              (eid, b.get("task_id"), txt, now()))
            c.commit()
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM memory WHERE emp_id=? ORDER BY id DESC", (eid,)).fetchall()]
            c.close()
            return self.send(200, {"ok": True, "memory": rows})

        if path == "/api/tasks":
            c = db()
            cur = c.execute(
                "INSERT INTO tasks(project_id,title,goal,accept,budget,mode,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (b.get("project_id"), b.get("title") or "未命名任务", b.get("goal", ""),
                 b.get("accept", ""), float(b.get("budget", 5.0)), b.get("mode", "relay"),
                 "pending", now()))
            tid = cur.lastrowid
            ai = b.get("approval_idx")
            if isinstance(ai, bool) or ai is None:
                appr = set()
            elif isinstance(ai, int):
                appr = {ai} if ai >= 0 else set()
            elif isinstance(ai, (list, tuple, set)):
                appr = {int(x) for x in ai if str(x).lstrip("-").isdigit() and int(x) >= 0}
            else:
                appr = set()
            for i, eid in enumerate(b.get("emp_ids") or []):
                c.execute("INSERT INTO steps(task_id,idx,emp_id,status,needs_approval)"
                          " VALUES(?,?,?,?,?)", (tid, i, eid, "pending", 1 if i in appr else 0))
            c.commit()
            c.close()
            return self.send(200, {"ok": True, "task_id": tid})

        if path.startswith("/api/tasks/"):
            parts = path.rstrip("/").split("/")
            tid = int(parts[3])
            if parts[-1] == "delete":
                c = db()
                c.execute("DELETE FROM messages WHERE task_id=?", (tid,))
                c.execute("DELETE FROM steps WHERE task_id=?", (tid,))
                c.execute("DELETE FROM tasks WHERE id=?", (tid,))
                c.commit()
                c.close()
                return self.send(200, {"ok": True})
            if parts[-1] == "start":
                if not API_KEY:
                    return self.send(400, {"error": "没有 API key"})
                threading.Thread(target=run_task, args=(tid,), daemon=True).start()
                return self.send(200, {"ok": True})

        if path.startswith("/api/presets/"):
            sid = int(path.rstrip("/").split("/")[-1])
            c = db()
            c.execute("DELETE FROM presets WHERE id=? AND mine=1", (sid,))
            c.commit()
            c.close()
            return self.send(200, {"ok": True})

        if path.startswith("/api/chat/"):
            eid = int(path.rstrip("/").split("/")[-1])
            text = (b.get("text") or "").strip()
            if not text:
                return self.send(400, {"error": "空消息"})
            if not API_KEY:
                return self.send(400, {"error": "没有 API key"})
            c = db()
            emp = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
            hist = [dict(r) for r in c.execute(
                "SELECT * FROM chats WHERE emp_id=? ORDER BY id DESC LIMIT 12", (eid,)).fetchall()]
            mem = [r["content"] for r in c.execute(
                "SELECT content FROM memory WHERE emp_id=? ORDER BY id DESC LIMIT 8",
                (eid,)).fetchall()][::-1]
            c.execute("INSERT INTO chats(project_id,emp_id,role,content,ts) VALUES(?,?,?,?,?)",
                      (emp["project_id"] if emp else None, eid, "boss", text, now()))
            c.commit()
            c.close()
            if not emp:
                return self.send(404, {"error": "没这个人"})
            msgs = [{"role": "system", "content": emp_system(dict(emp), mem)}]
            for h in reversed(hist):
                msgs.append({"role": "user" if h["role"] == "boss" else "assistant",
                             "content": h["content"]})
            msgs.append({"role": "user", "content": text})
            try:
                reply, tin, tout, ms = call_model(emp["model"], msgs, temperature=0.75,
                                                  max_tokens=4000)
            except Exception as e:
                return self.send(500, {"error": "模型出错：%s" % e})
            cost = cost_of(tin, tout)
            c = db()
            cur = c.execute("INSERT INTO chats(project_id,emp_id,role,content,tokens_in,tokens_out,"
                            "cost,ms,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                            (emp["project_id"], eid, "emp", reply, tin, tout, cost, ms, now()))
            c.commit()
            row = c.execute("SELECT * FROM chats WHERE id=?", (cur.lastrowid,)).fetchone()
            c.close()
            return self.send(200, {"message": dict(row)})

        if path.startswith("/api/steps/"):
            parts = path.rstrip("/").split("/")
            sid = int(parts[3])
            c = db()
            row = c.execute("SELECT task_id FROM steps WHERE id=?", (sid,)).fetchone()
            c.close()
            if not row:
                return self.send(404, {"error": "no step"})
            slot = APPROVALS.get((row["task_id"], sid))
            if slot:
                slot["ok"] = parts[-1] == "approve"
                slot["event"].set()
            return self.send(200, {"ok": True})

        return self.send(404, {"error": "not found"})


def _pick_port(start):
    import socket
    for p in range(start, start + 12):
        with socket.socket() as sk:
            if sk.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start



# ---------- 指挥中心 / HR ----------
TASK_TYPES = {
    "paper": "报告/论文", "article": "公众号/文案", "video": "短视频脚本",
    "image": "图/海报", "plan": "方案/策划", "code": "代码/脚本",
    "data": "调研/数据", "other": "其他",
}

CHIEF_SYS = """你是一家「一人公司」的指挥中心（Chief of Staff）。老板用大白话提需求，你要：
1) 先判断这是哪一类任务（task_type 只能取这些之一）：
   paper（报告/论文/课程作业/总结/字数最少的成文）、article（公众号/推文/文案/朋友圈）、
   video（短视频脚本/分镜/口播）、image（海报/logo/插画/配图）、plan（方案/策划/PPT 大纲/提案）、
   code（代码/脚本/工具/爬虫）、data（调研/数据分析/表格/找资料）、other；
   并给一句 why 说明依据（看哪些词、要什么交付物）。
2) 用老板听得懂的话复述需求（understanding ≤ 80 字）与要交的成品（deliverable）；
3) 按任务类型给一个预算建议 budget_suggest（人民币元）与理由：
   paper 3000 字≈1.2—2；article 800 字≈0.4—0.8；video 脚本≈0.6—1；image≈0.3—0.5；
   plan≈1—2；code≈1.5—3；data≈1—2.5；
4) 只问最多 3 个真会改变做法的问题，每题 2—4 个候选答案；
   不同题型要问的点不同：paper 问字数/是否要数据/引用格式；article 问调性/读者/转化；
   video 问平台/时长/是否要分镜；image 问风格/尺寸/用途；plan 问对象/篇幅/是否要预算；
   code 问运行环境/输入输出；data 问数据来源/期限。
5) 判断是否还值得再追一轮（ask_more：需求模糊、缺关键约束时为 true）。
   判定「模糊」的标准：没说清交付物形态、没说清给谁看/给谁用、没说清尺度（字数/时长/尺寸）、
   没说清能不能用外部素材。命中任意两条就把 ask_more 设为 true，并让问题直接对准缺失的那一项。
不写正文。只输出 JSON，不要代码块围栏：
{"task_type":"paper","type_label":"报告/论文","why":"...","understanding":"...","deliverable":"...",
 "budget_suggest":1.5,"budget_reason":"...","questions":[{"q":"...","options":["...","..."]}],"ask_more":true}
"""

ASK_SYS = """你是指挥中心，正在追问第二轮。基于老板的需求与已答内容，只问 1—2 个还没问清、且真的会改变交付的问题。
已经问过或能自己判断的不要再问。只输出 JSON：
{"questions":[{"q":"...","options":["...","..."]}],"ask_more":false}
"""

BUILD_SYS = """你同时扮演两个角色，只输出一个 JSON：

【指挥中心】把需求拆成一份能派的活：
task.title ≤ 18 字；task.goal 写清交付物与判断标准；task.accept 写 3—5 条可检查的验收标准。

【HR】在预算内组队：
- 预算规则：< 1 元 → 2 个岗位；1—2.5 元 → 3 个岗位；> 2.5 元 → 最多 4 个；
- 先看「候选人」清单，能复用就复用（填 reuse_emp_id 或 reuse_preset_id，其余字段可省）；
- 没有合适的就新建：role_title、duty、input_spec、output_spec、boundary；
- 按任务类型选岗：paper→研究/写作/审稿；article→素材整理/写作/润色；video→钩子/分镜/文案；
  image→画面构思/出图；plan→需求分析/结构化/审校；code→方案/实现/测试；data→检索/清洗/结论；
- duty 只写流水线上的一环，并写明「不负责什么」；boundary 写最可能越界的 2—3 件事（分号隔开）；
- 岗位之间不许职责重叠（不要同时排「审稿」和「校对」）；
- **必须有一个「核对岗」**：职责只做一件事——对照 task.accept 逐条检查交付物有没有偏离需求，
  输出「符合/不符合 + 缺哪一条 + 改哪里」，它不写正文、不改稿、不润色；
  提问：「产出是否偏离老板要求」就是它负责（role_title 可用 核对/质检/验收检查）；
- 每个岗位给 est_cost（预估花费，累加不超过预算）；
- emoji 只能从：🧭 🔍 📝 🧩 📊 📋 🎯 🎬 💬 🔎 📈 📚 🖼 里选；
- kind 填 text 或 image（出图岗才填 image）；
- order 给出执行顺序（team 的下标数组）。

只输出 JSON：
{"task": {"title": "...", "goal": "...", "accept": "..."},
 "team": [{"role_title": "...", "emoji": "📝", "kind": "text", "duty": "...",
           "input_spec": "...", "output_spec": "...", "boundary": "...",
           "reuse_emp_id": null, "reuse_preset_id": null, "est_cost": 0.3}],
 "order": [0, 1], "cost_estimate": 0.9, "note": "一句话说明分工与预算取舍"}
"""


def _json_obj(txt):
    """模型输出里抓一段 JSON；被截断时按最后一个 } 补救一次。"""
    m = re.search(r"\{.*", txt or "", re.S)
    if not m:
        raise RuntimeError("模型没给出 JSON")
    blob = m.group(0)
    try:
        return json.loads(blob)
    except Exception:  # noqa: BLE001
        pass
    end = blob.rfind("}")
    while end > 0:
        try:
            return json.loads(blob[: end + 1])
        except Exception:  # noqa: BLE001
            end = blob.rfind("}", 0, end)
    raise RuntimeError("模型给的 JSON 不完整（可能被 max_tokens 截断）")


def _qs(obj, limit=3):
    out = []
    for q in (obj.get("questions") or [])[:limit]:
        if isinstance(q, dict) and q.get("q"):
            out.append({"q": str(q.get("q"))[:200],
                        "options": [str(o)[:60] for o in (q.get("options") or [])][:4]})
    return out


def agency_candidates(pid):
    c = db()
    emps = c.execute("SELECT id,title,duty FROM employees WHERE project_id=? ORDER BY id",
                     (pid,)).fetchall()
    pres = c.execute("SELECT id,title,tag,family FROM presets"
                     " ORDER BY mine DESC, family, id").fetchall()
    c.close()
    lines = []
    for r in emps:
        lines.append({"kind": "emp", "id": r["id"], "title": r["title"],
                      "brief": (r["duty"] or "")[:60]})
    for r in pres:
        lines.append({"kind": "preset", "id": r["id"], "title": r["title"],
                      "brief": "%s · %s" % (r["family"] or "", r["tag"] or "")})
    return lines


def agency_understand(text):
    txt, _a, _b, _c = call_model("deepseek-chat",
                                 [{"role": "system", "content": CHIEF_SYS},
                                  {"role": "user", "content": "老板的需求：%s" % text}],
                                 temperature=0.3, max_tokens=900)
    obj = _json_obj(txt)
    tt = str(obj.get("task_type") or "other")
    if tt not in TASK_TYPES:
        tt = "other"
    return {
        "task_type": tt,
        "type_label": str(obj.get("type_label") or TASK_TYPES[tt])[:20],
        "why": str(obj.get("why") or "")[:200],
        "understanding": str(obj.get("understanding") or "")[:300],
        "deliverable": str(obj.get("deliverable") or "")[:200],
        "budget_suggest": max(0.2, min(5.0, float(obj.get("budget_suggest") or 1.0))),
        "budget_reason": str(obj.get("budget_reason") or "")[:200],
        "questions": _qs(obj, 3),
        "ask_more": bool(obj.get("ask_more")),
    }


def agency_ask(text, answers):
    txt, _a, _b, _c = call_model("deepseek-chat",
                                 [{"role": "system", "content": ASK_SYS},
                                  {"role": "user", "content": "需求：%s\n\n已答：%s" % (text, answers or "（无）")}],
                                 temperature=0.35, max_tokens=600)
    obj = _json_obj(txt)
    return {"questions": _qs(obj, 2), "ask_more": bool(obj.get("ask_more"))}


def agency_draft(pid, text, answers, ttype, budget):
    cands = agency_candidates(pid)
    cl = "\n".join("- %s #%s %s（%s）" % (c["kind"], c["id"], c["title"], c["brief"])
                   for c in cands[:90])
    user = ("任务类型：%s（%s）\n预算上限：%.2f 元\n\n老板的需求：%s\n\n老板对追问的回答：%s\n\n候选人清单：\n%s"
            % (ttype, TASK_TYPES.get(ttype, "其他"), budget, text,
               answers or "（没回答，按最合理默认处理）", cl))
    txt, _a, _b, _c = call_model("deepseek-chat",
                                 [{"role": "system", "content": BUILD_SYS},
                                  {"role": "user", "content": user}],
                                 temperature=0.4, max_tokens=4200)
    obj = _json_obj(txt)
    obj = _ensure_checker(obj, cands)
    return obj, cands


CHECK_WORDS = ("核对", "质检", "验收", "检查", "偏离", "校验")


def _ensure_checker(obj, cands):
    """兜底：HR 忘了排核对岗时，替他补一个（岗位库优先，没有就现场建）。"""
    team = [t for t in (obj.get("team") or []) if isinstance(t, dict)]
    for t in team:
        name = (t.get("role_title") or "")
        duty = (t.get("duty") or "")
        if any(w in name for w in CHECK_WORDS) or ("偏离" in duty):
            return obj
    pre = None
    for c in cands:
        if c["kind"] == "preset" and c.get("tag") == "审核":
            pre = c
            break
    duty = ("只做一件事：把交付物逐条对照验收标准，指出「符合哪几条、不符合哪几条、缺什么、"
            "该改哪一段」。不写正文、不改稿、不润色、不替别人下结论。")
    slot = {
        "role_title": "核对（偏离检查）",
        "emoji": "🔎",
        "kind": "text",
        "duty": duty,
        "input_spec": "交付物 + 验收标准（task.accept）",
        "output_spec": "逐条比对清单：每条验收标准 → 符合/不符合 → 证据（原文位置）→ 修改建议",
        "boundary": "不重写正文；不新增观点；不评价写作风格；不替上游补做缺失环节",
        "reuse_emp_id": None,
        "reuse_preset_id": (pre["id"] if pre else None),
        "est_cost": 0.2,
    }
    if pre:
        slot["role_title"] = "核对（%s）" % pre["title"]
        slot["duty"] = (pre.get("duty") or duty)[:200]
        slot["output_spec"] = pre.get("output_spec") or slot["output_spec"]
    team.append(slot)
    obj["team"] = team[:6]
    order = [i for i in (obj.get("order") or range(len(team)))]
    order = [i for i in order if i < len(obj["team"])]
    if len(order) < len(obj["team"]):
        order.append(len(obj["team"]) - 1)
    obj["order"] = order
    obj["note"] = ((obj.get("note") or "") +
                   " 已自动补一个「核对岗」：最后一步专门检查产出有没有偏离要求。").strip()
    return obj


def agency_commit(pid, payload):
    """把前端确认（可编辑）的草案落库：员工 + 任务 + 步骤 + 两条 note 消息。"""
    task = payload.get("task") or {}
    team = [t for t in (payload.get("team") or []) if isinstance(t, dict)][:6]
    if not team:
        raise RuntimeError("没有岗位可开工")
    cands = agency_candidates(pid)
    emp_index = {c["id"]: c for c in cands if c["kind"] == "emp"}
    pre_index = {c["id"]: c for c in cands if c["kind"] == "preset"}
    budget = float(payload.get("budget") or 2.0)
    c = db()
    made = []
    for t in team:
        r_emp = t.get("reuse_emp_id")
        r_pre = t.get("reuse_preset_id")
        if r_emp and int(r_emp) in emp_index:
            eid = int(r_emp)
            row = c.execute("SELECT duty,boundary FROM employees WHERE id=?", (eid,)).fetchone()
            made.append({"mode": "reuse", "emp_id": eid,
                         "title": emp_index[eid]["title"],
                         "duty": (row["duty"] or "")[:160] if row else "",
                         "boundary": (row["boundary"] or "")[:240] if row else ""})
            continue
        fields = None
        if r_pre and int(r_pre) in pre_index:
            row = c.execute("SELECT * FROM presets WHERE id=?", (int(r_pre),)).fetchone()
            if row:
                fields = (row["title"], row["emoji"], row["duty"], row["input_spec"],
                          row["output_spec"], (row["kind"] or "text") or "text",
                          "只做本岗清单里的事；不重写别人的正文；不替其他岗位下结论")
        if fields is None:
            fields = (str(t.get("role_title") or "新员工")[:20],
                      str(t.get("emoji") or "🧑")[:4],
                      str(t.get("duty") or "")[:600],
                      str(t.get("input_spec") or "")[:300],
                      str(t.get("output_spec") or "")[:300],
                      ("image" if t.get("kind") == "image" else "text"),
                      str(t.get("boundary") or "")[:400])
        cur = c.execute(
            "INSERT INTO employees(project_id,name,title,emoji,duty,input_spec,"
            "output_spec,model,memory,created_at,kind,boundary)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, "小" + fields[0][:2], fields[0], fields[1], fields[2], fields[3],
             fields[4], "deepseek-chat", 1, now(), fields[5], fields[6]))
        eid = cur.lastrowid
        made.append({"mode": ("preset" if r_pre else "new"), "emp_id": eid,
                     "title": fields[0], "duty": (fields[2] or "")[:160],
                     "boundary": fields[6]})
    cur = c.execute(
        "INSERT INTO tasks(project_id,title,goal,accept,budget,mode,status,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (pid, str(task.get("title") or "新任务")[:40], str(task.get("goal") or ""),
         str(task.get("accept") or ""), budget, "relay", "pending", now()))
    tid = cur.lastrowid
    order = [i for i in (payload.get("order") or [])
             if isinstance(i, int) and 0 <= i < len(made)]
    if not order:
        order = list(range(len(made)))
    for idx, i in enumerate(order):
        c.execute("INSERT INTO steps(task_id,idx,emp_id,status,needs_approval)"
                  " VALUES(?,?,?,?,?)",
                  (tid, idx, made[i]["emp_id"], "pending",
                   1 if idx == len(order) - 1 else 0))
    head = str(payload.get("understanding") or "")[:400]
    c.execute("INSERT INTO messages(task_id,step_id,sender,receiver,kind,content,"
              "tokens_in,tokens_out,cost,ms,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (tid, None, "指挥中心", "老板", "note",
               "任务类型：%s（%s）\n预算上限 %.2f 元\n\n我理解的需求：%s"
               % (payload.get("task_type") or "other",
                  TASK_TYPES.get(payload.get("task_type") or "other", "其他"),
                  budget, head), 0, 0, 0, 0, now()))
    c.execute("INSERT INTO messages(task_id,step_id,sender,receiver,kind,content,"
              "tokens_in,tokens_out,cost,ms,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (tid, None, "HR", "老板", "note",
               "组队：%s\n\n分工理由：%s"
               % ("、".join("%s（%s）" % (m["title"],
                                         {"new": "新建", "preset": "岗位库招", "reuse": "复用现成"}[m["mode"]])
                            for m in made),
                  str(payload.get("note") or "")[:300]), 0, 0, 0, 0, now()))
    c.commit()
    c.close()
    return {"task_id": tid, "staffed": made, "order": order}

def main():
    global PORT
    init_db()
    if not API_KEY:
        print("[!] no DEEPSEEK_API_KEY found (put key.txt next to me)")
    PORT = _pick_port(PORT)
    url = "http://127.0.0.1:%d" % PORT
    print("=" * 46)
    print("  XiaoFeiyu AI Company  v3")
    print("  open: " + url)
    print("  close this window to stop the server")
    print("=" * 46)
    if os.environ.get("AC_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
