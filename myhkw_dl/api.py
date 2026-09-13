"""myhkw.cn（明月浩空）API 封装。

站点后台 https://myhkw.cn/admin/#/ 需要注册登录，但官方提供免注册体验控制台
https://s.myhkw.cn/ ，访问首页即自动生成匿名账号(myhkid cookie)，后台/控制台共用
同一组接口：

  GET /action/search   搜索歌曲（key=关键词, type=wy|qq|kg|kw 或 wygd|qqgd|kggd|kwgd 歌单ID）
  GET /api/url         获取/下载 MP3（song=歌曲ID, type=来源, id=账号, sign=搜索返回的签名）
  GET /api/lyrics      获取 LRC 歌词
"""

from __future__ import annotations

import threading
import time
from typing import Iterator, Optional

import requests

DEFAULT_BASE = "https://s.myhkw.cn"
SOURCES = ("wy", "qq", "kg", "kw")          # 网易 / QQ / 酷狗 / 酷我
SOURCE_NAMES = {"wy": "网易", "qq": "QQ", "kg": "酷狗", "kw": "酷我"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PAGE_LIMIT = 50


class Track:
    """一条搜索结果（某一来源上的一个可下载版本）。"""

    __slots__ = ("source", "song_id", "title", "artist", "album",
                 "mp3_sign", "lrc_sign", "duration")

    def __init__(self, source: str, song_id, title: str, artist: str,
                 album: str, mp3_sign: str, lrc_sign: str):
        self.source = source
        self.song_id = song_id
        self.title = title
        self.artist = artist
        self.album = album
        self.mp3_sign = mp3_sign
        self.lrc_sign = lrc_sign
        self.duration: Optional[float] = None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Track {self.source}:{self.song_id} {self.artist}-{self.title}>"

    def display(self) -> str:
        name = SOURCE_NAMES.get(self.source, self.source)
        return f"[{name}] {self.artist} - {self.title} 《{self.album}》"


class MyhkwClient:
    """一个轻量级客户端；线程内各自持有一个实例（requests.Session 非线程安全）。"""

    def __init__(self, base: str = DEFAULT_BASE, cookie: Optional[str] = None,
                 account: Optional[str] = None, timeout: int = 30):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": self.base + "/search.html",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        self.account = account or self._detect_account(cookie)
        if cookie:
            self._apply_cookie(cookie)
        if account:
            self.session.cookies.set("myhkid", account,
                                     domain=self._host(), path="/")

    # ---------- 账号 / 会话 ----------

    def _host(self) -> str:
        return self.base.split("//", 1)[-1].split("/", 1)[0]

    def _apply_cookie(self, cookie: str) -> None:
        """cookie 形如 "myhkid=xxx; PHPSESSID=yyy"（从已登录浏览器复制）。"""
        for part in cookie.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                self.session.cookies.set(k.strip(), v.strip(),
                                         domain=self._host(), path="/")

    def _detect_account(self, cookie: Optional[str]) -> str:
        if cookie:
            for part in cookie.split(";"):
                k, _, v = part.partition("=")
                if k.strip() == "myhkid":
                    return v.strip()
        # 免注册：访问首页自动下发 myhkid
        self.session.get(self.base + "/", timeout=self.timeout)
        acct = self.session.cookies.get("myhkid")
        if not acct:
            raise RuntimeError(
                "无法获取匿名账号 myhkid。若使用注册账号，请加 --cookie/--account")
        return acct

    # ---------- 搜索 ----------

    def search_page(self, key: str, type_: str, page: int = 1,
                    limit: int = PAGE_LIMIT) -> dict:
        resp = self.session.get(
            self.base + "/action/search",
            params={"myhkid": self.account, "key": key, "type": type_,
                    "page": page, "limit": min(limit, PAGE_LIMIT)},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def search(self, key: str, source: str, playlist: bool = False,
               max_pages: int = 10, delay: float = 0.8) -> Iterator[Track]:
        """按关键词搜索单个来源，自动翻页。

        source: wy/qq/kg/kw；playlist=True 时 key 为歌单 ID。
        """
        type_ = source + ("gd" if playlist else "")
        for page in range(1, max_pages + 1):
            data = self.search_page(key, type_, page=page)
            rows = data.get("data") or []
            for row in rows:
                yield self._row_to_track(row, type_)
            if len(rows) < PAGE_LIMIT or not rows:
                break
            time.sleep(delay)

    @staticmethod
    def _row_to_track(row: dict, type_: str) -> Track:
        return Track(
            source=row.get("type") or type_,
            song_id=row.get("song_id"),
            title=(row.get("songname") or "").strip(),
            artist=(row.get("artist_name") or "").strip(),
            album=(row.get("album_name") or "").strip(),
            mp3_sign=row.get("mp3") or "",
            lrc_sign=row.get("lyrics") or "",
        )

    # ---------- 播放地址 / 下载 ----------

    def audio_params(self, t: Track) -> dict:
        return {"song": t.song_id, "type": t.source,
                "id": self.account, "sign": t.mp3_sign}

    def audio_url(self, t: Track) -> str:
        from urllib.parse import urlencode
        return self.base + "/api/url?" + urlencode(self.audio_params(t))

    def open_stream(self, t: Track, resume_from: int = 0):
        """流式打开 MP3。返回 (response, expected_total, resume_offset)。"""
        headers = {}
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
        resp = self.session.get(
            self.base + "/api/url", params=self.audio_params(t),
            headers=headers, stream=True, timeout=(15, 180),
            allow_redirects=True,
        )
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "audio" not in ctype and "octet-stream" not in ctype:
            resp.close()
            raise IOError(f"响应不是音频: {ctype or '未知'}")
        expected = None
        if resp.status_code == 206:
            cr = resp.headers.get("Content-Range") or ""
            if "/" in cr:
                try:
                    expected = int(cr.rsplit("/", 1)[1])
                except ValueError:
                    expected = None
            offset = resume_from
        else:
            cl = resp.headers.get("Content-Length")
            expected = int(cl) if cl and cl.isdigit() else None
            offset = 0
        return resp, expected, offset

    # ---------- 歌词 ----------

    def lyrics(self, t: Track) -> Optional[str]:
        if not t.lrc_sign:
            return None
        resp = self.session.get(
            self.base + "/api/lyrics",
            params={"song": t.song_id, "type": t.source,
                    "id": "testplayer", "sign": t.lrc_sign},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            return None
        text = resp.text
        if not text or "[" not in text:
            return None
        return text


class RateLimiter:
    """全局请求节流，多个线程共用。"""

    def __init__(self, interval: float):
        self.interval = max(0.0, interval)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep = self._next - now
            self._next = max(now, self._next) + self.interval
        if sleep > 0:
            time.sleep(sleep)
