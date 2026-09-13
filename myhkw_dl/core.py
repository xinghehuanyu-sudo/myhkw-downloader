"""批量下载编排：搜索规划 -> 跨来源去重 -> 时长过滤 -> 下载入库。"""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

from .api import SOURCE_NAMES, MyhkwClient, RateLimiter, Track
from .dedup import DedupStore, content_hash, logical_key, norm_text
from .probe import probe_file, probe_remote

CHUNK = 1 << 16


@dataclass
class Candidate:
    """一首“歌”的多个可下载版本（不同来源/不同上传），versions 按优先级排列。"""
    key: str
    versions: List[Track] = field(default_factory=list)
    display: str = ""


@dataclass
class Result:
    path: Optional[str]
    kind: str  # saved | short | dup | failed | exists


# ---------------------------------------------------------------- 文件名
def sanitize_filename(name: str, maxlen: int = 120) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" .")
    name = name[:maxlen].rstrip(" .")
    return name or "untitled"


def track_dest(track: Track, outdir: str, organize: str) -> str:
    folder = outdir
    if organize == "artist":
        folder = os.path.join(folder, sanitize_filename(track.artist, 60))
    elif organize == "album":
        folder = os.path.join(
            folder, sanitize_filename(f"{track.artist}《{track.album}》", 80))
    os.makedirs(folder, exist_ok=True)
    fname = sanitize_filename(f"{track.artist} - {track.title}") + ".mp3"
    return os.path.join(folder, fname)


# ---------------------------------------------------------------- 搜索规划
def dedup_tracks(tracks: Iterable[Track]) -> List[Candidate]:
    """按 规范化曲名+歌手 归并跨来源重复；同一首歌内保留发现顺序(=来源优先级)。"""
    grouped: Dict[str, Candidate] = {}
    for t in tracks:
        if not t.title or not t.mp3_sign:
            continue
        key = logical_key(t.title, t.artist)
        cand = grouped.get(key)
        if cand is None:
            cand = grouped[key] = Candidate(key=key, display=t.display())
        cand.versions.append(t)
    return list(grouped.values())


def plan_artist(client: MyhkwClient, name: str, sources: List[str],
                delay: float = 0.8, log: Optional[Callable] = None) -> List[Candidate]:
    """歌手模式：各来源按 <歌手名> 搜索，保留歌手字段匹配的结果。"""
    log = log or (lambda s: print(s, flush=True))
    n = norm_text(name)
    tracks: List[Track] = []
    for src in sources:
        found = 0
        for t in client.search(name, src, delay=delay):
            if n and n in norm_text(t.artist):
                tracks.append(t)
                found += 1
        log(f"  来源[{SOURCE_NAMES.get(src, src)}] 命中 {found} 条")
    return dedup_tracks(tracks)


def plan_album(client: MyhkwClient, album: str, sources: List[str],
               artist: Optional[str] = None, delay: float = 0.8,
               pick_best: bool = True,
               log: Optional[Callable] = None) -> List[Candidate]:
    """专辑模式：按专辑名(以及 歌手+专辑 组合词)搜索，按专辑归组。"""
    log = log or (lambda s: print(s, flush=True))
    na = norm_text(album)
    nartist = norm_text(artist) if artist else ""
    queries = [album] + ([f"{artist} {album}"] if artist else [])
    tracks: List[Track] = []
    for q in queries:
        for src in sources:
            tracks.extend(client.search(q, src, delay=delay))
    # 过滤
    matched = []
    for t in dedup_unique(tracks):
        if norm_text(t.album) != na and na not in norm_text(t.album):
            continue
        if nartist and nartist not in norm_text(t.artist):
            continue
        matched.append(t)
    if not matched:
        return []
    if pick_best:
        # 同名专辑可能对应多个歌手，取与条件吻合度最高(曲目数最多)的一组
        groups: Dict[str, List[Track]] = {}
        for t in matched:
            groups.setdefault(logical_key(album, t.artist), []).append(t)
        best = max(groups.values(), key=len)
        if len(groups) > 1:
            log(f"  匹配到 {len(groups)} 个同名专辑，选取曲目最多的一组 "
                f"({len(best)} 首): {best[0].artist}")
        matched = best
    return dedup_tracks(matched)


def dedup_unique(tracks: Iterable[Track]) -> List[Track]:
    """同一来源内完全重复的 song_id 去重(组合词搜索会命中同一条)。"""
    seen = set()
    out = []
    for t in tracks:
        sig = (t.source, str(t.song_id))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(t)
    return out


def plan_playlist(client: MyhkwClient, playlist_id: str, sources: List[str],
                  delay: float = 0.8) -> List[Candidate]:
    tracks: List[Track] = []
    for src in sources:
        tracks.extend(client.search(playlist_id, src, playlist=True, delay=delay))
    return dedup_tracks(tracks)


def plan_keyword(client: MyhkwClient, keyword: str, sources: List[str],
                 delay: float = 0.8) -> List[Candidate]:
    tracks: List[Track] = []
    for src in sources:
        tracks.extend(client.search(keyword, src, delay=delay))
    return dedup_tracks(tracks)


