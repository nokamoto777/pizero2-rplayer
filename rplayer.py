#!/usr/bin/env python3
import base64
import io
import json
import os
import queue
import signal
import secrets
import subprocess
import time
import re
import random
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl, urljoin
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

DEFAULT_STATIONS_FILE = "stations.json"
DEFAULT_MPD_HOST = "localhost"
DEFAULT_MPD_PORT = "6600"
DEFAULT_BUTTON_A_PIN = 5
DEFAULT_BUTTON_B_PIN = 6
DEFAULT_BUTTON_X_PIN = 16
DEFAULT_BUTTON_Y_PIN = 24
DEFAULT_REFRESH_SEC = 1.0
DEFAULT_METADATA_SEC = 10.0
DEFAULT_PROGRAM_REFRESH_SEC = float(os.getenv("RPLAYER_PROGRAM_REFRESH_SEC", "3600"))
DEFAULT_DOUBLE_CLICK_SEC = float(os.getenv("RPLAYER_DOUBLE_CLICK_SEC", "0.5"))
DEFAULT_SHUTDOWN_CONFIRM_SEC = float(os.getenv("RPLAYER_SHUTDOWN_CONFIRM_SEC", "10"))
DEBUG = os.getenv("RPLAYER_DEBUG") == "1"
DEFAULT_ALSA_DEVICE = os.getenv("RPLAYER_ALSA_DEVICE", "hw:1,0")
DEFAULT_FFMPEG = os.getenv("RPLAYER_FFMPEG", "ffmpeg")
DEFAULT_RADIKO_AUTHKEY = os.getenv(
    "RPLAYER_RADIKO_AUTHKEY", "bcd151073c03b352e1ef2fd66c32209da9ca0afa"
)
DEFAULT_RADIKO_APP = os.getenv("RPLAYER_RADIKO_APP", "pc_html5")
DEFAULT_RADIKO_APP_VER = os.getenv("RPLAYER_RADIKO_APP_VER", "0.0.1")
DEFAULT_RADIKO_DEVICE = os.getenv("RPLAYER_RADIKO_DEVICE", "pc")
DEFAULT_RADIKO_USER = os.getenv("RPLAYER_RADIKO_USER", "dummy_user")
DEFAULT_RADIKO_COOKIE = os.getenv("RPLAYER_RADIKO_COOKIE", "")
DEFAULT_RADIKO_AUTH1_URLS = os.getenv(
    "RPLAYER_RADIKO_AUTH1_URLS",
    "https://radiko.jp/v2/api/auth1,https://radiko.jp/v2/api/auth1_fms,http://radiko.jp/v2/api/auth1,http://radiko.jp/v2/api/auth1_fms",
)
DEFAULT_RADIKO_AUTH2_URLS = os.getenv(
    "RPLAYER_RADIKO_AUTH2_URLS",
    "https://radiko.jp/v2/api/auth2,https://radiko.jp/v2/api/auth2_fms,http://radiko.jp/v2/api/auth2,http://radiko.jp/v2/api/auth2_fms",
)


@dataclass
class Station:
    id: str
    name: str
    stream_url: str
    image_url: str = ""


@dataclass
class ProgramInfo:
    title: str
    img_url: str
    ft: datetime
    to: datetime


class ConsoleDisplay:
    def __init__(self) -> None:
        self._last = ("", "")

    def show(self, line1: str, line2: str) -> None:
        if (line1, line2) == self._last:
            return
        self._last = (line1, line2)
        print(f"{line1} | {line2}")


class LineOutDisplay:
    def __init__(self) -> None:
        self._display = None
        self._image = None
        self._draw = None
        self._font = None
        self._font_size = _env_int("RPLAYER_FONT_SIZE", 16)
        self._width = 0
        self._height = 0
        self._fallback = ConsoleDisplay()
        self._init_display()

    def _init_display(self) -> None:
        # Best-effort: try to use ST7789 + PIL if available.
        try:
            from PIL import Image, ImageDraw, ImageFont  # type: ignore
        except Exception:
            return

        display_cls = None
        try:
            import st7789  # type: ignore

            display_cls = st7789.ST7789
        except Exception:
            display_cls = None

        if display_cls is None:
            try:
                from ST7789 import ST7789 as display_cls  # type: ignore
            except Exception:
                display_cls = None

        if display_cls is None:
            if DEBUG:
                print("Display init: no ST7789 module found")
            return

        try:
            rotation = _env_int("RPLAYER_ST7789_ROTATION", 90)
            port = _env_int("RPLAYER_ST7789_PORT", 0)
            cs = _env_int("RPLAYER_ST7789_CS", 1)
            dc = _env_int("RPLAYER_ST7789_DC", 9)
            backlight = _env_int("RPLAYER_ST7789_BACKLIGHT", 13)
            speed_hz = _env_int("RPLAYER_ST7789_SPEED_HZ", 20_000_000)
            self._display = display_cls(
                rotation=rotation,
                port=port,
                cs=cs,
                dc=dc,
                backlight=backlight,
                spi_speed_hz=speed_hz,
            )
            self._width = getattr(self._display, "width", 240)
            self._height = getattr(self._display, "height", 240)
            self._image = Image.new("RGB", (self._width, self._height))
            self._draw = ImageDraw.Draw(self._image)
            self._font = self._load_font(ImageFont)
            if DEBUG:
                print(f"Display init: {self._width}x{self._height}")
        except Exception:
            if DEBUG:
                print("Display init: failed")
            self._display = None

    def _load_font(self, image_font) -> object:
        font_path = os.getenv("RPLAYER_FONT", "").strip()
        candidates = [font_path] if font_path else []
        candidates.extend(
            [
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJKjp-Regular.otf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]
        )
        for path in candidates:
            if not path:
                continue
            try:
                return image_font.truetype(path, self._font_size)
            except Exception:
                continue
        return image_font.load_default()

    def show(self, line1: str, line2: str, image=None) -> None:
        if not self._display:
            self._fallback.show(line1, line2)
            return

        try:
            assert self._draw and self._image and self._font
            self._draw.rectangle((0, 0, self._width, self._height), fill=(0, 0, 0))
            line1 = _fit_text(self._draw, self._font, line1, self._width - 4)
            line2 = _fit_text(self._draw, self._font, line2, self._width - 4)
            self._draw.text((2, 2), line1, font=self._font, fill=(255, 255, 255))
            line2_y = 2 + self._font_size + 2
            self._draw.text((2, line2_y), line2, font=self._font, fill=(255, 255, 255))
            if image is not None:
                img_top = line2_y + self._font_size + 4
                if img_top < self._height - 2:
                    target_w = self._width - 4
                    target_h = self._height - img_top - 2
                    try:
                        resized = _fit_image(image, target_w, target_h)
                        x = (self._width - resized.width) // 2
                        y = img_top + (target_h - resized.height) // 2
                        self._image.paste(resized, (x, y))
                    except Exception:
                        pass
            self._display.display(self._image)
        except Exception:
            self._fallback.show(line1, line2)


