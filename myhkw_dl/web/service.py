"""Web 服务：任务调度、曲库管理（供 Flask 路由调用）。"""

from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from typing import Dict, List, Optional

from ..api import MyhkwClient, RateLimiter
from ..core import (Candidate, DownloadManager, plan_album, plan_artist,
                    plan_keyword, plan_playlist)
from ..dedup import DedupStore
from ..probe import ffprobe_exe

LIB_NAME = ".myhkw_library.json"
MODES = ("artist", "album", "search", "playlist")


def _make_client(base: str, cookie: Optional[str],
                 account: Optional[str]) -> MyhkwClient:
    return MyhkwClient(base=base, cookie=cookie, account=account)


def _plan(mode: str, p: dict, client: MyhkwClient, log,
          search_delay: float) -> List[Candidate]:
    sources = p.get("sources") or ["wy", "qq", "kg"]
    if mode == "artist":
        return plan_artist(client, p["name"], sources,
                           delay=search_delay, log=log)
    if mode == "album":
        return plan_album(client, p["name"], sources,
                          artist=p.get("artist") or None,
                          delay=search_delay,
                          pick_best=not p.get("all_albums", False),
                          log=log)
    if mode == "search":
        return plan_keyword(client, p["keyword"], sources,
                            delay=search_delay)
    if mode == "playlist":
        return plan_playlist(client, p["id"], [p.get("source") or "wy"],
                             delay=search_delay)
    raise ValueError(f"未知模式: {mode}")


def validate_payload(p: dict) -> Optional[str]:
    mode = p.get("mode")
    if mode not in MODES:
        return "mode 必须为 artist/album/search/playlist"
    if mode == "artist" and not (p.get("name") or "").strip():
        return "请输入歌手名"
    if mode == "album" and not (p.get("name") or "").strip():
        return "请输入专辑名"
    if mode == "search" and not (p.get("keyword") or "").strip():
        return "请输入关键词"
    if mode == "playlist" and not (p.get("id") or "").strip():
        return "请输入歌单 ID"
    return None


class Task:
    def __init__(self, tid: str, payload: dict):
        self.id = tid
        self.payload = payload
        self.status = "running"        # running | done | error
        self.events: List[str] = []
        self.stats: Dict[str, int] = {}
        self.done = 0
        self.total = 0
        self.error: Optional[str] = None
        self.started = time.time()
        self.finished: Optional[float] = None
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        with self._lock:
            self.events.append(msg.rstrip("\n"))

    def set_progress(self, done: int, total: int, _res=None) -> None:
        with self._lock:
            self.done, self.total = done, total

    def set_total(self, n: int) -> None:
        with self._lock:
            self.total = n

    def finish(self, status: str, stats: Optional[dict] = None,
               error: Optional[str] = None) -> None:
        with self._lock:
            self.status = status
            self.stats = dict(stats or {})
            self.error = error
            self.finished = time.time()

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "events": self.events[since:],
                "next": len(self.events),
                "stats": dict(self.stats),
                "progress": {"done": self.done, "total": self.total},
                "error": self.error,
                "mode": self.payload.get("mode"),
                "label": _label(self.payload),
            }


def _label(p: dict) -> str:
    mode = p.get("mode")
    if mode == "artist":
        return f"歌手: {p.get('name')}"
    if mode == "album":
        art = f" / {p['artist']}" if p.get("artist") else ""
        return f"专辑: {p.get('name')}{art}"
    if mode == "search":
        return f"关键词: {p.get('keyword')}"
    if mode == "playlist":
        return f"歌单: {p.get('source', 'wy')}#{p.get('id')}"
    return mode or ""


