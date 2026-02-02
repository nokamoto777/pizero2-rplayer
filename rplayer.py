#!/usr/bin/env python3
import base64
import json
import os
import queue
import signal
import subprocess
import time
import re
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

DEFAULT_STATIONS_FILE = "stations.json"
DEFAULT_MPD_HOST = "localhost"
DEFAULT_MPD_PORT = "6600"
DEFAULT_BUTTON_A_PIN = 5
DEFAULT_BUTTON_B_PIN = 6
DEFAULT_REFRESH_SEC = 1.0
DEFAULT_METADATA_SEC = 10.0
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
            speed_hz = _env_int("RPLAYER_ST7789_SPEED_HZ", 80_000_000)
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
            self._font = ImageFont.load_default()
            if DEBUG:
                print(f"Display init: {self._width}x{self._height}")
        except Exception:
            if DEBUG:
                print("Display init: failed")
            self._display = None

    def show(self, line1: str, line2: str) -> None:
        if not self._display:
            self._fallback.show(line1, line2)
            return

        try:
            assert self._draw and self._image and self._font
            self._draw.rectangle((0, 0, self._width, self._height), fill=(0, 0, 0))
            line1 = _fit_text(self._draw, self._font, line1, self._width - 4)
            line2 = _fit_text(self._draw, self._font, line2, self._width - 4)
            self._draw.text((2, 2), line1, font=self._font, fill=(255, 255, 255))
            self._draw.text((2, 18), line2, font=self._font, fill=(255, 255, 255))
            self._display.display(self._image)
        except Exception:
            self._fallback.show(line1, line2)


