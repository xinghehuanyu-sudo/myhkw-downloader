"""命令行入口。

示例:
  python main.py artist 周杰伦 -o  downloads
  python main.py album  叶惠美 --artist 周杰伦 --organize album --lrc
  python main.py search 晴天 --limit 5
  python main.py playlist 3778678 --source wy
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from typing import List, Optional

from .api import SOURCES, MyhkwClient, RateLimiter
from .core import (Candidate, DownloadManager, plan_album, plan_artist,
                   plan_keyword, plan_playlist)
from .dedup import DedupStore
from .probe import ffprobe_exe


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="myhkw-dl",
        description="明月浩空(myhkw.cn) 音乐批量下载：按歌手/专辑，自动跳过"
                    "短音频，跨来源去重只下一个版本。")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base", default="https://s.myhkw.cn",
                        help="接口站点，默认免注册体验站 s.myhkw.cn；"
                             "若已注册账号可填 https://myhkw.cn 并配 --cookie")
    common.add_argument("--cookie", default=None,
                        help='浏览器登录后的完整 Cookie 串，如 '
                             '"myhkid=..; PHPSESSID=.."')
    common.add_argument("--account", default=None,
                        help="仅指定 myhkid 账号（代替 --cookie）")
    common.add_argument("-o", "--out", default="downloads",
                        help="下载目录(默认 ./downloads)")
    common.add_argument("-s", "--sources", nargs="+",
                        default=["wy", "qq", "kg"],
                        choices=list(SOURCES), metavar="SRC",
                        help="来源优先级(默认 wy qq kg)。kw=酷我在免费账号下无法下载，"
                             "一般不要选")
    common.add_argument("--min-duration", type=float, default=60.0,
                        help="最短时长秒数，低于该值跳过(默认 60)")
    common.add_argument("--limit", type=int, default=0,
                        help="本次最多下载 N 首(0=不限)")
    common.add_argument("--workers", type=int, default=1,
                        help="并发下载数(默认 1，建议<=3)")
    common.add_argument("--delay", type=float, default=1.0,
                        help="相邻下载/探测请求的最小间隔秒数(默认 1.0)")
    common.add_argument("--search-delay", type=float, default=0.8,
                        help="相邻搜索翻页请求间隔秒数(默认 0.8)")
    common.add_argument("--lrc", action="store_true", help="同时下载 LRC 歌词")
    common.add_argument("--organize", choices=["flat", "artist", "album"],
                        default="flat", help="目录组织方式(默认平铺)")
    common.add_argument("--dry-run", action="store_true",
                        help="只列出将要下载的歌单，不实际下载")
    common.add_argument("--force", action="store_true",
                        help="忽略已入库记录，重新下载(内容哈希去重仍生效)")
    common.add_argument("--all-albums", action="store_true",
                        help="专辑模式下下载所有歌手的全部同名专辑"
                             "(默认只取最匹配的一组)")
    common.add_argument("--ffprobe", default=None,
                        help="ffprobe 路径(默认自动查找)")
    common.add_argument("--log", default=None, help="同时把过程写入日志文件")
    sub = p.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("artist", parents=[common], help="按歌手批量下载")
    a.add_argument("name")
    b = sub.add_parser("album", parents=[common], help="按专辑批量下载")
    b.add_argument("name")
    b.add_argument("--artist", default=None, help="限定歌手，避免撞名专辑")
    c = sub.add_parser("search", parents=[common], help="按关键词搜索下载(去重)")
    c.add_argument("keyword")
    d = sub.add_parser("playlist", parents=[common], help="按歌单ID批量下载")
    d.add_argument("id")
    d.add_argument("--source", default="wy", choices=list(SOURCES))
    w = sub.add_parser("web", parents=[common], help="启动网页界面")
    w.add_argument("--host", default="127.0.0.1", help="监听地址(默认 127.0.0.1)")
    w.add_argument("--port", type=int, default=8765, help="端口(默认 8765)")
    w.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    w.add_argument("--debug", action="store_true", help="Flask 调试模式")
    return p


class Logger:
    def __init__(self, logfile: Optional[str] = None):
        self._fp = open(logfile, "a", encoding="utf-8") if logfile else None

    def __call__(self, msg: str) -> None:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            print(msg.encode("gbk", "replace").decode("gbk"), flush=True)
        if self._fp:
            self._fp.write(msg + "\n")
            self._fp.flush()

    def close(self) -> None:
        if self._fp:
            self._fp.close()


def make_candidates(args, client: MyhkwClient, log) -> List[Candidate]:
    if args.mode == "artist":
        log(f"搜索歌手: {args.name} (来源: {' '.join(args.sources)})")
        return plan_artist(client, args.name, args.sources,
                           delay=args.search_delay, log=log)
    if args.mode == "album":
        log(f"搜索专辑: {args.name}" +
            (f" / 歌手: {args.artist}" if args.artist else ""))
        return plan_album(client, args.name, args.sources, artist=args.artist,
                          delay=args.search_delay,
                          pick_best=not args.all_albums, log=log)
    if args.mode == "search":
        return plan_keyword(client, args.keyword, args.sources,
                            delay=args.search_delay)
    if args.mode == "playlist":
        return plan_playlist(client, args.id, [args.source],
                             delay=args.search_delay)
    raise SystemExit("未知模式")


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, io.UnsupportedOperation):
        pass
    args = build_parser().parse_args(argv)
    log = Logger(args.log)

    if args.mode == "web":
        from .web.app import run_web
        code = run_web(args)
        log.close()
        return code

    probe = ffprobe_exe(args.ffprobe)
    if not probe:
        log("警告: 未找到 ffprobe，无法预探测时长；"
            "将改为下载后无法复检(仍保留内容哈希去重)。安装 ffmpeg 后可启用。")
    if args.min_duration > 0 and not probe:
        log("警告: 无 ffprobe 时 “跳过 <1 分钟音频” 不可用 (--min-duration 失效)")

    try:
        client = MyhkwClient(base=args.base, cookie=args.cookie,
                             account=args.account)
    except Exception as exc:  # noqa: BLE001
        log(f"初始化客户端失败: {exc}")
        return 2
    log(f"使用账号: {client.account}")

    candidates = make_candidates(args, client, log)
    if args.limit > 0:
        candidates = candidates[:args.limit]
    if not candidates:
        log("没有匹配的曲目。")
        return 1
    log(f"\n去重后共 {len(candidates)} 首：")
    for i, c in enumerate(candidates, 1):
        vers = "/".join(sorted({v.source for v in c.versions}))
        log(f"  {i:3d}. {c.display}  (可下载来源: {vers})")

    if args.dry_run:
        return 0

    store = DedupStore(f"{args.out}/.myhkw_library.json")
    if args.force:
        store._keys.clear()

    factory = None
    if args.workers > 1:
        def factory() -> MyhkwClient:
            return MyhkwClient(base=args.base, cookie=args.cookie,
                               account=client.account)

    mgr = DownloadManager(
        client, args.out, store,
        ffprobe=probe, min_dur=args.min_duration if probe else 0.0,
        with_lrc=args.lrc, organize=args.organize,
        limiter=RateLimiter(args.delay), client_factory=factory, log=log)

    log("")
    stats = mgr.run(candidates, workers=max(1, args.workers))
    log("\n========== 结果 ==========")
    log(f"新下载: {stats.get('saved', 0)} | 已在库: {stats.get('exists', 0)} | "
        f"时长过短跳过: {stats.get('short', 0)} | 内容重复跳过: "
        f"{stats.get('dup', 0)} | 失败: {stats.get('failed', 0)}")
    log(f"音乐目录: {os.path.abspath(args.out)}")
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
