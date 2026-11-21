# -*- coding: utf-8 -*-
"""小肥鱼 AI 公司 · 桌面版
一个真正的窗口：不起黑框、不开浏览器标签页，关窗口就是关公司。
"""
import os
import sys
import threading
import time
import socket

import app as core

try:
    import webview
except Exception:  # noqa: BLE001
    webview = None


def _free_port(start):
    for p in range(start, start + 12):
        with socket.socket() as sk:
            if sk.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def serve(port, ready):
    core.init_db()
    srv = core.ThreadingHTTPServer(("127.0.0.1", port), core.H)
    ready.set()
    srv.serve_forever()


def main():
    base = int(os.environ.get("AC_PORT", "8811"))
    port = _free_port(base)
    url = "http://127.0.0.1:%d" % port
    ready = threading.Event()
    threading.Thread(target=serve, args=(port, ready), daemon=True).start()
    ready.wait(8)

    if webview is None:
        # 没有 webview：退回系统浏览器（仍然不显示黑框）
        import webbrowser
        time.sleep(0.6)
        webbrowser.open(url)
        while True:
            time.sleep(3600)

    webview.create_window(
        "小肥鱼 AI 公司",
        url,
        width=1440,
        height=920,
        min_size=(1080, 700),
        background_color="#f5f6f8",
        easy_drag=False,
        text_select=True,
    )
    webview.start(debug=bool(os.environ.get("AC_DEBUG")))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        here = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
        with open(os.path.join(here, "error.log"), "a", encoding="utf-8") as fh:
            traceback.print_exc(file=fh)
        raise