class ButtonInput:
    def __init__(self, a_pin: int, b_pin: int, x_pin: int, y_pin: int) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._btn_a = None
        self._btn_b = None
        self._btn_x = None
        self._btn_y = None
        self._last_y_at = 0.0
        if os.getenv("RPLAYER_DISABLE_GPIO") == "1":
            return
        self._init_gpio(a_pin, b_pin, x_pin, y_pin)

    def _init_gpio(self, a_pin: int, b_pin: int, x_pin: int, y_pin: int) -> None:
        try:
            from gpiozero import Button  # type: ignore
        except Exception:
            if DEBUG:
                print("GPIO init: gpiozero not available")
            return

        def on_a() -> None:
            self._queue.put("prev")
            if DEBUG:
                print("Button A pressed")

        def on_b() -> None:
            self._queue.put("next")
            if DEBUG:
                print("Button B pressed")

        def on_x() -> None:
            self._queue.put("mode")
            if DEBUG:
                print("Button X pressed")

        def on_y() -> None:
            now = time.time()
            if now - self._last_y_at <= DEFAULT_DOUBLE_CLICK_SEC:
                if DEBUG:
                    print("Button Y double-click")
                self._queue.put("shutdown")
                self._last_y_at = 0.0
                return
            if DEBUG:
                print("Button Y pressed")
            self._last_y_at = now

        try:
            self._btn_a = Button(a_pin, pull_up=True)
            self._btn_b = Button(b_pin, pull_up=True)
            self._btn_x = Button(x_pin, pull_up=True)
            self._btn_y = Button(y_pin, pull_up=True)
            self._btn_a.when_pressed = on_a
            self._btn_b.when_pressed = on_b
            self._btn_x.when_pressed = on_x
            self._btn_y.when_pressed = on_y
            if DEBUG:
                print(f"GPIO init: A=BCM{a_pin} B=BCM{b_pin} X=BCM{x_pin} Y=BCM{y_pin}")
        except Exception:
            # Ignore GPIO failures and fall back to no-op input.
            if DEBUG:
                print("GPIO init: failed")
            return

    def poll(self) -> Optional[str]:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None