class ButtonInput:
    def __init__(self, a_pin: int, b_pin: int) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._btn_a = None
        self._btn_b = None
        if os.getenv("RPLAYER_DISABLE_GPIO") == "1":
            return
        self._init_gpio(a_pin, b_pin)

    def _init_gpio(self, a_pin: int, b_pin: int) -> None:
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

        try:
            self._btn_a = Button(a_pin, pull_up=True)
            self._btn_b = Button(b_pin, pull_up=True)
            self._btn_a.when_pressed = on_a
            self._btn_b.when_pressed = on_b
            if DEBUG:
                print(f"GPIO init: A=BCM{a_pin} B=BCM{b_pin}")
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
        self._auth_headers: Dict[str, str] = {}
        self._stream_url_cache: Dict[str, str] = {}
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

    @staticmethod
    def live_stream_url(station_id: str) -> str:
        return f"http://f-radiko.smartstream.ne.jp/{station_id}/_definst_/simul-stream.stream/playlist.m3u8"

    def stream_url(self, station_id: str) -> Optional[str]:
        if not self._client:
            return None
        cached = self._stream_url_cache.get(station_id)
        if cached:
            return cached

        url = self._stream_url_from_xml(station_id)
        if url:
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
                        {"station_id": station_id, "l": "15", "type": "b"},
                        {"station_id": station_id, "l": "15"},
                        {"station_id": station_id},
                    ]
                    for pcu_url in playlist_urls:
                        for params in param_variants:
                            playlist_url = self._with_query(pcu_url, params, token)
                            if DEBUG:
                                print(f"Radiko: playlist_create_url -> {playlist_url}")
                            m3u8 = self._fetch_playlist_m3u8(playlist_url, headers)
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
    def _with_query(url: str, extra: Dict[str, str], token: Optional[str]) -> str:
        parts = urlparse(url)
        query = dict(parse_qsl(parts.query))
        query.update(extra)
        if token and "auth_token" not in query:
            query["auth_token"] = token
        return urlunparse(parts._replace(query=urlencode(query)))

    def _fetch_playlist_m3u8(self, url: str, headers: Dict[str, str]) -> Optional[str]:
        try:
            import requests  # type: ignore
        except Exception:
            return None
        try:
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
            if DEBUG:
                print(f"Radiko: playlist no m3u8 for {url}")
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
                "X-Radiko-Partialkey": partial,
            }
            if DEBUG:
                print("Radiko: auth2 ok")
            return token
        except Exception as exc:
            if DEBUG:
                print(f"Radiko: auth flow failed: {exc!r}")
            return None


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
        self._index = 0
        self._last_meta = ""
        self._last_meta_at = 0.0
        self._stream_cache: Dict[str, str] = {}
        self._title_cache: Dict[str, Tuple[str, float]] = {}
        self._ffmpeg: Optional[subprocess.Popen] = None
        self._radiko_token: Optional[str] = None
        self._hydrate_station_names()
        self._load_radiko_token()

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
        return self._stations[self._index]

    def next_station(self) -> None:
        if len(self._stations) < 2:
            self._display.show(self.current_station().name or self.current_station().id, "Only one station")
            return
        self._index = (self._index + 1) % len(self._stations)
        if DEBUG:
            current = self.current_station()
            print(f"Station next -> {self._index}: {current.id} {current.name}")
        self._start_current()

    def prev_station(self) -> None:
        if len(self._stations) < 2:
            self._display.show(self.current_station().name or self.current_station().id, "Only one station")
            return
        self._index = (self._index - 1) % len(self._stations)
        if DEBUG:
            current = self.current_station()
            print(f"Station prev -> {self._index}: {current.id} {current.name}")
        self._start_current()

    def _start_current(self) -> None:
        station = self.current_station()
        label = station.name or station.id
        stream_url = station.stream_url
        if not stream_url and self._resolver:
            stream_url = self._stream_cache.get(station.id)
            if not stream_url:
                stream_url = self._resolver.stream_url(station.id)
                if stream_url:
                    self._stream_cache[station.id] = stream_url

        if not stream_url:
            self._display.show(label, "stream_url missing")
            return

        self._stop_ffmpeg()
        if DEBUG:
            print(f"Play station {station.id} ({label}) -> {stream_url}")
        if stream_url.endswith(".m3u8") and not self._radiko_token:
            self._display.show(label, "radiko token missing")
            if DEBUG:
                print("Radiko: auth token missing, cannot play HLS")
            return
        if self._radiko_token and stream_url.endswith(".m3u8"):
            self._start_ffmpeg(stream_url, self._radiko_token)
        else:
            play_stream(stream_url)
        self._display.show(label, "Loading...")
        self._last_meta = ""
        self._last_meta_at = 0.0

    def tick(self) -> None:
        action = self._buttons.poll()
        if action == "prev":
            self.prev_station()
        elif action == "next":
            self.next_station()

        now = time.time()
        if now - self._last_meta_at >= DEFAULT_METADATA_SEC:
            self._last_meta = self._get_title()
            self._last_meta_at = now

        current = self.current_station()
        line1 = current.name or current.id or "Station"
        line2 = self._last_meta if self._last_meta else "Now Playing"
        self._display.show(line1, line2)

    def _get_title(self) -> str:
        station = self.current_station()
        if self._resolver:
            cached = self._title_cache.get(station.id)
            if cached and time.time() - cached[1] < DEFAULT_METADATA_SEC:
                return cached[0]
            title = self._resolver.on_air_title(station.id)
            if title:
                self._title_cache[station.id] = (title, time.time())
                return title
        return get_mpd_title()

    def _load_radiko_token(self) -> None:
        if not self._resolver or not self._resolver.available():
            return
        self._radiko_token = self._resolver.auth_token()
        if DEBUG and self._radiko_token:
            print("Radiko: auth token loaded")

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
            )
        )
    return stations


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
    )
    resolver = RadikoResolver()
    if not mpd_is_available() and not resolver.auth_token():
        display.show("mpd not running", "start mpd")
        print("mpd is not running. Start it with: sudo systemctl enable --now mpd")
        return 1
    player = Player(stations, display, buttons, resolver)
    player._start_current()

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
