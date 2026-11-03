# -*- coding: utf-8 -*-
"""
Discord Rich Presence для любого музыкального плеера на Windows.
Использует System Media Transport Controls (SMTC) - тот же механизм,
который выводит инфу о треке в всплывающее окно звука Windows.
Работает с Яндекс Музыкой, YouTube (Chrome/Edge), Spotify, VLC и др.
"""
import asyncio
import json
import os
import sys
import threading
import time
import urllib.parse

# Чтоб русский текст в консоли не превращался в кракозябры
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests
from pypresence import (
    ConnectionTimeout,
    DiscordError,
    DiscordNotFound,
    InvalidPipe,
    Presence,
    PipeClosed,
    PyPresenceException,
)

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
except ImportError:
    print("Не установлена библиотека winsdk. Выполни: pip install winsdk")
    sys.exit(1)

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


BASE_DIR = get_base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "_readme": "Вставь в client_id свой Application ID с https://discord.com/developers/applications (OAuth2)",
    # Client ID твоего Discord-приложения.
    # discord.com/developers/applications -> New Application -> OAuth2 -> Application ID
    "client_id": "",
    # Показывать прогресс (время начала/окончания трека)
    "show_time": True,
    # Показывать кнопку "Открыть трек"
    "show_button": True,
    # Показывать обложку трека (подгружается картинка через iTunes/Deezer)
    "show_images": True,
    # Показывать синхронизированный текст песни (LRCLIB / YouTube Music / NetEase)
    "show_lyrics": True,
    # Если синхронного текста нигде нет — делаем приблизительную синхронизацию
    # из простого текста (как WorldLyrics в DeltaClient)
    "lyrics_estimate": True,
    # OAuth-токен Яндекс Музыки (опционально): тексты и обложки из каталога Яндекса.
    # Где взять написано в README ("Токен Яндекс Музыки")
    "yandex_token": "",
    # Интервал опроса системы (в секундах)
    "refresh_interval": 2,
}