class RadikoResolver:
    def __init__(self) -> None:
        self._client = None
        self._station_map: Dict[str, object] = {}
        self._selected_id: Optional[str] = None
        self._auth_token: Optional[str] = None
        self._area_id: Optional[str] = None
        self._auth_headers: Dict[str, str] = {}
        self._stream_url_cache: Dict[str, str] = {}
        self._program_cache: Dict[str, Tuple[float, str, List[ProgramInfo]]] = {}
        self._init_client()

    def _init_client(self) -> None:
        try:
            import radiko  # type: ignore
        except Exception:
            if DEBUG:
                print("Radiko: radiko.py not available")
            return
        try:
            self._client = radiko.Client()
            for station in getattr(self._client, "stations", []):
                station_id = getattr(station, "id", "")
                if station_id:
                    self._station_map[str(station_id)] = station
            if DEBUG:
                print(f"Radiko: loaded {len(self._station_map)} stations")
                self._debug_client_attrs()
        except Exception:
            self._client = None
            self._station_map = {}
            if DEBUG:
                print("Radiko: init failed")

    def available(self) -> bool:
        return self._client is not None and bool(self._station_map)

    def station_name(self, station_id: str) -> Optional[str]:
        station = self._station_map.get(station_id)
        if not station:
            return None
        return str(getattr(station, "name", "")).strip() or None

    def auth_token(self) -> Optional[str]:
        if self._auth_token:
            return self._auth_token
        token = self._get_token_from_attrs()
        if token:
            self._auth_token = token
            return token
        token = self._get_token_from_methods()
        if token:
            self._auth_token = token
            return token
        token = self._auth_with_radiko()
        if token:
            self._auth_token = token
        return self._auth_token

    def auth_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = dict(self._auth_headers)
        token = self.auth_token()
        if token and "X-Radiko-Authtoken" not in headers:
            headers["X-Radiko-Authtoken"] = token
        return headers

    def current_program(self, station_id: str) -> Optional[ProgramInfo]:
        programs = self._get_programs(station_id)
        if not programs:
            return None
        now = datetime.now(ZoneInfo("Asia/Tokyo"))
        for program in programs:
            if program.ft <= now < program.to:
                return program
        return None

    def _get_programs(self, station_id: str) -> List[ProgramInfo]:
        now = datetime.now(ZoneInfo("Asia/Tokyo"))
        date_key = now.strftime("%Y%m%d")
        cached = self._program_cache.get(station_id)
        if cached:
            fetched_at, cached_date, programs = cached
            if cached_date == date_key and (time.time() - fetched_at) < DEFAULT_PROGRAM_REFRESH_SEC:
                return programs
        programs = self._fetch_programs(station_id, date_key)
        if programs:
            self._program_cache[station_id] = (time.time(), date_key, programs)
        return programs

    def _fetch_programs(self, station_id: str, date_key: str) -> List[ProgramInfo]:
        try:
            import requests  # type: ignore
        except Exception:
            return []
        url = f"https://radiko.jp/v3/program/station/date/{date_key}/{station_id}.xml"
        try:
            res = requests.get(url, timeout=5)
            if res.status_code != 200:
                if DEBUG:
                    print(f"Radiko: program status {res.status_code} ({url})")
                return []
            root = ET.fromstring(res.text)
            station_node = root.find(".//station")
            if station_node is None:
                return []
            programs: List[ProgramInfo] = []
            for prog in station_node.findall(".//prog"):
                ft_raw = prog.attrib.get("ft", "")
                to_raw = prog.attrib.get("to", "")
                title = (prog.findtext("title") or "").strip()
                img = (prog.findtext("img") or "").strip()
                if not ft_raw or not to_raw:
                    continue
                try:
                    ft = datetime.strptime(ft_raw, "%Y%m%d%H%M%S").replace(
                        tzinfo=ZoneInfo("Asia/Tokyo")
                    )
                    to = datetime.strptime(to_raw, "%Y%m%d%H%M%S").replace(
                        tzinfo=ZoneInfo("Asia/Tokyo")
                    )
                except ValueError:
                    continue
                programs.append(ProgramInfo(title=title, img_url=img, ft=ft, to=to))
            return programs
        except Exception as exc:
            if DEBUG:
                print(f"Radiko: program fetch failed: {exc!r}")
            return []

    @staticmethod
    def live_stream_url(station_id: str) -> str:
        return f"http://f-radiko.smartstream.ne.jp/{station_id}/_definst_/simul-stream.stream/playlist.m3u8"

    def stream_url(self, station_id: str) -> Optional[str]:
        if not self._client:
            return None
        cached = self._stream_url_cache.get(station_id)
        if cached and "medialist" not in cached:
            return cached

        url = self._stream_url_from_xml(station_id)
        if url:
            if "medialist" not in url:
                self._stream_url_cache[station_id] = url
            if DEBUG:
                print(f"Radiko: stream_url via xml -> {url}")
            return url
        if DEBUG:
            print(f"Radiko: stream_url via xml failed for {station_id}")

        station = self._station_map.get(station_id)
        if not station:
            return None
        try:
            self._ensure_selected(station, station_id)
            url = self._get_stream_from_station(station, station_id)
            if DEBUG:
                print(f"Radiko: stream_url for {station_id} -> {url}")
            return url
        except Exception as exc:
            if DEBUG:
                print(f"Radiko: stream_url failed for {station_id}: {exc!r}")
            if self._maybe_retry_after_select(exc, station, station_id):
                try:
                    url = self._get_stream_from_station(station, station_id)
                    if DEBUG:
                        print(f"Radiko: stream_url retry for {station_id} -> {url}")
                    return url
                except Exception as exc2:
                    if DEBUG:
                        print(f"Radiko: stream_url retry failed for {station_id}: {exc2!r}")
            # Fallback: some APIs may accept station_id directly.
            try:
                url = str(self._client.get_stream(station_id))
                if DEBUG:
                    print(f"Radiko: stream_url (id) for {station_id} -> {url}")
                return url
            except Exception as exc3:
                if DEBUG:
                    print(f"Radiko: stream_url (id) failed for {station_id}: {exc3!r}")
            return None

    def on_air_title(self, station_id: str) -> Optional[str]:
        station = self._station_map.get(station_id)
        if not station:
            return None
        try:
            self._ensure_selected(station, station_id)
            on_air = station.get_on_air()
            title = getattr(on_air, "title", "")
            return str(title).strip() or None
        except Exception as exc:
            if DEBUG:
                print(f"Radiko: on_air failed for {station_id}: {exc!r}")
            if self._maybe_retry_after_select(exc, station, station_id):
                try:
                    on_air = station.get_on_air()
                    title = getattr(on_air, "title", "")
                    return str(title).strip() or None
                except Exception as exc2:
                    if DEBUG:
                        print(f"Radiko: on_air retry failed for {station_id}: {exc2!r}")
            return None

    def _ensure_selected(self, station: object, station_id: str) -> None:
        if self._selected_id == station_id:
            return
        if self._select_station(station, station_id):
            self._selected_id = station_id

    def _maybe_retry_after_select(self, exc: Exception, station: object, station_id: str) -> bool:
        message = repr(exc)
        if "NotSelectedError" in message or "選択" in message:
            return self._select_station(station, station_id)
        return False

    def _select_station(self, station: object, station_id: str) -> bool:
        if not self._client:
            return False
        try:
            if hasattr(station, "select"):
                station.select()
            elif hasattr(self._client, "select_station"):
                self._call_select(self._client.select_station, station, station_id)
            elif hasattr(self._client, "set_station"):
                self._call_select(self._client.set_station, station, station_id)
            elif hasattr(self._client, "select"):
                self._call_select(self._client.select, station, station_id)
            elif hasattr(self._client, "station"):
                setattr(self._client, "station", station)
            elif hasattr(self._client, "selected_station"):
                setattr(self._client, "selected_station", station)
            else:
                return False
            if DEBUG:
                print(f"Radiko: selected {station_id}")
            return True
        except Exception as exc:
            if DEBUG:
                print(f"Radiko: select failed for {station_id}: {exc!r}")
            return False

    @staticmethod
    def _call_select(func, station: object, station_id: str) -> None:
        try:
            func(station)
        except TypeError:
            func(station_id)

    def _debug_client_attrs(self) -> None:
        if not DEBUG or not self._client:
            return
        keys = [k for k in dir(self._client) if "auth" in k.lower() or "token" in k.lower()]
        if keys:
            print("Radiko: client attrs:", ", ".join(sorted(keys)))

    def _get_token_from_attrs(self) -> Optional[str]:
        if not self._client:
            return None
        for attr in dir(self._client):
            if "token" in attr.lower():
                value = getattr(self._client, attr, None)
                if isinstance(value, str) and value:
                    return value
        return None

    def _get_stream_from_station(self, station: object, station_id: str) -> str:
        if hasattr(station, "get_stream_url"):
            try:
                url = str(station.get_stream_url())
                if DEBUG:
                    print(f"Radiko: stream via station.get_stream_url -> {url}")
                return url
            except Exception:
                pass
        if hasattr(station, "stream_url"):
            try:
                url = str(getattr(station, "stream_url"))
                if url:
                    if DEBUG:
                        print(f"Radiko: stream via station.stream_url -> {url}")
                    return url
            except Exception:
                pass
        if hasattr(self._client, "get_stream_url"):
            try:
                url = str(self._client.get_stream_url(station_id))
                if DEBUG:
                    print(f"Radiko: stream via client.get_stream_url -> {url}")
                return url
            except Exception:
                pass
        if hasattr(station, "get_stream"):
            try:
                url = str(station.get_stream())
                if DEBUG:
                    print(f"Radiko: stream via station.get_stream -> {url}")
                return url
            except Exception:
                pass
        try:
            url = str(self._client.get_stream(station))
            if DEBUG:
                print(f"Radiko: stream via client.get_stream(station) -> {url}")
            return url
        except Exception:
            pass
        try:
            url = str(self._client.get_stream())
            if DEBUG:
                print(f"Radiko: stream via client.get_stream() -> {url}")
            return url
        except TypeError:
            pass
        url = str(self._client.get_stream(station_id))
        if DEBUG:
            print(f"Radiko: stream via client.get_stream(id) -> {url}")
        return url

    def _stream_url_from_xml(self, station_id: str) -> Optional[str]:
        try:
            import requests  # type: ignore
        except Exception:
            return None

        urls = [
            f"https://radiko.jp/v3/station/stream/pc_html5/{station_id}.xml",
            f"https://radiko.jp/v3/station/stream/pc/{station_id}.xml",
            f"http://radiko.jp/v3/station/stream/pc_html5/{station_id}.xml",
            f"http://radiko.jp/v3/station/stream/pc/{station_id}.xml",
        ]
        try:
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Origin": "https://radiko.jp",
                "Referer": "https://radiko.jp/",
                "X-Radiko-App": DEFAULT_RADIKO_APP,
                "X-Radiko-App-Version": DEFAULT_RADIKO_APP_VER,
                "X-Radiko-Device": DEFAULT_RADIKO_DEVICE,
                "X-Radiko-User": DEFAULT_RADIKO_USER,
            }
            token = self.auth_token()
            if token:
                headers["X-Radiko-Authtoken"] = token
            if DEFAULT_RADIKO_COOKIE:
                headers["Cookie"] = DEFAULT_RADIKO_COOKIE
            for url in urls:
                res = requests.get(url, headers=headers, timeout=5)
                if DEBUG:
                    print(f"Radiko: stream xml status {res.status_code} for {station_id} ({url})")
                if res.status_code != 200:
                    continue
                text = res.text or ""
                try:
                    root = ET.fromstring(text)
                    # Prefer live/on-air (areafree=0, timefree=0)
                    url_nodes = root.findall(".//url")
                    playlist_urls: List[str] = []
                    lsid = secrets.token_hex(16)
                    for node in url_nodes:
                        pcu = node.find("playlist_create_url")
                        if pcu is None or not pcu.text:
                            continue
                        areafree = node.attrib.get("areafree")
                        timefree = node.attrib.get("timefree")
                        if areafree == "0" and timefree == "0":
                            playlist_urls.insert(0, pcu.text.strip())
                        else:
                            playlist_urls.append(pcu.text.strip())

                    param_variants = [
                        {"station_id": station_id, "l": "15", "lsid": lsid, "type": "b"},
                        {"station_id": station_id, "l": "15", "lsid": lsid},
                        {"station_id": station_id, "lsid": lsid},
                        {"station_id": station_id},
                    ]
                    for pcu_url in playlist_urls:
                        for params in param_variants:
                            if DEBUG:
                                print(f"Radiko: playlist_create_url -> {pcu_url}")
                            m3u8 = self._fetch_playlist_m3u8(pcu_url, params, headers)
                            if m3u8:
                                return m3u8

                    # Fallback: any element text that looks like an HLS URL.
                    for elem in root.iter():
                        if elem.text and "http" in elem.text and "m3u8" in elem.text:
                            candidate = elem.text.strip()
                            if "playlist.m3u8" in candidate and "station_id=" not in candidate:
                                continue
                            return candidate
                except Exception:
                    pass
                # Fallback: regex for any m3u8 URL in the XML.
                match = re.search(r"https?://[^\\s<>\"]+\\.m3u8", text)
                if match:
                    return match.group(0)
                if DEBUG:
                    print(f"Radiko: stream xml no url for {station_id} ({url})")
                    print(f"Radiko: stream xml body {text[:200]!r}")
        except Exception as exc:
            if DEBUG:
                print(f"Radiko: stream xml failed for {station_id}: {exc!r}")
        return None

    @staticmethod
    def _with_query(url: str, extra: Dict[str, str]) -> str:
        parts = urlparse(url)
        query = dict(parse_qsl(parts.query))
        query.update(extra)
        return urlunparse(parts._replace(query=urlencode(query)))

    def _fetch_playlist_m3u8(
        self, base_url: str, params: Dict[str, str], headers: Dict[str, str]
    ) -> Optional[str]:
        try:
            import requests  # type: ignore
        except Exception:
            return None
        try:
            post_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
            if DEBUG:
                print(f"Radiko: playlist POST {base_url} params={params}")
            # Try POST with form data first (radiko expects parameters in body).
            res = requests.post(
                base_url,
                headers=post_headers,
                data=params,
                timeout=5,
            )
            if res.status_code != 200:
                if DEBUG:
                    print(f"Radiko: playlist status {res.status_code} for {base_url}")
                    print(f"Radiko: playlist body {res.text[:200]!r}")
                # Fallback: GET with query
                url = self._with_query(base_url, params)
                res = requests.get(url, headers=headers, timeout=5)
                if res.status_code != 200:
                    if DEBUG:
                        print(f"Radiko: playlist status {res.status_code} for {url}")
                        print(f"Radiko: playlist body {res.text[:200]!r}")
                    return None
            body = res.text or ""
            match = re.search(r"https?://[^\s<>\"]+\.m3u8", body)
            if match:
                return match.group(0)
            if "#EXTM3U" in body:
                lines = [line.strip() for line in body.splitlines() if line.strip()]
                for line in lines:
                    if line.startswith("#"):
                        continue
                    return urljoin(res.url, line)
            if DEBUG:
                print(f"Radiko: playlist no m3u8 for {res.url}")
                print(f"Radiko: playlist body {body[:200]!r}")
        except Exception as exc:
            if DEBUG:
                print(f"Radiko: playlist fetch failed: {exc!r}")
        return None

    def _get_token_from_methods(self) -> Optional[str]:
        if not self._client:
            return None
        for name in ("auth", "authorize", "authenticate", "get_token"):
            func = getattr(self._client, name, None)
            if callable(func):
                try:
                    value = func()
                    if isinstance(value, str) and value:
                        return value
                except Exception as exc:
                    if DEBUG:
                        print(f"Radiko: {name}() failed: {exc!r}")
        return None

    def _auth_with_radiko(self) -> Optional[str]:
        try:
            import requests  # type: ignore
        except Exception as exc:
            if DEBUG:
                print(f"Radiko: requests not available: {exc!r}")
            return None

        headers = {
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "X-Radiko-App": DEFAULT_RADIKO_APP,
            "X-Radiko-App-Version": DEFAULT_RADIKO_APP_VER,
            "X-Radiko-Device": DEFAULT_RADIKO_DEVICE,
            "X-Radiko-User": DEFAULT_RADIKO_USER,
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://radiko.jp",
            "Referer": "https://radiko.jp/",
        }
        if DEFAULT_RADIKO_COOKIE:
            headers["Cookie"] = DEFAULT_RADIKO_COOKIE
        try:
            res1 = None
            token = keylength = keyoffset = None
            auth1_urls = [u.strip() for u in DEFAULT_RADIKO_AUTH1_URLS.split(",") if u.strip()]
            for url in auth1_urls:
                res1 = requests.get(
                    url,
                    headers=headers,
                    timeout=5,
                    allow_redirects=True,
                )
                if res1.status_code in (404, 405) and DEBUG:
                    print(f"Radiko: auth1 {res1.status_code} ({url})")
                token = res1.headers.get("X-Radiko-AuthToken")
                keylength = res1.headers.get("X-Radiko-KeyLength")
                keyoffset = res1.headers.get("X-Radiko-KeyOffset")
                if token and keylength and keyoffset:
                    if DEBUG:
                        print(f"Radiko: auth1 ok ({url})")
                    break

            if not (token and keylength and keyoffset):
                if DEBUG and res1 is not None:
                    print(f"Radiko: auth1 status {res1.status_code}")
                    print(f"Radiko: auth1 headers {dict(res1.headers)}")
                    print(f"Radiko: auth1 body {res1.text[:200]!r}")
                    print(f"Radiko: auth1 url {res1.url}")
                    if "<!DOCTYPE html" in res1.text or "<html" in res1.text:
                        print("Radiko: auth1 returned HTML (likely blocked or redirected)")
                if DEBUG:
                    print("Radiko: auth1 missing headers")
                return None

            offset = int(keyoffset)
            length = int(keylength)
            authkey_bytes = DEFAULT_RADIKO_AUTHKEY.encode("ascii")
            partial = base64.b64encode(authkey_bytes[offset : offset + length]).decode("ascii")
            headers2 = dict(headers)
            headers2["X-Radiko-Authtoken"] = token
            headers2["X-Radiko-Partialkey"] = partial
            res2 = None
            auth2_urls = [u.strip() for u in DEFAULT_RADIKO_AUTH2_URLS.split(",") if u.strip()]
            for url in auth2_urls:
                res2 = requests.post(
                    url,
                    headers=headers2,
                    data=b"\r\n",
                    timeout=5,
                    allow_redirects=True,
                )
                if res2.status_code in (404, 405):
                    res2 = requests.get(
                        url,
                        headers=headers2,
                        timeout=5,
                        allow_redirects=True,
                    )
                if res2.status_code == 200:
                    if DEBUG:
                        print(f"Radiko: auth2 ok ({url})")
                    break
            if res2 is None or res2.status_code != 200:
                if DEBUG and res2 is not None:
                    print(f"Radiko: auth2 failed: {res2.status_code}")
                return None
            area_id = None
            if res2.text:
                head = res2.text.split(",")[0].strip()
                if head.startswith("JP"):
                    area_id = head
                else:
                    match = re.search(r"JP\\d{2}", res2.text)
                    if match:
                        area_id = match.group(0)
            if area_id:
                self._area_id = area_id

            self._auth_headers = {
                "Pragma": "no-cache",
                "Cache-Control": "no-cache",
                "X-Radiko-App": DEFAULT_RADIKO_APP,
                "X-Radiko-App-Version": DEFAULT_RADIKO_APP_VER,
                "X-Radiko-Device": DEFAULT_RADIKO_DEVICE,
                "X-Radiko-User": DEFAULT_RADIKO_USER,
                "User-Agent": "Mozilla/5.0",
                "Origin": "https://radiko.jp",
                "Referer": "https://radiko.jp/",
                "X-Radiko-Authtoken": token,
                "X-Radiko-AuthToken": token,
                "X-Radiko-Partialkey": partial,
            }
            if self._area_id:
                self._auth_headers["X-Radiko-AreaId"] = self._area_id
            if DEBUG:
                print("Radiko: auth2 ok")
            return token
        except Exception as exc:
            if DEBUG:
                print(f"Radiko: auth flow failed: {exc!r}")
            return None


