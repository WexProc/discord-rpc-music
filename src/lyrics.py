# -*- coding: utf-8 -*-
"""
Поиск синхронизированного текста песни (LRC) — порт логики WorldLyrics
из DeltaClient (LyricsService + LyricsProviders, адаптация EvoVis).

Источники: LRCLIB -> YouTube Music (youtubei) -> NetEase, затем
приблизительная синхронизация из plain-текста (6% интро / 5% аутро).
Текст качается в фоне (worker-поток), кэш *.lrc кладётся на диск.
"""
import base64
import hashlib
import hmac
import os
import queue
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
from collections import OrderedDict

import requests

HTTP_GAP = 0.30        # мин. пауза между HTTP-запросами, сек (как в DeltaClient)
SEVERE_TTL = 20_000    # мс до повтора, если источники недоступны
MISS_TTL = 180_000     # мс до повтора, если текст реально не найден
_UA = "DeltaMusicRPC/1.1 Windows"


# ----------------------------------------------------------------------------------------------------------------------
# Сопоставление (порт LyricsProviders.match/normalize/similarity)

def normalize(value: str) -> str:
    v = unicodedata.normalize("NFD", (value or "").lower().replace("ё", "е"))
    v = "".join(ch for ch in v if not unicodedata.combining(ch))
    return re.sub(r"[^\w]+", " ", v, flags=re.UNICODE).strip()


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 0.85
    first, second = set(a.split()), set(b.split())
    if not first or not second:
        return 0.0
    return 2.0 * len(first & second) / (len(first) + len(second))


def score_match(q_title: str, q_artist: str, c_title: str, c_artist: str,
                c_dur_ms: float, q_dur_ms: float) -> float:
    """Полный порт LyricsProviders.match: 0 = не совпадает."""
    title_score = similarity(q_title, c_title)
    artist_score = 1.0 if not normalize(q_artist) else similarity(q_artist, c_artist)
    if title_score < (0.85 if not normalize(q_artist) else 0.5) or artist_score < 0.5:
        return 0.0
    score = title_score * 0.6 + artist_score * 0.4
    if 1000 <= q_dur_ms < 3_600_000 and c_dur_ms > 0:
        diff = abs(c_dur_ms - q_dur_ms) / 1000.0
        if diff > 12:
            return 0.0
        score += 0.15 if diff <= 3 else -0.35
    return score


# ----------------------------------------------------------------------------------------------------------------------
# queryFor/pageTitle (порт LyricsService.queryFor)

def page_title(title: str) -> bool:
    lower = (title or "").lower()
    return ("слушать онлайн" in lower
            or lower in ("", "яндекс музыка", "yandex music", "youtube music", "spotify"))