class TaskRunner:
    """同一时刻只允许一个下载任务（对公共接口友好，也避免并发写索引冲突）。"""

    def __init__(self, outdir: str, base: str,
                 cookie: Optional[str] = None, account: Optional[str] = None):
        self.outdir = outdir
        self.base = base
        self.cookie = cookie
        self.account = account
        self.tasks: Dict[str, Task] = {}
        self._lock = threading.Lock()
        self._running: Optional[str] = None

    def busy(self) -> bool:
        with self._lock:
            return self._running is not None

    def start(self, payload: dict) -> Task:
        err = validate_payload(payload)
        if err:
            raise ValueError(err)
        with self._lock:
            if self._running:
                raise RuntimeError("已有任务正在运行，请等待完成")
            tid = uuid.uuid4().hex[:12]
            task = Task(tid, payload)
            self.tasks[tid] = task
            self._running = tid
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return task

    # ---------------- 任务执行 ----------------
    def _run(self, task: Task) -> None:
        p = task.payload
        try:
            task.log(f"== 任务开始：{_label(p)} ==")
            client = _make_client(self.base, self.cookie, self.account)
            task.log(f"账号: {client.account} @ {self.base}")
            probe = ffprobe_exe(p.get("ffprobe"))
            if not probe:
                task.log("提示: 未检测到 ffprobe，无法按时长过滤（将下载全部匹配曲目）")
            cands = _plan(p["mode"], p, client, task.log,
                          float(p.get("search_delay", 0.8)))
            limit = int(p.get("limit") or 0)
            if limit > 0:
                cands = cands[:limit]
            task.set_total(len(cands))
            task.log(f"去重后共 {len(cands)} 首待处理：")
            for i, c in enumerate(cands[:300], 1):
                t0 = c.versions[0]
                srcs = "/".join(sorted({v.source for v in c.versions}))
                task.log(f"  {i:3d}. {t0.artist} - {t0.title}"
                         f"《{t0.album or '无专辑'}》 [{srcs}]")
            if len(cands) > 300:
                task.log(f"  …（其余 {len(cands) - 300} 首省略）")
            if not cands:
                task.finish("done", {})
                return
            store = DedupStore(os.path.join(self.outdir, LIB_NAME))
            def factory() -> MyhkwClient:
                return _make_client(self.base, self.cookie,
                                    account=client.account)

            workers = max(1, int(p.get("workers", 1)))
            mgr = DownloadManager(
                client, self.outdir, store,
                ffprobe=probe,
                min_dur=float(p.get("min_duration", 60)) if probe else 0.0,
                with_lrc=bool(p.get("lrc")),
                organize=p.get("organize", "flat"),
                limiter=RateLimiter(float(p.get("delay", 1.0))),
                client_factory=factory if workers > 1 else None,
                log=task.log,
            )
            stats = mgr.run(cands, workers=workers, progress=task.set_progress)
            task.log(f"== 任务完成：{stats} ==")
            task.finish("done", stats)
        except Exception as exc:  # noqa: BLE001
            task.log(f"任务出错: {exc}")
            task.log(traceback.format_exc(limit=4))
            task.finish("error", error=str(exc))
        finally:
            with self._lock:
                if self._running == task.id:
                    self._running = None

    # ---------------- 预览（同步，不下载） ----------------
    def preview(self, payload: dict, max_rows: int = 80) -> dict:
        err = validate_payload(payload)
        if err:
            raise ValueError(err)
        logs: List[str] = []
        client = _make_client(self.base, self.cookie, self.account)
        cands = _plan(payload["mode"], payload, client, logs.append,
                      float(payload.get("search_delay", 0.5)))
        limit = int(payload.get("limit") or 0)
        total = len(cands)
        if limit > 0:
            cands = cands[:limit]
        store = DedupStore(os.path.join(self.outdir, LIB_NAME))
        rows = []
        for c in cands[:max_rows]:
            t0 = c.versions[0]
            rows.append({
                "title": t0.title,
                "artist": t0.artist,
                "album": t0.album,
                "sources": sorted({v.source for v in c.versions}),
                "versions": len(c.versions),
                "in_library": store.has_logical(c.key),
            })
        return {"total": total, "count": len(cands), "rows": rows,
                "logs": logs}


# ---------------- 曲库 ----------------
def library_list(outdir: str) -> dict:
    path = os.path.join(outdir, LIB_NAME)
    store = DedupStore(path)
    base = os.path.dirname(os.path.abspath(path))
    items = []
    for key, e in store.entries().items():
        f = os.path.join(base, e.get("file", ""))
        exists = os.path.isfile(f)
        items.append({
            "key": key,
            "title": e.get("title", ""),
            "artist": e.get("artist", ""),
            "album": e.get("album", ""),
            "source": e.get("source", ""),
            "duration": e.get("duration"),
            "file": (e.get("file") or "").replace("\\", "/"),
            "name": e.get("name") or os.path.basename(e.get("file", "")),
            "size": os.path.getsize(f) if exists else 0,
            "exists": exists,
            "lrc": os.path.isfile(os.path.splitext(f)[0] + ".lrc"),
        })
    items.sort(key=lambda x: (x["artist"], x["title"]))
    return {"dir": os.path.abspath(outdir), "count": len(items),
            "items": items}


def delete_entry(outdir: str, key: str) -> bool:
    path = os.path.join(outdir, LIB_NAME)
    store = DedupStore(path)
    e = store.entries().get(key)
    if not e:
        return False
    base = os.path.dirname(os.path.abspath(path))
    for suffix in ("", ".lrc"):
        f = os.path.join(base, e.get("file", ""))
        if suffix:
            f = os.path.splitext(f)[0] + suffix
        try:
            if os.path.isfile(f):
                os.remove(f)
        except OSError:
            pass
    store.remove(key)
    return True