class WorldRadioResolver:
    def __init__(self) -> None:
        self._base = "https://all.api.radio-browser.info/json"
        self._cache: List[Station] = []
        self._last_fetch = 0.0

    def random_station(self) -> Optional[Station]:
        stations = self._get_stations()
        if not stations:
            return None
        return random.choice(stations)

    def _get_stations(self) -> List[Station]:
        if self._cache and (time.time() - self._last_fetch) < DEFAULT_PROGRAM_REFRESH_SEC:
            return self._cache
        try:
            import requests  # type: ignore
        except Exception:
            return self._cache
        url = f"{self._base}/stations/search"
        params = {
            "limit": "200",
            "hidebroken": "true",
        }
        try:
            res = requests.get(url, params=params, timeout=10)
            if res.status_code != 200:
                if DEBUG:
                    print(f"WorldRadio: status {res.status_code} ({url})")
                return self._cache
            data = res.json()
            stations: List[Station] = []
            for item in data:
                name = str(item.get("name") or "").strip()
                stream = str(item.get("url_resolved") or item.get("url") or "").strip()
                if not name or not stream:
                    continue
                image_url = str(item.get("favicon") or "").strip()
                stations.append(Station(id=name, name=name, stream_url=stream, image_url=image_url))
            if stations:
                self._cache = stations
                self._last_fetch = time.time()
            return self._cache
        except Exception as exc:
            if DEBUG:
                print(f"WorldRadio: fetch failed: {exc!r}")
            return self._cache


