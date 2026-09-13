"""Flask 应用与路由。"""

from __future__ import annotations

import os
import webbrowser
from typing import Optional

from flask import (Flask, abort, jsonify, request, send_file,
                   send_from_directory)

from ..api import SOURCES
from ..probe import ffprobe_exe
from . import service

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def create_app(outdir: str, base: str, cookie: Optional[str] = None,
               account: Optional[str] = None) -> Flask:
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
    os.makedirs(outdir, exist_ok=True)
    runner = service.TaskRunner(outdir, base, cookie=cookie, account=account)

    # ---------------- 页面 ----------------
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    # ---------------- 配置 / 状态 ----------------
    @app.get("/api/config")
    def config():
        return jsonify({
            "outdir": os.path.abspath(outdir),
            "base": base,
            "sources": list(SOURCES),
            "ffprobe": bool(ffprobe_exe()),
            "busy": runner.busy(),
        })

    # ---------------- 任务 ----------------
    @app.post("/api/preview")
    def preview():
        payload = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(runner.preview(payload))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"预览失败: {exc}"}), 502

    @app.post("/api/task")
    def start_task():
        payload = request.get_json(force=True, silent=True) or {}
        try:
            task = runner.start(payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"id": task.id})

    @app.get("/api/task/<tid>")
    def task_state(tid: str):
        task = runner.tasks.get(tid)
        if task is None:
            abort(404)
        try:
            since = int(request.args.get("since", 0))
        except ValueError:
            since = 0
        return jsonify(task.snapshot(since))

    @app.get("/api/tasks")
    def task_list():
        items = [t.snapshot(0) for t in runner.tasks.values()]
        items.sort(key=lambda x: x.get("id", ""))
        return jsonify({"tasks": items[-20:]})

    # ---------------- 曲库 ----------------
    @app.get("/api/library")
    def library():
        return jsonify(service.library_list(outdir))

    @app.post("/api/library/delete")
    def library_delete():
        payload = request.get_json(force=True, silent=True) or {}
        key = payload.get("key")
        if not key:
            return jsonify({"error": "缺少 key"}), 400
        ok = service.delete_entry(outdir, key)
        return jsonify({"deleted": ok})

    # ---------------- 音频文件（支持 Range 拖动播放） ----------------
    @app.get("/media/<path:relpath>")
    def media(relpath: str):
        root = os.path.abspath(outdir)
        full = os.path.abspath(os.path.join(root, relpath))
        if not (full == root or full.startswith(root + os.sep)):
            abort(403)
        if not os.path.isfile(full):
            abort(404)
        return send_file(full, conditional=True)

    return app


def run_web(args) -> int:
    app = create_app(args.out, args.base, cookie=args.cookie,
                     account=args.account)
    url = f"http://{args.host}:{args.port}/"
    print(f"明月浩空音乐下载器 已启动: {url}")
    print(f"下载目录: {os.path.abspath(args.out)}")
    print("按 Ctrl+C 停止")
    if not args.no_browser:
        try:
            webbrowser.open(url.replace("127.0.0.1", "localhost"))
        except Exception:  # noqa: BLE001
            pass
    app.run(host=args.host, port=args.port, debug=args.debug,
            threaded=True, use_reloader=False)
    return 0