# ----------------------------------------------------------------------------------------------------------------------
# Конфиг

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    user = {}

    def coerce_bool(value, default):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("true", "1", "да", "yes", "on", "вкл"):
                return True
            if s in ("false", "0", "нет", "no", "off", "выкл", ""):
                return False
        return default

    def coerce_num(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            for key in ("show_time", "show_button", "show_images", "show_lyrics", "lyrics_estimate"):
                cfg[key] = coerce_bool(user.get(key), DEFAULT_CONFIG[key])
            cfg["refresh_interval"] = coerce_num(user.get("refresh_interval"), DEFAULT_CONFIG["refresh_interval"])
            cfg["client_id"] = str(user.get("client_id") or "").strip()
            cfg["yandex_token"] = str(user.get("yandex_token") or "").strip()
        except Exception as e:
            print("Не удалось прочитать config.json:", e)

    # Токен мог попасть не в то поле (например вместо подсказки _readme)
    if not cfg["yandex_token"] and cfg.get("client_id", "").startswith("y0_"):
        cfg["yandex_token"], cfg["client_id"] = cfg["client_id"], cfg["client_id"]
        print("ВНИМАНИЕ: поле client_id содержит y0_-токен — переложил его в yandex_token.")
    if not cfg["yandex_token"]:
        for key, val in list(cfg.items()) + list(user.items()):
            if isinstance(val, str) and val.strip().startswith("y0_"):
                cfg["yandex_token"] = val.strip()
                print(f"Замечен токен в поле '{key}' — использую его как yandex_token (лучше перенести в 'yandex_token').")
                break
    cfg["yandex_token"] = cfg["yandex_token"].split("OAuth")[-1].strip() if "OAuth" in cfg["yandex_token"] else cfg["yandex_token"]

    if not cfg["client_id"]:
        fw = dict(DEFAULT_CONFIG)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(fw, f, ensure_ascii=False, indent=2)
        print(
            "\n" + "=" * 60 +
            "\n  Нужно указать client_id в config.json!\n\n"
            "  1. Открой https://discord.com/developers/applications\n"
            "  2. Нажми 'New Application' и задай любое имя (напр. Music RPC)\n"
            "  3. Во вкладке 'OAuth2' скопируй 'Application ID'\n"
            "  4. ВСТАВЬ его в config.json вместо \"\"\n" +
            "=" * 60
        )
        if getattr(sys, "frozen", False):
            try:
                input("\nНажмите Enter для выхода...")
            except Exception:
                pass
        sys.exit(0)
    return cfg


CONFIG = load_config()

APP_NAMES = [
    ("yandexmusic", "Яндекс Музыка"),
    ("yandex", "Яндекс Музыка"),
    ("chrome.exe", "Google Chrome"),
    ("chrome", "Chrome"),
    ("msedge", "Microsoft Edge"),
    ("edge", "Microsoft Edge"),
    ("firefox", "Firefox"),
    ("spotify", "Spotify"),
    ("itunes", "Apple Music"),
    ("vlc", "VLC"),
    ("wmplayer", "Windows Player"),
    ("aimp", "AIMP"),
    ("foobar", "foobar2000"),
    ("potplayer", "PotPlayer"),
    ("minecraft", "Minecraft"),
    ("steam music", "Steam"),
    ("zunemusic", "Медиаплеер"),
    ("mediaplayer", "Медиаплеер"),
    ("brave", "Brave"),
]


def pretty_app(app_id: str) -> str:
    a = (app_id or "").lower()
    for key, name in APP_NAMES:
        if key in a:
            return name
    return (app_id or "Неизвестный источник").split("!")[0].split(".exe")[0]

# ----------------------------------------------------------------------------------------------------------------------
# Обложка через бесплатных поиск (iTunes / Deezer)

_art_cache = {}


def fetch_art_url(artist: str, title: str):
    key = f"{artist}|{title}".lower()
    if key in _art_cache:
        return _art_cache[key]
    art = None
    try:
        q = urllib.parse.quote(f"{artist} {title}".strip())
        r = requests.get(
            f"https://itunes.apple.com/search?term={q}&entity=song&limit=5",
            timeout=6,
        )
        for item in (r.json().get("results") or [])[:5]:
            art100 = item.get("artworkUrl100")
            if art100:
                art = art100.replace("100x100", "512x512")
                break
    except Exception:
        pass
    if not art:
        try:
            q = urllib.parse.quote(f'artist:"{artist}" track:"{title}"')
            r = requests.get(f"https://api.deezer.com/search?q={q}&limit=1", timeout=6)
            data = r.json().get("data") or []
            if data:
                art = data[0].get("album", {}).get("cover_big") or data[0].get("album", {}).get("cover_medium")
        except Exception:
            pass
    _art_cache[key] = art
    return art


# ----------------------------------------------------------------------------------------------------------------------
# Синхронизированный текст — фоновой модуль lyrics.py (порт WorldLyrics)
import bisect
try:
    from . import lyrics as lyrics_mod
except ImportError:
    try:
        import lyrics as lyrics_mod
    except ImportError:
        from src import lyrics as lyrics_mod

LYRICS = None  # создаётся в main() после загрузки конфига


def get_lyrics_service():
    global LYRICS
    if LYRICS is None:
        LYRICS = lyrics_mod.LyricsService(
            allow_estimate=CONFIG.get("lyrics_estimate", True),
            yandex_token=CONFIG.get("yandex_token", ""),
        )
    return LYRICS


# ----------------------------------------------------------------------------------------------------------------------
# Состояние

STATE = {
    "ready": False,
    "app": "",
    "title": "",
    "artist": "",
    "album": "",
    "status": "STOPPED",
    "position": 0.0,   # сек
    "duration": 0.0,   # сек
    "updated_at": 0.0, # момент снятия снапшота (time.time())
}
LOCK = threading.Lock()


def track_key(st):
    return (st["app"], st["title"], st["artist"], st["album"], st["status"])


# ----------------------------------------------------------------------------------------------------------------------
# Сбор данных с SMTC

# Мусорные хвосты таб-заголовков (Chrome отдаёт их вместо нормального метадата)
JUNK_TAILS = [
    "слушать онлайн на Яндекс Музыке",
    "Яндекс Музыка",
    "Яндекс.Музыка",
    "YouTube Music",
    "YouTube",
    "Spotify",
    "Apple Music",
    "Deezer",
    "Suno",
    "SoundCloud",
    "Zaycev.net",
    "Radio Record",
    "- listen online",
]
_SEPARATORS = (" — ", " – ", " - ", " —", "— ", " · ")


def cleanup_media_title(title: str, artist: str):
    """Убирает суффиксы сайтов и, если исполнитель пуст, пробует вытащить
    его из таб-заголовка формата 'Трек - Исполнитель'."""
    t = (title or "").strip()
    changed = True
    while t and changed:
        changed = False
        for junk in JUNK_TAILS:
            if t.lower().endswith(junk.lower()):
                t = t[: -len(junk)].strip()
                changed = True
        for sep in _SEPARATORS:
            if t.endswith(sep):
                t = t[: -len(sep)].strip()
                changed = True
    if not artist:
        for sep in (" — ", " – ", " - "):
            if t.count(sep) == 1:
                left, right = t.split(sep, 1)
                left, right = left.strip(), right.strip()
                if left and right:
                    return right[:128], left[:64]  # трек, исполнитель
    return t[:128] or title[:128], artist


PREF_KEYWORD = ("yandex",)


# winsdk возвращает числовые статусы, собираем словарь самого enum: PLAYING=4, PAUSED=5
_STATUS_NAMES = {}
try:
    for _name in dir(PlaybackStatus):
        if not _name.startswith("_"):
            try:
                _STATUS_NAMES[int(getattr(PlaybackStatus, _name))] = _name
            except (TypeError, ValueError):
                continue
except Exception:
    pass


def status_str(status) -> str:
    try:
        return _STATUS_NAMES.get(int(status), "UNKNOWN")
    except (TypeError, ValueError):
        return str(status).rsplit(".", 1)[-1]


def is_preferred(session) -> bool:
    try:
        src = (session.source_app_user_model_id or "").lower()
        return "yandex" in src
    except Exception:
        return False


async def watch_loop():
    try:
        mgr = await MediaManager.request_async()
    except Exception as e:
        print("Ошибка SMTC:", e)
        return
    poll = max(0.3, float(CONFIG.get("refresh_interval", 2)))
    fallback = None
    fallback_t = 0.0
    paused_since = 0.0
    last_w_status = None
    sticky_src = None
    while True:
        data = None
        try:
            sessions = mgr.get_sessions()
            candidates = []
            for s in sessions:
                try:
                    status = status_str(s.get_playback_info().playback_status)
                except Exception:
                    continue
                try:
                    mp = await s.try_get_media_properties_async()
                except Exception:
                    mp = None
                if mp is None:
                    continue
                candidates.append((s, status, mp))
            playing = [(s, st, m) for (s, st, m) in candidates if st == "PLAYING"]
            paused = [(s, st, m) for (s, st, m) in candidates if st == "PAUSED"]
            others = [(s, st, m) for (s, st, m) in candidates
                      if st not in ("PLAYING", "PAUSED", "CLOSED", "STOPPED")]  # CHANGING и пр.
            pool = playing if playing else (paused if paused else others)
            chosen = None
            chosen_status = None
            chosen_mp = None
            # Липкость: играющая первая выбранная сессия имеет приоритет, чтоб
            # статус не прыгал между Chrome и плеером при двух играющих сразу
            if sticky_src:
                for s, st, m in pool:
                    if (s.source_app_user_model_id or "") == sticky_src:
                        chosen, chosen_status, chosen_mp = s, st, m
                        break
            if chosen is None:
                for s, st, m in pool:
                    if is_preferred(s):
                        chosen, chosen_status, chosen_mp = s, st, m
                        break
            if chosen is None and pool:
                chosen, chosen_status, chosen_mp = pool[0]
            sticky_src = (chosen.source_app_user_model_id or "") if chosen is not None else sticky_src

            if chosen is not None:
                tl = chosen.get_timeline_properties()
                mp = chosen_mp
                chosen_status_eff = chosen_status
                # Дребезг: некоторые плееры/устройства паузают на 1-2 сек -
                # такие краткие паузы игнорируем, статус и таймер не трогаем
                now_w = time.time()
                if chosen_status == "PAUSED":
                    if not paused_since:
                        paused_since = now_w
                    if time.time() - paused_since < 3.0 and last_w_status == "PLAYING":
                        chosen_status_eff = "PLAYING"  # считаем моргание случайностью
                elif chosen_status == "PLAYING":
                    paused_since = 0.0
                last_w_status = chosen_status_eff
                status = chosen_status_eff
                clean_title, clean_artist = cleanup_media_title(mp.title or "", mp.artist or mp.subtitle or "")
                data = {
                    "ready": False,
                    "app": pretty_app(chosen.source_app_user_model_id),
                    "title": clean_title,
                    "artist": clean_artist,
                    "album": mp.album_title or "",
                    "status": status,
                    "position": tl.position.total_seconds() if tl.position else 0.0,
                    "duration": tl.end_time.total_seconds() if tl.end_time else 0.0,
                    "updated_at": time.time(),
                }
                data["ready"] = True
        except Exception as e:
            print("Ошибка чтения сессии:", e)
        # Гестна: SMTC при смене/паузе треков на миг теряет сессию и
        # статус в Discord мерцал — держим прежний снимок опроса.
        if data is None and fallback is not None and time.time() - fallback_t < 12:
            data = dict(fallback)
        elif data is not None:
            fallback, fallback_t = dict(data), time.time()
        with LOCK:
            global_state_save(data)
        await asyncio.sleep(poll)


def global_state_save(data):
    if data:
        STATE.update(data)
    else:
        STATE["ready"] = False


# ----------------------------------------------------------------------------------------------------------------------
# Discord RPC

def main():
    client_id = CONFIG["client_id"]
    print("Discord Music RPC запущен. Ожидаю треки...")
    try:
        rpc = Presence(client_id=client_id)
        rpc.connect()
    except DiscordNotFound:
        print("Discord не запущен или Client ID неверный. Проверь config.json и запусти Discord.")
        sys.exit(1)
    except (ConnectionTimeout, PyPresenceException, OSError) as e:
        print("Ошибка подключения к Discord:", e, "— повтор через 10 сек...")
        time.sleep(10)
        return main()

    print("Подключено к Discord!")

    last_key = None
    last_time_status = None
    last_start_ts = None
    last_push_ts = 0.0
    last_latch = None
    last_lrc_idx = -1
    last_had_lyrics = False
    last_art_sent = None

    while True:
        try:
            with LOCK:
                st = dict(STATE)
            if not st["ready"] or not st["title"]:
                if last_key is not None:
                    try:
                        rpc.clear()
                    except Exception:
                        pass
                    print("Треков нет - присутствие скрыто")
                    last_key = None
                    last_time_status = None
                time.sleep(3)
                continue

            key = track_key(st)
            position = st["position"]
            dur = st.get("duration") or 0
            now = time.time()
            duration = dur
            status = st["status"]

            # Таймер: СМТС часто не наращивает position со временем (Chrome),
            # поэтому тикаем сами от точки привязки (t0 + elapsed0).
            if key != last_key or last_latch is None or status != "PLAYING" and last_time_status == "playing":
                # новый трек / пауза — перезахват
                last_latch = {"t0": now, "elapsed0": position, "latch_pos": position}
            elif status == "PLAYING" and abs(position - last_latch["latch_pos"]) > 6:
                # SMTC перескочил — перемотка, перезахват
                last_latch = {"t0": now, "elapsed0": position, "latch_pos": position}
            elapsed = max(0.0, (now - last_latch["t0"]) + last_latch["elapsed0"]) if status == "PLAYING" else \
                last_latch["elapsed0"]

            needs_update = False
            reason = ""
            if key != last_key:
                needs_update = True
                reason = "key"
            elif last_time_status == "paused" and status == "PLAYING":
                needs_update = True
                reason = "resume"
            elif last_time_status == "playing" and status != "PLAYING":
                needs_update = True
                reason = "pause"
            # Перемотка: позиция в системном медиа-контроле разошлась с тем,
            # что таймер Discord уже показал => обновляем таймкоды
            elif (last_start_ts is not None and status == "PLAYING"
                  and abs((now - last_start_ts) - elapsed) > 6
                  and now - last_push_ts > 10):
                needs_update = True
                reason = "drift"
            if os.environ.get("RPC_DEBUG") and needs_update:
                print(f"[debug] update reason={reason} shown={now - last_start_ts if last_start_ts else 0:.1f} "
                      f"smtc={elapsed:.1f} pos={st['position']:.1f} upd_age={now - st['updated_at']:.1f}")

            # Лирика: неблокирующая фоновая загрузка (lyrics.py), строка по таймингам
            lyrics = []
            disp_artist, disp_title, disp_album = st["artist"], st["title"], st["album"]
            lrc_idx, lrc_line = -1, None
            lyrics_ready = False
            show_lyrics = CONFIG.get("show_lyrics", True)
            if show_lyrics and (st["artist"] or st["title"]):
                svc = get_lyrics_service()
                res = svc.lookup(st["artist"], st["title"], st["app"], duration)
                if res.has():
                    lyrics = res.lyrics
                    lyrics_ready = True
                    meta = res.meta
                    # если SMTC не дал нормального исполнителя — берём с сервера
                    if not st["artist"] and meta[0]:
                        disp_artist = meta[0]
                        disp_title = meta[1] or disp_title
                    if not st["artist"] and not st["album"] and meta[2]:
                        disp_album = meta[2]
                    times = [e[0] for e in lyrics]
                    i = bisect.bisect_right(times, elapsed) - 1
                    # None = граница инструментального пауза — строку не показываем
                    if i >= 0 and lyrics[i][1]:
                        lrc_idx = i
                        lrc_line = lyrics[i][1]

            ycover = None
            if lyrics_ready:
                ycover = res.cover or None
            art = ycover or (fetch_art_url(disp_artist, disp_title) if (disp_artist or disp_title) else None)

            # Новая строка текста? (лимит ~1 пуш в 4.5 c из-за рейт-лимита Discord)
            if (not needs_update and lyrics_ready and status == "PLAYING"
                    and lrc_idx != last_lrc_idx and now - last_push_ts >= 4.5):
                needs_update = True
                reason = "lyric"
            # Текст дозагрузился после старта трека — сразу обновить статус
            elif (not needs_update and lyrics_ready and not last_had_lyrics
                  and now - last_push_ts >= 1.5):
                needs_update = True
                reason = "lyric-load"
            # Кэш обложки подгрузился — дополнить статус
            elif (not needs_update and art and art != last_art_sent
                  and now - last_push_ts >= 2.0):
                needs_update = True
                reason = "art"
            if os.environ.get("RPC_DEBUG"):
                if needs_update:
                    print(f"[debug] update {reason}")
                if lrc_idx != last_lrc_idx:
                    print(f"[debug] line idx={lrc_idx} line={lrc_line!r:.80}")

            if needs_update:
                state_line = disp_artist or st["app"]
                if disp_album and disp_album not in (state_line or ""):
                    state_line = f'{state_line} — {disp_album}' if state_line else disp_album
                if status != "PLAYING":
                    state_line = f'⏸ {state_line or "Пауза"}' if state_line else "⏸ Пауза"

                payload = {
                    "details": (f"♪ {lrc_line}" if lrc_line else disp_title)[:128],
                }
                if state_line:
                    payload["state"] = f'{state_line}'[:128]
                if lrc_line and disp_title:
                    # в state допишем трек, чтоб он был виден под строкой текста
                    payload["state"] = f'{disp_title} — {state_line}'[:128] \
                        if disp_title not in state_line else state_line[:128]

                # Обложка (внешний URL)
                if art and CONFIG.get("show_images", True):
                    payload["large_image"] = art
                    payload["large_text"] = (disp_album or disp_title)[:128]

                # Время
                if CONFIG["show_time"] and status == "PLAYING" and duration > 0 and elapsed < duration:
                    payload["start"] = max(0, round(now - elapsed))
                    payload["end"] = round(now + (duration - elapsed))

                # Кнопка
                if CONFIG.get("show_button", True) and (st["artist"] or st["title"]):
                    search_text = f'{st["artist"]} {st["title"]}'.strip()
                    if "yandex" in st["app"].lower():
                        btn_label = "Открыть в Яндекс Музыке"
                    else:
                        btn_label = "Открыть трек"
                    payload["buttons"] = [{
                        "label": btn_label[:32],
                        "url": "https://music.yandex.ru/search?text=" + urllib.parse.quote(search_text),
                    }]

                try:
                    rpc.update(**payload)
                    emoji = "▶️" if status == "PLAYING" else "⏸"
                    if lrc_line:
                        print(f'{emoji} ♪ {lrc_line} | {st["title"]} — {disp_artist} [{st["app"]}]')
                    else:
                        print(f'{emoji} {st["title"]} — {disp_artist} [{st["app"]}]')
                    last_key = key
                    last_time_status = "playing" if status == "PLAYING" else "paused"
                    last_start_ts = payload.get("start")
                    last_push_ts = now
                    last_lrc_idx = lrc_idx
                    last_had_lyrics = lyrics_ready
                    last_art_sent = art if art else last_art_sent
                except Exception as e:
                    print("Ошибка обновления RPC:", e)
                    time.sleep(5)
            # быстро реагируем на смену строки текста (лимит пушей всё равно есть)
            time.sleep(min(1.0, float(CONFIG["refresh_interval"])))
        except (ConnectionTimeout, InvalidPipe, PipeClosed, DiscordError):
            print("Discord отключился. Повторное подключение через 5 сек...")
            try:
                time.sleep(5)
                rpc = Presence(client_id=CONFIG["client_id"])
                rpc.connect()
                last_key = None
            except Exception as e2:
                print("Не удалось переподключиться:", e2)
        except KeyboardInterrupt:
            try:
                rpc.clear()
            except Exception:
                pass
            sys.exit(0)
        except Exception as e:
            print("Ошибка основного цикла:", e)
            time.sleep(5)


def watcher_run():
    asyncio.run(watch_loop())


def start():
    threading.Thread(target=watcher_run, daemon=True).start()
    time.sleep(1)
    main()


if __name__ == "__main__":
    start()