class Player:
    def __init__(
        self,
        stations: List[Station],
        display: LineOutDisplay,
        buttons: ButtonInput,
        resolver: Optional[RadikoResolver],
    ) -> None:
        if not stations:
            raise ValueError("No stations configured")
        self._stations = stations
        self._display = display
        self._buttons = buttons
        self._resolver = resolver if resolver and resolver.available() else None
        self._world = WorldRadioResolver()
        self._index = 0
        self._mode = "radiko"
        self._last_meta = ""
        self._last_meta_at = 0.0
        self._program_title = ""
        self._program_image = None
        self._program_image_url = ""
        self._last_program_at = 0.0
        self._last_program_check_at = 0.0
        self._world_station: Optional[Station] = None
        self._world_history: List[Station] = []
        self._world_index = -1
        self._world_image = None
        self._world_image_url = ""
        self._program_fetching = False
        self._program_lock = threading.Lock()
        self._shutdown_confirm_at: Optional[float] = None
        self._state_path = os.getenv("RPLAYER_STATE", "state.json")
        self._stream_cache: Dict[str, str] = {}
        self._title_cache: Dict[str, Tuple[str, float]] = {}
        self._ffmpeg: Optional[subprocess.Popen] = None
        self._radiko_token: Optional[str] = None
        self._hydrate_station_names()
        self._load_radiko_token()
        self._load_state()

    def _hydrate_station_names(self) -> None:
        if not self._resolver:
            return
        for station in self._stations:
            if station.name:
                continue
            name = self._resolver.station_name(station.id)
            if name:
                station.name = name

    def current_station(self) -> Station:
        if self._mode == "world" and self._world_station:
            return self._world_station
        return self._stations[self._index]

    def next_station(self) -> None:
        if self._mode == "world":
            self._world_next()
            return
        if len(self._stations) < 2:
            self._display.show(self.current_station().name or self.current_station().id, "Only one station")
            return
        self._index = (self._index + 1) % len(self._stations)
        if DEBUG:
            current = self.current_station()
            print(f"Station next -> {self._index}: {current.id} {current.name}")
        self._start_current()
        self._save_state()

    def prev_station(self) -> None:
        if self._mode == "world":
            self._world_prev()
            return
        if len(self._stations) < 2:
            self._display.show(self.current_station().name or self.current_station().id, "Only one station")
            return
        self._index = (self._index - 1) % len(self._stations)
        if DEBUG:
            current = self.current_station()
            print(f"Station prev -> {self._index}: {current.id} {current.name}")
        self._start_current()
        self._save_state()

    def _start_current(self) -> None:
        station = self.current_station()
        label = station.name or station.id
        self._program_title = ""
        self._program_image = None
        self._program_image_url = ""
        self._last_program_at = 0.0
        self._world_image = None
        self._world_image_url = ""
        stream_url = station.stream_url
        if not stream_url and self._resolver:
            cached = self._stream_cache.get(station.id)
            if cached and "medialist" not in cached:
                stream_url = cached
            if not stream_url:
                stream_url = self._resolver.stream_url(station.id)
                if stream_url:
                    if "medialist" not in stream_url:
                        self._stream_cache[station.id] = stream_url

        if not stream_url:
            self._display.show(label, "stream_url missing")
            return

        self._stop_ffmpeg()
        if DEBUG:
            print(f"Play station {station.id} ({label}) -> {stream_url}")
        is_radiko_stream = bool(
            self._resolver
            and ("radiko" in stream_url or "smartstream" in stream_url)
            and self._mode == "radiko"
        )
        if stream_url.endswith(".m3u8") and not self._radiko_token:
            self._display.show(label, "radiko token missing")
            if DEBUG:
                print("Radiko: auth token missing, cannot play HLS")
            return
        if self._mode == "world":
            self._start_ffmpeg_plain(stream_url)
        elif self._radiko_token and (stream_url.endswith(".m3u8") or is_radiko_stream):
            self._start_ffmpeg(stream_url, self._radiko_token)
        else:
            play_stream(stream_url)
        self._display.show(label, "Loading...")
        self._last_meta = ""
        self._last_meta_at = 0.0
        if self._mode == "world":
            self._update_world_image()

    def tick(self) -> None:
        action = self._buttons.poll()
        if self._shutdown_confirm_at is not None:
            if action == "mode":
                self._shutdown_now()
                return
            if action in ("prev", "next", "shutdown"):
                self._shutdown_confirm_at = None
                action = None
            elif time.time() - self._shutdown_confirm_at >= DEFAULT_SHUTDOWN_CONFIRM_SEC:
                self._shutdown_confirm_at = None
            if self._shutdown_confirm_at is not None:
                self._display.show("Shutdown?", "Press X to confirm")
                return

        if action == "prev":
            self.prev_station()
        elif action == "next":
            self.next_station()
        elif action == "mode":
            self._toggle_mode()
        elif action == "shutdown":
            self._shutdown_confirm_at = time.time()
            self._display.show("Shutdown?", "Press X to confirm")
            return

        now = time.time()
        if self._mode == "radiko":
            # Refresh schedule occasionally, but check current program often.
            if now - self._last_program_at >= DEFAULT_PROGRAM_REFRESH_SEC:
                self._last_program_at = now
            if now - self._last_program_check_at >= DEFAULT_METADATA_SEC:
                self._kick_program_update()
                self._last_program_check_at = now
        if now - self._last_meta_at >= DEFAULT_METADATA_SEC:
            self._last_meta = self._get_title()
            self._last_meta_at = now

        current = self.current_station()
        line1 = current.name or current.id or "Station"
        if self._mode == "radiko":
            line2 = self._program_title or self._last_meta or "Now Playing"
            image = self._program_image
        else:
            line2 = self._last_meta or "World Radio"
            image = self._world_image
        self._display.show(line1, line2, image)

    def _get_title(self) -> str:
        station = self.current_station()
        if self._mode == "radiko" and self._resolver:
            cached = self._title_cache.get(station.id)
            if cached and time.time() - cached[1] < DEFAULT_METADATA_SEC:
                return cached[0]
            title = self._resolver.on_air_title(station.id)
            if title:
                self._title_cache[station.id] = (title, time.time())
                return title
        return get_mpd_title()

    def _kick_program_update(self) -> None:
        if not self._resolver:
            return
        with self._program_lock:
            if self._program_fetching:
                return
            self._program_fetching = True
        station_id = self.current_station().id

        def worker() -> None:
            try:
                program = self._resolver.current_program(station_id)
                if not program or self._mode != "radiko":
                    return
                if program.title:
                    self._program_title = program.title
                if not program.img_url or program.img_url == self._program_image_url:
                    return
                self._program_image_url = program.img_url
                try:
                    import requests  # type: ignore
                    from PIL import Image  # type: ignore

                    res = requests.get(program.img_url, timeout=5)
                    if res.status_code != 200:
                        if DEBUG:
                            print(f"Radiko: program image status {res.status_code}")
                        return
                    if self.current_station().id != station_id:
                        return
                    self._program_image = Image.open(io.BytesIO(res.content)).convert("RGB")
                except Exception as exc:
                    if DEBUG:
                        print(f"Radiko: program image fetch failed: {exc!r}")
            finally:
                with self._program_lock:
                    self._program_fetching = False

        threading.Thread(target=worker, daemon=True).start()

    def _load_radiko_token(self) -> None:
        if not self._resolver or not self._resolver.available():
            return
        self._radiko_token = self._resolver.auth_token()
        if DEBUG and self._radiko_token:
            print("Radiko: auth token loaded")

    def _toggle_mode(self) -> None:
        if self._mode == "radiko":
            self._mode = "world"
            if not self._world_station:
                self._world_next()
        else:
            self._mode = "radiko"
        if DEBUG:
            print(f"Mode -> {self._mode}")
        self._start_current()
        self._save_state()

    def _shutdown_now(self) -> None:
        if DEBUG:
            print("Shutdown: poweroff")
        try:
            subprocess.Popen(["sudo", "shutdown", "-h", "now"])
        except Exception:
            pass

    def _world_next(self) -> None:
        if self._world_index + 1 < len(self._world_history):
            self._world_index += 1
            self._world_station = self._world_history[self._world_index]
        else:
            station = self._world.random_station()
            if not station:
                return
            self._world_history.append(station)
            self._world_index = len(self._world_history) - 1
            self._world_station = station
        if DEBUG and self._world_station:
            print(f"WorldRadio: next -> {self._world_station.name}")
        self._start_current()
        self._save_state()

    def _world_prev(self) -> None:
        if self._world_index > 0:
            self._world_index -= 1
            self._world_station = self._world_history[self._world_index]
            if DEBUG and self._world_station:
                print(f"WorldRadio: prev -> {self._world_station.name}")
            self._start_current()
            self._save_state()
            return
        # If no previous history, pick a new random station.
        self._world_next()

    def _update_world_image(self) -> None:
        station = self.current_station()
        if not station.image_url:
            return
        if station.image_url == self._world_image_url:
            return
        self._world_image_url = station.image_url
        try:
            import requests  # type: ignore
            from PIL import Image  # type: ignore

            res = requests.get(station.image_url, timeout=5)
            if res.status_code != 200:
                if DEBUG:
                    print(f"WorldRadio: image status {res.status_code}")
                return
            self._world_image = Image.open(io.BytesIO(res.content)).convert("RGB")
        except Exception as exc:
            if DEBUG:
                print(f"WorldRadio: image fetch failed: {exc!r}")

    def _load_state(self) -> None:
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        mode = str(data.get("mode") or "").strip()
        if mode in ("radiko", "world"):
            self._mode = mode
        if self._mode == "radiko":
            station_id = str(data.get("station_id") or "").strip()
            if station_id:
                for idx, st in enumerate(self._stations):
                    if st.id == station_id:
                        self._index = idx
                        break
        else:
            name = str(data.get("world_name") or "").strip()
            url = str(data.get("world_url") or "").strip()
            image_url = str(data.get("world_image_url") or "").strip()
            if name and url:
                self._world_station = Station(id=name, name=name, stream_url=url, image_url=image_url)
            if not self._world_station:
                self._world_station = self._world.random_station()
            if self._world_station:
                self._world_history = [self._world_station]
                self._world_index = 0
        self._start_current()

    def _save_state(self) -> None:
        try:
            data = {"mode": self._mode}
            if self._mode == "radiko":
                data["station_id"] = self.current_station().id
            else:
                if self._world_station:
                    data["world_name"] = self._world_station.name
                    data["world_url"] = self._world_station.stream_url
                    data["world_image_url"] = self._world_station.image_url
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            return

    def _start_ffmpeg(self, url: str, token: str) -> None:
        headers_map: Dict[str, str] = {}
        if self._resolver:
            headers_map = self._resolver.auth_headers()
        if not headers_map:
            headers_map = {"X-Radiko-Authtoken": token}
        headers = "".join(f"{k}: {v}\\r\\n" for k, v in headers_map.items())
        cmd = [
            DEFAULT_FFMPEG,
            "-loglevel",
            "warning" if not DEBUG else "info",
            "-headers",
            headers,
            "-i",
            url,
            "-f",
            "alsa",
            "-ac",
            "2",
            "-ar",
            "48000",
            DEFAULT_ALSA_DEVICE,
        ]
        if DEBUG:
            print("ffmpeg:", " ".join(cmd))
        self._ffmpeg = subprocess.Popen(cmd)

    def _start_ffmpeg_plain(self, url: str) -> None:
        cmd = [
            DEFAULT_FFMPEG,
            "-loglevel",
            "warning" if not DEBUG else "info",
            "-i",
            url,
            "-f",
            "alsa",
            "-ac",
            "2",
            "-ar",
            "48000",
            DEFAULT_ALSA_DEVICE,
        ]
        if DEBUG:
            print("ffmpeg:", " ".join(cmd))
        self._ffmpeg = subprocess.Popen(cmd)

    def _stop_ffmpeg(self) -> None:
        if not self._ffmpeg:
            return
        try:
            self._ffmpeg.terminate()
            self._ffmpeg.wait(timeout=2)
        except Exception:
            try:
                self._ffmpeg.kill()
            except Exception:
                pass
        finally:
            self._ffmpeg = None


