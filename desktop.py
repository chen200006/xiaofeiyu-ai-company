# -*- coding: utf-8 -*-
"""小肥鱼 AI 公司 · 桌面版
一个真正的窗口：不起黑框、不开浏览器标签页，关窗口就躲进托盘。
再次双击图标会把已经开着的窗口叫回来。
"""
import os
import sys
import socket
import threading
import time
import urllib.request

import app as core

try:
    import webview
except Exception:  # noqa: BLE001
    webview = None

try:
    import pystray
    from PIL import Image
except Exception:  # noqa: BLE001
    pystray = None

HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))

SPLASH = """<!doctype html><meta charset="utf-8">
<body style="margin:0;background:#0e1626;color:#dbe6f7;height:100vh;
 font:14px/1.7 'Microsoft YaHei',Segoe UI,sans-serif;overflow:hidden">
<div style="display:flex;height:100%;align-items:center;justify-content:center;flex-direction:column">
  <div style="font-size:44px">🐳</div>
  <div style="margin-top:12px;font-size:15px;letter-spacing:.5px">小肥鱼 AI 公司</div>
  <div style="margin-top:6px;font-size:12px;opacity:.5">正在开公司…</div>
</div></body>"""

STATE = {"win": None, "quitting": False, "port": 0}


def _log(msg):
    try:
        with open(os.path.join(HERE, "error.log"), "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:  # noqa: BLE001
        pass


# ---------- 服务器 ----------
def _free_port(start):
    for p in range(start, start + 12):
        with socket.socket() as sk:
            if sk.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def _alive(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/state?pid=1" % port, timeout=1.2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _ask_show(port):
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/show" % port, data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def serve(port, ready):
    core.init_db()
    srv = core.ThreadingHTTPServer(("127.0.0.1", port), core.H)
    ready.set()
    srv.serve_forever()


# ---------- 窗口 ----------
def show_main(*_):
    win = STATE.get("win")
    if win is None:
        return
    try:
        win.show()
        win.restore()
    except Exception:  # noqa: BLE001
        pass


def open_project_window(pid):
    """给某个项目再开一个窗口（同一个服务，不重新起进程）。"""
    url = "http://127.0.0.1:%d/?pid=%s" % (STATE["port"], pid)
    w = webview.create_window(
        "小肥鱼 AI 公司 · 项目 #%s" % pid,
        url,
        width=1380,
        height=880,
        min_size=(1080, 700),
        background_color="#f5f6f8",
        text_select=True,
    )
    return w


class Api:
    """暴露给页面：window.pywebview.api.xxx()"""

    def open_window(self, pid):
        try:
            w = open_project_window(pid or 1)
            _log("opened window pid=%s win=%r" % (pid, w))
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            import traceback
            _log("open_window failed: " + traceback.format_exc())
            return {"ok": False, "error": str(e)}

    def hide(self):
        try:
            STATE["win"].hide()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}

    def quit(self):
        STATE["quitting"] = True
        try:
            STATE["win"].destroy()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}


def build_tray():
    if pystray is None:
        return None
    icon_path = os.path.join(HERE, "icon.ico")
    if os.path.exists(icon_path):
        img = Image.open(icon_path)
    else:
        img = Image.new("RGBA", (64, 64), (30, 60, 120, 255))

    def on_show(icon, item):
        show_main()

    def on_quit(icon, item):
        STATE["quitting"] = True
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass
        win = STATE.get("win")
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("打开小肥鱼 AI 公司", on_show, default=True),
        pystray.MenuItem("退出", on_quit),
    )
    return pystray.Icon("xiaofeiyu-ai-company", img, "小肥鱼 AI 公司", menu)


def main():
    base = int(os.environ.get("AC_PORT", "8811"))

    # 已经有公司在跑？把它叫到前面来，本进程退出
    for p in range(base, base + 12):
        if _alive(p) and _ask_show(p):
            return

    port = _free_port(base)
    STATE["port"] = port
    url = "http://127.0.0.1:%d" % port

    ready = threading.Event()
    threading.Thread(target=serve, args=(port, ready), daemon=True).start()
    ready.wait(10)

    core.SHOW_HOOK = show_main

    if webview is None:
        import webbrowser
        webbrowser.open(url)
        while True:
            time.sleep(3600)

    win = webview.create_window(
        "小肥鱼 AI 公司",
        html=SPLASH,
        width=1440,
        height=920,
        min_size=(1080, 700),
        background_color="#0e1626",
        text_select=True,
        js_api=Api(),
    )
    STATE["win"] = win

    def on_closing():
        if STATE["quitting"]:
            return True
        try:
            win.hide()
        except Exception:  # noqa: BLE001
            pass
        return False  # 不真的关，躲进托盘

    try:
        win.events.closing += on_closing
    except Exception:  # noqa: BLE001
        pass

    def load_real():
        time.sleep(0.35)
        try:
            win.load_url(url)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=load_real, daemon=True).start()

    def watch_open_requests():
        while not STATE["quitting"]:
            try:
                while core.OPEN_REQUESTS:
                    pid = core.OPEN_REQUESTS.pop(0)
                    try:
                        open_project_window(pid)
                        _log("watcher opened window for project %s" % pid)
                    except Exception:  # noqa: BLE001
                        import traceback
                        _log("watcher failed: " + traceback.format_exc())
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)

    threading.Thread(target=watch_open_requests, daemon=True).start()

    tray = build_tray()
    if tray is not None:
        threading.Thread(target=lambda: tray.run(), daemon=True).start()

    webview.start(debug=bool(os.environ.get("AC_DEBUG")))

    if tray is not None:
        try:
            tray.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        with open(os.path.join(HERE, "error.log"), "a", encoding="utf-8") as fh:
            traceback.print_exc(file=fh)
        raise