# ---------------------------------------------------------------- 下载
class DownloadManager:
    def __init__(self, client: MyhkwClient, outdir: str, store: DedupStore,
                 *, ffprobe: Optional[str] = None, min_dur: float = 60.0,
                 with_lrc: bool = False, organize: str = "flat",
                 limiter: Optional[RateLimiter] = None,
                 client_factory: Optional[Callable[[], MyhkwClient]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self._client = client
        self._factory = client_factory
        self._local = threading.local()
        self.outdir = outdir
        self.store = store
        self.ffprobe = ffprobe
        self.min_dur = min_dur
        self.with_lrc = with_lrc
        self.organize = organize
        self.limiter = limiter or RateLimiter(0.5)
        self.log = log or (lambda s: print(s, flush=True))
        self._lock = threading.Lock()
        os.makedirs(outdir, exist_ok=True)

    @property
    def client(self) -> MyhkwClient:
        """workers>1 时每线程独立一个 requests.Session。"""
        if not self._factory:
            return self._client
        c = getattr(self._local, "client", None)
        if c is None:
            c = self._local.client = self._factory()
        return c

    # -------- 单个版本 --------
    def _stream_to(self, track: Track, part: str) -> int:
        resume = os.path.getsize(part) if os.path.exists(part) else 0
        resp, expected, offset = self.client.open_stream(track, resume)
        mode = "ab" if (offset and offset == resume) else "wb"
        written = 0
        try:
            with open(part, mode) as f:
                for chunk in resp.iter_content(CHUNK):
                    f.write(chunk)
                    written += len(chunk)
        finally:
            resp.close()
        total = (offset if mode == "ab" else 0) + written
        if expected is not None and total < expected:
            raise IOError(f"下载不完整 {total}/{expected} 字节")
        return total

    def _download_version(self, track: Track, dest: str) -> Result:
        part = dest + ".part"
        # 1) 远程探测时长，避免浪费带宽下载短音频
        if self.ffprobe and self.min_dur > 0:
            self.limiter.wait()
            d = probe_remote(self.client.audio_url(track), self.ffprobe)
            if d is not None:
                track.duration = d
                if d < self.min_dur - 0.5:
                    self.log(f"    跳过 [时长 {d:,.0f}s < {self.min_dur:.0f}s] "
                             f"{track.display()}")
                    return Result(None, "short")
        # 2) 下载（失败保留 .part 便于断点续传）
        self._stream_to(track, part)
        # 3) 本地复检时长（远程探测失败/无 ffprobe 时兜底）
        if track.duration is None and self.ffprobe and self.min_dur > 0:
            d = probe_file(part, self.ffprobe)
            if d is not None:
                track.duration = d
                if d < self.min_dur - 0.5:
                    os.remove(part)
                    self.log(f"    跳过 [下载后检出时长 {d:,.0f}s] "
                             f"{track.display()}")
                    return Result(None, "short")
        # 4) 内容哈希去重（不同曲名但同文件/同歌不同 ID 的情况）
        digest = content_hash(part)
        if self.store.has_content(digest):
            os.remove(part)
            self.log(f"    跳过 [内容与库中某文件完全相同] {track.display()}")
            return Result(None, "dup")
        os.replace(part, dest)
        return self._finish(track, dest, digest)

    def _finish(self, track: Track, dest: str, digest: str) -> Result:
        if self.with_lrc and track.lrc_sign:
            self.limiter.wait()
            try:
                text = self.client.lyrics(track)
            except Exception as exc:  # noqa: BLE001
                self.log(f"    歌词获取失败: {exc}")
                text = None
            if text:
                with open(os.path.splitext(dest)[0] + ".lrc", "w",
                          encoding="utf-8") as f:
                    f.write(text)
        return Result(dest, "saved")

    # -------- 一首歌（多版本） --------
    def process(self, cand: Candidate) -> Result:
        if self.store.has_logical(cand.key):
            self.log(f"[已入库] {cand.display}")
            return Result(None, "exists")
        last: Optional[Result] = None
        for track in cand.versions:
            dest = track_dest(track, self.outdir, self.organize)
            if os.path.exists(dest):
                self.log(f"[文件已存在] {os.path.basename(dest)}")
                self.store.add(cand.key, content_hash(dest), dest,
                               _meta(track))
                return Result(dest, "saved")
            try:
                res = self._download_version(track, dest)
            except Exception as exc:  # noqa: BLE001
                self.log(f"    失败[{track.source}] {exc}，尝试其他版本…")
                continue
            if res.kind == "saved":
                meta = _meta(track)
                self.store.add(cand.key, content_hash(res.path), res.path, meta)
                size = os.path.getsize(res.path) / 1048576
                dur = (f"{track.duration:,.0f}s" if track.duration else "?")
                self.log(f"[下载完成] {os.path.basename(res.path)} "
                         f"({size:.1f}MB, {dur})")
                return res
            # short / dup: 其他来源可能有完整且未重复的版本，继续尝试
            last = res
        if last is not None:
            return last
        self.log(f"[失败] 所有版本均未成功: {cand.display}")
        return Result(None, "failed")

    # -------- 批量 --------
    def run(self, candidates: List[Candidate], workers: int = 1,
            progress: Optional[Callable[[int, int, Optional["Result"]], None]] = None,
            ) -> Dict[str, int]:
        stats = {"saved": 0, "exists": 0, "short": 0, "dup": 0, "failed": 0}
        total = len(candidates)
        done = 0
        lock = threading.Lock()

        def wrapped(cand: Candidate) -> Result:
            nonlocal done
            res = self.process(cand)
            with lock:
                done += 1
                if progress:
                    progress(done, total, res)
            return res

        if workers <= 1:
            results = [wrapped(c) for c in candidates]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(wrapped, candidates))
        for r in results:
            stats[r.kind] = stats.get(r.kind, 0) + 1
        return stats


def _meta(track: Track) -> dict:
    return {"title": track.title, "artist": track.artist,
            "album": track.album, "source": track.source,
            "duration": track.duration}