def query_for(artist: str, title: str, source: str = ""):
    """Чистит запрос по правилам WorldLyrics (браузерные заголовки и т.п.)"""
    artist = (artist or "").strip()
    title = (title or "").strip()
    browser = bool(re.search(r"chrome|firefox|edge|opera|browser", (source or "").lower()))
    title = re.sub(r"(?i)\s*[—–-]\s*слушать онлайн.*$", "", title).strip()
    sep = title.find(" - ")
    if sep < 0 and browser and not page_title(title):
        title = title.replace(" — ", " - ").replace(" – ", " - ")
        sep = title.find(" - ")
    if sep > 0 and (browser or not artist):
        artist = title[:sep].strip()
        title = title[sep + 3:].strip()
    artist = re.sub(r"(?i)(\s*-\s*topic|vevo)$", "", artist).strip()
    title = re.sub(r"(?i)[\[(][^]\)]*(official|music video|lyric video|lyrics|audio|visualizer|\bhd\b|\b4k\b)[^]\)]*[]\)]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return artist, title


# ----------------------------------------------------------------------------------------------------------------------
# Парсер LRC и приблизительная синхронизация

def parse_lrc(text: str):
    """[(t_sec, text|None)] — None = граница инструментального пауза.
    Отображаются только строки с непустым текстом (как в WorldLyricsTest)."""
    entries = []
    if not text:
        return entries
    for raw in text.splitlines():
        m = re.match(r"^((?:\[\d+:\d+(?:[.:]\d+)?\])+)(.*)$", raw)
        if not m:
            continue
        text_part = m.group(2).strip() or None
        for mm, ss, ff in re.findall(r"\[(\d+):(\d+)(?:[.:](\d+))?\]", m.group(1)):
            frac = int(ff) / (10 ** len(ff)) if ff else 0.0
            entries.append((int(mm) * 60 + int(ss) + frac, text_part))
    entries.sort(key=lambda e: e[0])
    return entries


def stamp(ms: int) -> str:
    return f"[{ms // 60_000:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}]"


def estimate(plain: str, duration_ms: int) -> str:
    """Строки по весу длины; 6% интро / 5% аутро (порт LyricsService.estimate)."""
    lines = [ln.strip() for ln in (plain or "").splitlines()
             if ln.strip() and not ln.strip().startswith("[")]
    if not lines:
        return ""
    if duration_ms <= 0:
        duration_ms = 180_000
    start = int(duration_ms * 0.06)
    span = int(duration_ms * 0.89)
    weights = [max(6, len(l)) for l in lines]
    total = sum(weights)
    out = ["[delta-estimated:1]"]
    used = 0
    for ln, w in zip(lines, weights):
        out.append(stamp(start + span * used // total) + ln)
        used += w
    out.append(stamp(start + span))
    return "\n".join(out)


# ----------------------------------------------------------------------------------------------------------------------
# HTTP: рейт-лимиты по хостам + пауза между запросами

class Transport:
    def __init__(self):
        self.session = requests.Session()
        self.next_ok = 0.0
        self.rate = {}
        self.failed = False

    def get_json(self, url, params=None, extra_headers=None):
        return self._call(url, None, params, extra_headers)

    def post_json(self, url, body, extra_headers=None):
        return self._call(url, body, None, extra_headers)

    def _call(self, url, body, params, extra_headers=None):
        host = urllib.parse.urlsplit(url).netloc
        key_host = host.replace("www.", "")
        now = time.time()
        if now < self.rate.get(key_host, 0):
            self.failed = True
            return None
        if now < self.next_ok:
            time.sleep(self.next_ok - now)
        headers = {"User-Agent": _UA}
        if "music.163.com" in host:
            headers["Referer"] = "https://music.163.com/"
        if extra_headers:
            headers.update(extra_headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = "https://music.youtube.com"
        try:
            if body is None:
                r = self.session.get(url, params=params, headers=headers, timeout=6)
            else:
                r = self.session.post(url, json=body, headers=headers, timeout=6)
        except Exception:
            self.failed = True
            self.next_ok = time.time() + HTTP_GAP
            return None
        self.next_ok = time.time() + HTTP_GAP
        if r.status_code == 429:
            try:
                retry_after = float(r.headers.get("Retry-After") or 60)
            except (TypeError, ValueError):
                retry_after = 60.0
            self.rate[key_host] = time.time() + max(1.0, retry_after)
            self.failed = True
            return None
        if r.status_code in (400, 404):
            return None                       # промах — не ошибка
        if r.status_code >= 500:
            time.sleep(0.4)                   # как в DeltaClient: один ретрай
            try:
                r = (self.session.post(url, json=body, headers=headers, timeout=6) if body is not None
                     else self.session.get(url, params=params, headers=headers, timeout=6))
            except Exception:
                self.failed = True
                return None
        if r.status_code != 200:
            self.failed = True
            return None
        try:
            return r.json()
        except Exception:
            self.failed = True
            return None


# Яндекс-подписи (как в YandexMusicDesktop: HMAC-SHA256 -> base64)
# ключи и client-заголовок из сообщества yandex-music-api (MarshalX)
_YM_API = "https://api.music.yandex.net"
_YM_DESKTOP_KEY = "kzqU4XhfCaY6B6JTHODeq5"
_YM_DESKTOP_CLIENT = "YandexMusicDesktopAppWindows/5.95.0"


def ym_sign(track_id, key=_YM_DESKTOP_KEY):
    ts = int(time.time())
    msg = f"{track_id}{ts}".encode("utf-8")
    sig = base64.b64encode(hmac.new(key.encode(), msg, hashlib.sha256).digest()).decode("utf-8")
    return ts, sig


def ym_headers():
    return {"X-Yandex-Music-Client": _YM_DESKTOP_CLIENT}


def ym_cover_url(uri: str, size: str = "400x400"):
    if not uri:
        return ""
    full = uri if uri.startswith("http") else ("https://" + uri)
    return full.replace("%%", size)


# ----------------------------------------------------------------------------------------------------------------------
# Провайдеры: LRCLIB -> Яндекс Музыка -> YouTube Music -> NetEase

class Found:
    __slots__ = ("lrc", "plain", "provider", "score", "meta", "cover")

    def __init__(self, lrc="", plain="", provider="", score=0.0, meta=("", "", ""), cover=""):
        self.lrc = lrc
        self.plain = plain
        self.provider = provider
        self.score = score
        self.meta = meta  # (artistName, trackName, albumName) с сервера
        self.cover = cover  # URL обложки (Яндекс)

    def timed(self):
        return bool(self.lrc)


EMPTY = Found()


def walk(obj, key, owners=False, depth=0):
    """Рекурсивный сбор элементов по ключу (порт LyricsProviders.walk/collect)"""
    found = []
    if depth > 80 or obj is None:
        return found
    if isinstance(obj, list):
        for item in obj:
            found.extend(walk(item, key, owners, depth + 1))
    elif isinstance(obj, dict):
        if key in obj:
            if owners:
                found.append(obj)
            elif isinstance(obj[key], dict):
                found.append(obj[key])
        for v in obj.values():
            found.extend(walk(v, key, owners, depth + 1))
    return found


def ytext(elem) -> str:
    if not isinstance(elem, dict):
        return ""
    if isinstance(elem.get("simpleText"), str):
        return elem["simpleText"]
    return "".join(r.get("text", "") for r in elem.get("runs") or [] if isinstance(r, dict))


def parse_ytduration(text: str):
    m = re.search(r"(?<!\d)(\d+):(\d{2})(?::(\d{2}))?(?!\d)", text or "")
    if not m:
        return 0
    a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
    return (a * 60 + b) * 1000 if c is None else (a * 3600 + b * 60 + int(c)) * 1000


def ytm_body(android: bool, **props):
    client = {"clientName": "ANDROID_MUSIC" if android else "WEB_REMIX",
              "clientVersion": "7.27.52" if android else "1.20250101.01.00", "hl": "en"}
    if android:
        client.update({"androidSdkVersion": 34, "osName": "Android", "osVersion": "14"})
    body = {"context": {"client": client}}
    body.update(props)
    return body


def timed_youtube_lrc(browse_json) -> str:
    lines = []
    for holder in walk(browse_json, "lyricLine", owners=True):
        line = holder.get("lyricLine") or {}
        cue = line.get("cueRange") or {}
        try:
            start = int(float(cue.get("startTimeMilliseconds") or 0))
            end = int(float(cue.get("endTimeMilliseconds") or 0))
        except (TypeError, ValueError):
            continue
        text = (line.get("lyricLine") or "").strip()
        if end <= start or not text:
            continue
        lines.append(f"{stamp(start)}{text}\n{stamp(end)}")
    return "\n".join(lines)


def artists_from(track: dict) -> str:
    return ", ".join(a.get("name") or "" for a in (track.get("artists") or []) if isinstance(a, dict))


def _coerce_lrc(text: str):
    """Yandex отдаёт текст разными видами: LRC '[mm:ss.xx] line',
    plain или 'mm:ss.xx line'. Возвращаем (lrc-текст | plain, synced)"""
    if not text:
        return "", False
    if re.search(r"\[\d+:\d", text):
        return text, True
    timed = [ln for ln in text.splitlines() if re.match(r"^\s*\d{1,2}:\d{2}([.:]\d+)?\s", ln)]
    if len(timed) >= 3:
        out = []
        for ln in timed:
            m = re.match(r"^\s*(\d{1,2}):(\d{2})([.:](\d+))?\s+(.*)$", ln)
            if m:
                mm, ss, frac, tail = m.group(1), m.group(2), m.group(4), m.group(5)
                f = f".{frac}" if frac else ".0"
                out.append(f"[{int(mm):02d}:{ss}{f}]{tail}")
        return "\n".join(out), True
    return text, False


def _extract_lyrics_text(resp):
    """Ищем в любом ответе Яндекс API первое поле с текстом лирики,
    попутно поднимая флаг наличия таймкодов (hasTimecodes / SYNC)."""
    flag = {"synced": False}

    def better(obj):
        for k in ("hasTimecodes", "type", "descriptionType", "lyricsType"):
            val = obj.get(k)
            if (isinstance(val, str) and "sync" in val.lower()) or val is True:
                flag["synced"] = True

    def scan(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and k.lower() in ("lyrics", "fulllyrics", "lyric", "text") \
                        and len(v.strip()) > 20:
                    better(obj)
                    return v
            for v in obj.values():
                r = scan(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = scan(item)
                if r:
                    return r
        return ""

    text = scan(resp if isinstance(resp, (dict, list)) else {})
    if not text:
        return "", False
    return _coerce_lrc(text)


class Providers:
    def __init__(self, transport: Transport, yandex_token: str = ""):
        self.t = transport
        self.yandex_token = (yandex_token or "").strip()
        self.plain = Found()

    def _chain(self, artist, title, duration_ms):
        yield ("LRCLIB", self.lrclib)
        if self.yandex_token:
            yield ("Яндекс Музыка", self.yandex)
        yield ("YouTube Music", self.youtube)
        yield ("NetEase", self.netease)

    def find(self, artist, title, duration_ms) -> Found:
        artist, title = query_for(artist, title)
        cover_best = ""
        result = None
        for _name, fn in self._chain(artist, title, duration_ms):
            try:
                res = fn(artist, title, duration_ms)
            except Exception:
                self.t.failed = True
                continue
            if res.cover and not cover_best:
                cover_best = res.cover
            if res.timed():
                result = res
                break
            if res.plain:  # plain-фоллбеки копим для приблизительной синхронизации
                self._remember(res)
        if result is None:
            result = self.plain
        if cover_best and not result.cover:
            result.cover = cover_best
        return result

    # --- LRCLIB -----------------------------------------------------------
    def lrclib(self, artist, title, duration_ms) -> Found:
        if normalize(artist):
            params = {"artist_name": artist, "track_name": title}
            if 1000 <= duration_ms <= 3_600_000:
                params["duration"] = int(duration_ms // 1000)
            exact = self.t.get_json("https://lrclib.net/api/get", params=params)
            f = self._from_lrc(exact, artist, title, duration_ms)
            if f.timed():
                return f
        tries = []
        if normalize(artist):
            tries.append({"track_name": title, "artist_name": artist})
        tries.append({"track_name": title})
        tries.append({"q": (artist + " " + title).strip()})
        for params in tries:
            data = self.t.get_json("https://lrclib.net/api/search", params=params)
            if not isinstance(data, list):
                continue
            best = Found()
            for item in data:
                if not isinstance(item, dict):
                    continue
                f = self._from_lrc(item, artist, title, duration_ms)
                if f.timed() and f.score > best.score:
                    best = f
            if best.timed():
                return best
        return Found()

    def _from_lrc(self, song, artist, title, duration_ms) -> Found:
        if not isinstance(song, dict):
            return Found()
        score = self._score(title, artist, song.get("trackName") or "", song.get("artistName") or "",
                            (song.get("duration") or 0) * 1000, duration_ms)
        if score < 0.6:  # даже api/get может вернуть не ту версию при отсутствии duration
            return Found()
        meta = (song.get("artistName") or "", song.get("trackName") or "", song.get("albumName") or "")
        f = Found(song.get("syncedLyrics") or "", song.get("plainLyrics") or "", "LRCLIB", score, meta)
        self._remember(f)
        return f

    # --- YouTube Music ------------------------------------------------------
    def youtube(self, artist, title, duration_ms) -> Found:
        body = ytm_body(False, query=(artist + " " + title).strip(),
                        params="EgWKAQIIAWoKEAoQCRADEAQQBQ==")
        search = self.t.post_json("https://music.youtube.com/youtubei/v1/search?prettyPrint=false", body)
        rows = walk(search, "musicResponsiveListItemRenderer")
        best_id, best_score, best_meta = "", 0.6, ("", "", "")
        for row in rows[:6]:
            columns = []
            for holder in walk(row, "musicResponsiveListItemFlexColumnRenderer"):
                if isinstance(holder.get("text"), dict):
                    columns.append(ytext(holder["text"]))
            if not columns:
                continue
            subtitle = columns[1] if len(columns) > 1 else ""
            col_artist = subtitle.split(" • ")[0]
            duration = parse_ytduration(" ".join(columns))
            score = self._score(title, artist, columns[0], col_artist, duration, duration_ms)
            if score <= best_score:
                continue
            video_id = ""
            for r in walk(row, "playlistItemData"):
                video_id = r.get("videoId") or ""
                if video_id:
                    break
            if not video_id:
                for endpoint in walk(row, "watchEndpoint"):
                    video_id = endpoint.get("videoId") or ""
                    if video_id:
                        break
            if video_id:
                best_score, best_id, best_meta = score, video_id, (col_artist, columns[0], "")
        if not best_id:
            return Found()
        for android in (True, False):
            body = ytm_body(android, videoId=best_id)
            nxt = self.t.post_json("https://music.youtube.com/youtubei/v1/next?prettyPrint=false", body)
            browse_id = ""
            for endpoint in walk(nxt, "browseEndpoint"):
                bid = endpoint.get("browseId") or ""
                if isinstance(bid, str) and bid.startswith("MPLY"):
                    browse_id = bid
                    break
            if not browse_id:
                continue
            body = ytm_body(android, browseId=browse_id)
            browse = self.t.post_json("https://music.youtube.com/youtubei/v1/browse?prettyPrint=false", body)
            lrc = timed_youtube_lrc(browse)
            if lrc:
                return Found(lrc, "", "YouTube Music", best_score, best_meta)
            for shelf in walk(browse, "musicDescriptionShelfRenderer"):
                if isinstance(shelf.get("description"), dict):
                    plain = ytext(shelf["description"])
                    if plain:
                        self._remember(Found("", plain, "YouTube Music", best_score, best_meta))
        return Found()

    # --- Яндекс Музыка (нужен OAuth-токен в config.json: "yandex_token") ---
    YMX_API = _YM_API

    def _ymx(self, url, params=None):
        hdr = {"Authorization": f"OAuth {self.yandex_token}"}
        hdr.update(ym_headers())
        return self.t.get_json(self.YMX_API + url, params=params, extra_headers=hdr)

    def yandex(self, artist, title, duration_ms) -> Found:
        if not self.yandex_token:
            return Found()
        q = self._ymx("/search", params={"text": (artist + " " + title).strip(),
                                         "type": "track", "page": 0, "pageSize": 8})
        if not isinstance(q, dict):
            return Found()
        res = q.get("result") or {}
        container = res.get("tracks")
        if isinstance(container, dict):
            # новый формат: {"total":…, "perPage":…, "order":…, "results":[…]}
            tracks = container.get("results") or []
        else:
            tracks = container or []
        best, best_id = None, None
        for tr in tracks:
            if not isinstance(tr, dict):
                continue
            names = ", ".join(a.get("name") or "" for a in (tr.get("artists") or []) if isinstance(a, dict))
            score = self._score(title, artist, tr.get("title") or "", names,
                                tr.get("durationMs") or 0, duration_ms)
            if best is None or score > best[0]:
                best = (score, names, tr)
        if best is None or not best[0]:
            return Found()
        best_score, names, tr = best
        best_id = tr.get("id")
        cover = ym_cover_url(tr.get("coverUri") or "")
        if not cover:
            albums = tr.get("albums") or []
            if albums and isinstance(albums[0], dict):
                cover = ym_cover_url(albums[0].get("coverUri") or "")
        album = ""
        albums = tr.get("albums") or []
        if albums and isinstance(albums[0], dict):
            album = albums[0].get("title") or ""
        meta = (artists_from(tr) or names, tr.get("title") or "", album)

        found = Found(cover=cover, provider="Яндекс Музыка", score=best_score, meta=meta)

        # Текст: подпись HMAC-SHA256(trackId + timeStamp) ключом десктоп-клиента
        try:
            ts, sig = ym_sign(best_id)
            params = {"format": "LRC", "timeStamp": ts, "sign": sig}
            ly = self._ymx(f"/tracks/{best_id}/lyrics", params=params)
            d = (ly or {}).get("result") or {}
            durl = d.get("downloadUrl") or d.get("download_url") or ""
            if durl:
                r3 = requests.get(durl, headers=ym_headers(), timeout=8)
                if r3.ok and r3.text.strip():
                    text = "\n".join(l.strip() for l in r3.text.splitlines())
                    found.lrc = text
                elif not r3.ok:
                    self.t.failed = True
        except Exception:
            self.t.failed = True
        if not found.lrc:
            found.lrc = ""
        return found

    # --- NetEase --------------------------------------------------------------
    def netease(self, artist, title, duration_ms) -> Found:
        q = urllib.parse.quote((artist + " " + title).strip())
        response = self.t.get_json(f"https://music.163.com/api/cloudsearch/pc?type=1&limit=10&offset=0&s={q}")
        if not isinstance(response, dict):
            return Found()
        songs = (response.get("result") or {}).get("songs") or []
        scored = []
        for song in songs:
            if not isinstance(song, dict):
                continue
            arr = song.get("ar") or song.get("artists") or []
            artists = ", ".join(a.get("name") or "" for a in arr if isinstance(a, dict))
            dur = song.get("dt") or song.get("duration") or 0
            score = self._score(title, artist, song.get("name") or "", artists, float(dur), duration_ms)
            if score >= 0.6:
                scored.append((score, song))
        scored.sort(key=lambda x: x[0], reverse=True)
        for score, song in scored[:3]:
            ly = self.t.get_json(
                f"https://music.163.com/api/song/lyric?os=pc&lv=-1&kv=-1&tv=-1&id={song.get('id')}")
            lrc = ((ly or {}).get("lrc") or {}).get("lyric") or ""
            if lrc and any(t for _, t in parse_lrc(lrc)):
                meta = (artists_from_net(song), song.get("name") or "", "")
                return Found(lrc, "", "NetEase", score, meta)
        return Found()

    # --- общее ------------------------------------------------------------------
    def _remember(self, f: Found):
        if f.plain and f.score > self.plain.score:
            self.plain = f

    @staticmethod
    def _score(q_title, q_artist, cand_title, cand_artist, cand_dur_ms, q_dur_ms):
        return score_match(q_title, q_artist, cand_title, cand_artist, cand_dur_ms, q_dur_ms)


def artists_from_net(song):
    arr = song.get("ar") or song.get("artists") or []
    return ", ".join(a.get("name") or "" for a in arr if isinstance(a, dict))


# ----------------------------------------------------------------------------------------------------------------------
# Сервис: фоновая очередь + кэш + диск (порт LyricsService)

class Result:
    __slots__ = ("lyrics", "meta", "status", "retry_at", "estimated", "provider", "cover")

    def __init__(self, lyrics=None, meta=("", "", ""), status="", retry_at=0.0, estimated=False,
                 provider="", cover=""):
        self.lyrics = lyrics or []
        self.meta = meta
        self.status = status
        self.retry_at = retry_at
        self.estimated = estimated
        self.provider = provider
        self.cover = cover

    def has(self):
        return bool(self.lyrics)


def get_base_dir() -> str:
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        parent_dir = os.path.dirname(exe_dir)
        if not os.path.exists(os.path.join(exe_dir, "config.json")) and os.path.exists(os.path.join(parent_dir, "config.json")):
            return parent_dir
        return exe_dir
    curr = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(curr).lower() == "src":
        return os.path.dirname(curr)
    return curr


class LyricsService:
    """lookup() никогда не блокируется — текст качается фоном."""

    def __init__(self, directory=None, allow_estimate=True, yandex_token=""):
        self.directory = directory or os.path.join(get_base_dir(), "lyrics")
        os.makedirs(self.directory, exist_ok=True)
        self.allow_estimate = allow_estimate
        self.yandex_token = yandex_token or ""
        self.cache = OrderedDict()
        self.pending = set()
        self.q = queue.Queue()
        self.t = Transport()
        threading.Thread(target=self._work, daemon=True, name="Lyrics").start()

    def lookup(self, artist, title, source="", duration=0.0) -> Result:
        q_artist, q_title = query_for(artist, title, source)
        key = normalize(q_artist) + "\x1f" + normalize(q_title)
        res = self.cache.get(key)
        if res and (res.retry_at == 0 or time.time() * 1000 < res.retry_at):
            return res
        if key not in self.pending:
            self.pending.add(key)
            self.q.put((key, q_artist, q_title, duration * 1000.0))
        return Result(status="Загрузка текста…")

    def _work(self):
        while True:
            try:
                key, artist, title, duration_ms = self.q.get()
            except Exception:
                continue
            try:
                self._resolve(key, artist, title, duration_ms)
            except Exception:
                try:
                    self._publish(key, Result(status="Текст недоступен, повторяем…",
                                              retry_at=time.time() * 1000 + SEVERE_TTL))
                except Exception:
                    pass

    def _resolve(self, key, artist, title, duration_ms):
        local = self._local(artist, title)
        if local and local.lyrics:
            self._publish(key, local)
            return
        providers = Providers(self.t, yandex_token=self.yandex_token)
        found = providers.find(artist, title, duration_ms)
        if found.lrc:
            lyrics = parse_lrc(found.lrc)
            if lyrics:
                self._publish(key, Result(lyrics=lyrics, meta=found.meta, provider=found.provider,
                                          cover=found.cover), lrc=found.lrc)
                return
        if found.plain and self.allow_estimate:
            lrc = estimate(found.plain, int(duration_ms))
            lyrics = parse_lrc(lrc)
            if lyrics:
                self._publish(key, Result(lyrics=lyrics, meta=found.meta, provider=found.provider,
                                          estimated=True, cover=found.cover), lrc=lrc)
                return
        retry = time.time() * 1000 + (SEVERE_TTL if self.t.failed else MISS_TTL)
        self._publish(key, Result(status=("Источники текста недоступны — повторяем поиск…" if self.t.failed
                                          else "Текст не найден в трёх источниках"), retry_at=retry))

    def _publish(self, key, result: Result, lrc=None):
        self.cache[key] = result
        while len(self.cache) > 48:
            self.cache.popitem(last=False)
        self.pending.discard(key)
        if lrc and result.lyrics:
            self._save_lrc(key, result, lrc)

    # --- диск -----------------------------------------------------------------
    def _local(self, artist, title):
        key = normalize(artist) + "\x1f" + normalize(title)
        digest_path = os.path.join(self.directory, hashlib.sha256(key.encode()).hexdigest()[:12] + ".lrc")
        for kind, path in (("кэш", digest_path),):
            lrc = self._read(path)
            if lrc:
                lyrics = parse_lrc(lrc)
                if lyrics:
                    return Result(lyrics=lyrics, provider=kind, estimated="[delta-estimated:1]" in lrc)
        # вручную положенный файл "Исполнитель - Название.lrc" (как в WorldLyrics)
        name = re.sub(r'[<>:"/\\|?*]', "_", f"{artist} - {title}")[:150]
        manual = os.path.join(self.directory, name + ".lrc")
        lrc = self._read(manual)
        if lrc:
            lyrics = parse_lrc(lrc)
            if lyrics:
                return Result(lyrics=lyrics, provider="локальный файл")
        return None

    def _save_lrc(self, key, result: Result, lrc):
        try:
            os.makedirs(self.directory, exist_ok=True)
            digest = hashlib.sha256(key.encode()).hexdigest()[:12]
            with open(os.path.join(self.directory, digest + ".lrc"), "w", encoding="utf-8") as f:
                f.write(lrc)
        except Exception:
            pass

    @staticmethod
    def _read(path):
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass
        return None
