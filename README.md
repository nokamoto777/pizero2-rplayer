# pizero2-rplayer
Radiko player for Raspberry Pi Zero 2 + PIMORONI Line Out (64-bit Raspberry Pi OS trixie).

## Goal
- Raspberry Pi Zero 2 + PIMORONI Line Out
- Python-based radiko player
- Line Out display shows current station and song title while playing
- A/B buttons change station
- Fresh OS install (no prior setup)

## Hardware
- Raspberry Pi Zero 2
- PIMORONI Line Out (with screen + A/B buttons)
- microSD, power, optional case

## High-level design
Components:
- **Audio playback**: `mpd` plays the radiko stream URL.
- **Station control**: Python app manages station list, starts/stops mpd playback.
- **Metadata**: Python app polls radiko program info and updates the display.
- **UI**: A/B buttons -> previous/next station.
- **Display**: Show station name + current track or program title.

Data flow:
1) User presses A/B -> Python selects station.
2) Python resolves radiko stream URL.
3) Python updates mpd playlist and starts playback.
4) Python polls radiko now-playing metadata.
5) Python updates Line Out display.

Process:
- A single `rplayer.py` runs as a systemd service at boot.

## Software stack
- Python 3
- `mpd` + `mpc`
- `ffmpeg` (if needed for stream handling)
- Python libs:
  - `requests`
  - `lxml`
  - `Pillow`
  - `radiko.py`
  - `st7789`
  - `gpiozero`

## OS setup (fresh install)
Assume: Raspberry Pi OS 64-bit (trixie base), freshly installed.

1) Update OS and install dependencies:
```bash
sudo apt update
sudo apt -y full-upgrade
sudo apt -y install python3 python3-pip python3-venv git mpd mpc ffmpeg \
  python3-gpiozero python3-rpi.gpio python3-spidev python3-pil python3-numpy
```

2) Enable SPI (for Line Out) and reboot:
```bash
sudo raspi-config
```
- Interface Options -> SPI -> Enable
- Reboot

3) Enable the DAC in `/boot/config.txt` and reboot:
```
dtoverlay=hifiberry-dac
gpio=25=op,dh
```
Optional (if you are not using onboard audio):
```
dtparam=audio=off
```

4) Audio device config (Line Out):
- Ensure Line Out is the default audio output.
- If needed, set in `raspi-config`:
  - System Options -> Audio -> Select Line Out device

5) mpd basic config:
- Default config works, but ensure mpd output uses ALSA.
- Quick sanity check:
```bash
systemctl --user status mpd || systemctl status mpd
```
If inactive, enable system mpd:
```bash
sudo systemctl enable --now mpd
```

6) Python deps:
```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r requirements.txt
```
GPIOバックエンドはaptで入れるため、venvは `--system-site-packages` 推奨。

## Minimal prototype (fixed stream)
1) Edit `stations.json` and set `stream_url` for one station.
2) Run:
```bash
python3 rplayer.py
```
3) Verify audio output and display update.

## Radiko mode (auto stream resolve)
1) Put radiko station IDs into `stations.json` and leave `stream_url` empty.
2) Run:
```bash
python3 rplayer.py
```
3) If auth fails, test with a known stream URL first.

### Button pin config
Defaults in `rplayer.py`:
- A button: BCM 5
- B button: BCM 6

Line Out buttons are wired to BCM 5/6/16/24 (active low).  
If A/B are swapped, change the env vars below.

Override with env vars:
```bash
RPLAYER_BUTTON_A=5 RPLAYER_BUTTON_B=6 python3 rplayer.py
```

If GPIO libraries are not available, you can disable button handling:
```bash
RPLAYER_DISABLE_GPIO=1 python3 rplayer.py
```

### Debug output
Enable debug logs to the console:
```bash
RPLAYER_DEBUG=1 python3 rplayer.py
```

### Troubleshooting checklist
- `mpd` must be running (`sudo systemctl enable --now mpd`).
- SPI must be enabled and `/dev/spidev0.0` should exist.
- If display shows garbage, try a different rotation:
```bash
RPLAYER_ST7789_ROTATION=0 python3 rplayer.py
```
- Verify buttons with gpiozero:
```bash
python3 - <<'PY'
from gpiozero import Button
from time import sleep
a=Button(5,pull_up=True)
b=Button(6,pull_up=True)
print("Press A/B, Ctrl+C to exit")
while True:
    if a.is_pressed: print("A")
    if b.is_pressed: print("B")
    sleep(0.1)
PY
```

### Display pin config
Defaults are set for Pirate Audio Line Out. Override if needed:
```bash
RPLAYER_ST7789_ROTATION=90 \
RPLAYER_ST7789_PORT=0 \
RPLAYER_ST7789_CS=1 \
RPLAYER_ST7789_DC=9 \
RPLAYER_ST7789_BACKLIGHT=13 \
RPLAYER_ST7789_SPEED_HZ=80000000 \
python3 rplayer.py
```

## Application structure (proposed)
```
.
├── rplayer.py
├── stations.json
├── requirements.txt
├── services/
│   └── rplayer.service
└── README.md
```

### stations.json
- List of stations and IDs.
- Example:
```json
[
  {"id": "TBS", "name": "", "stream_url": ""}
]
```
`stream_url` should be the final playable URL (MPD can read it).  
If `stream_url` is empty, the app will try to resolve it via `radiko.py`.

### rplayer.py (core responsibilities)
- Load stations list
- Track current station index
- Button handlers:
  - A: previous station
  - B: next station
- Resolve radiko stream URL for selected station
- Control mpd (`mpc clear`, `mpc add`, `mpc play`)
- Poll now-playing metadata from radiko (fallback to mpd stream title)
- Update Line Out display with:
  - Station name
  - Song/Program title (truncate/scroll if needed)

### Display behavior
- Top line: Station name
- Second line: Song title / program name
- If metadata is unavailable: show station name + "Now Playing"
- If display libraries are not available, the app logs to console as a fallback.

## systemd service (proposed)
`services/rplayer.service`:
```
[Unit]
Description=Radiko Player
After=network-online.target mpd.service
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/pizero2-rplayer
ExecStart=/usr/bin/python3 /home/pi/pizero2-rplayer/rplayer.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Enable:
```bash
sudo cp services/rplayer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rplayer.service
sudo systemctl start rplayer.service
```

## Next steps (implementation plan)
1) Implement station list + selection logic.
2) Implement radiko auth and stream URL resolve.
3) Wire mpd control.
4) Implement Line Out display and button handling.
5) Add metadata polling + text scroll.

## Notes
- radiko authentication/stream URL logic should be cached and renewed periodically.
- Line Out screen is small; use short names or scrolling text.
- If mpd cannot play, test raw stream with `mpv` to debug.
- `radiko.py` is a third-party library and may break if radiko changes.