def run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def play_stream(url: str) -> None:
    if DEBUG:
        print(f"mpc play: {url}")
    run(["mpc", "clear"])
    run(["mpc", "add", url])
    run(["mpc", "play"])


def get_mpd_title() -> str:
    out = run(["mpc", "-f", "%title%", "current"])
    title = out.stdout.strip()
    if title:
        return title
    out = run(["mpc", "current"])
    return out.stdout.strip()


def mpd_is_available() -> bool:
    out = run(["mpc", "status"])
    return out.returncode == 0


def load_stations(path: str) -> List[Station]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    stations: List[Station] = []
    for item in raw:
        stations.append(
            Station(
                id=str(item.get("id", "")).strip(),
                name=str(item.get("name", "")).strip(),
                stream_url=str(item.get("stream_url", "")).strip(),
                image_url=str(item.get("image_url", "")).strip(),
            )
        )
    return stations


def _fit_image(image, max_width: int, max_height: int):
    if image is None:
        return None
    width, height = image.size
    if width == 0 or height == 0:
        return image
    scale = min(max_width / width, max_height / height, 1.0)
    new_w = max(1, int(width * scale))
    new_h = max(1, int(height * scale))
    if new_w == width and new_h == height:
        return image
    return image.resize((new_w, new_h))


def _fit_text(draw, font, text: str, max_width: int) -> str:
    if not text:
        return ""

    def text_width(value: str) -> int:
        if hasattr(draw, "textlength"):
            return int(draw.textlength(value, font=font))
        bbox = draw.textbbox((0, 0), value, font=font)
        return int(bbox[2] - bbox[0])

    if text_width(text) <= max_width:
        return text

    for idx in range(len(text), 0, -1):
        candidate = text[:idx] + "..."
        if text_width(candidate) <= max_width:
            return candidate
    return "..."


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def main() -> int:
    if os.getenv("RPLAYER_LIST_STATIONS") == "1":
        resolver = RadikoResolver()
        if not resolver.available():
            print("Radiko station list not available")
            return 1
        stations = [
            {"id": station_id, "name": resolver.station_name(station_id) or ""}
            for station_id in sorted(resolver._station_map.keys())
        ]
        path = os.getenv("RPLAYER_STATIONS", DEFAULT_STATIONS_FILE)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(stations, f, ensure_ascii=False, indent=2)
        print(f"Wrote {len(stations)} stations to {path}")
        return 0

    stations_file = os.getenv("RPLAYER_STATIONS", DEFAULT_STATIONS_FILE)
    try:
        stations = load_stations(stations_file)
    except Exception as exc:
        print(f"Failed to load stations: {exc}")
        return 1

    display = LineOutDisplay()
    buttons = ButtonInput(
        int(os.getenv("RPLAYER_BUTTON_A", DEFAULT_BUTTON_A_PIN)),
        int(os.getenv("RPLAYER_BUTTON_B", DEFAULT_BUTTON_B_PIN)),
        int(os.getenv("RPLAYER_BUTTON_X", DEFAULT_BUTTON_X_PIN)),
        int(os.getenv("RPLAYER_BUTTON_Y", DEFAULT_BUTTON_Y_PIN)),
    )
    resolver = RadikoResolver()
    if not mpd_is_available() and not resolver.auth_token():
        display.show("mpd not running", "start mpd")
        print("mpd is not running. Start it with: sudo systemctl enable --now mpd")
        return 1
    player = Player(stations, display, buttons, resolver)

    running = True

    def stop(_signo, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while running:
        player.tick()
        time.sleep(DEFAULT_REFRESH_SEC)

    player._stop_ffmpeg()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
