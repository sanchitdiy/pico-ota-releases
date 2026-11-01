import network
import time
import machine
import os
import json
import socket
import ssl
import struct
import device_config
from machine import Pin, UART

from device_config import SSID, PASSWORD, BLYNK_TOKEN, BLYNK_SERVER
import ota


# Application version shown in Blynk V8.
# This is separate from the integer OTA release number.
APP_VERSION = "20261101-5"

# Tank configuration
EMPTY_DISTANCE_MM = 2000
FULL_DISTANCE_MM = 20
TANK_CAPACITY_LITERS = 1000

# Sensor configuration
DEFAULT_INTERVAL_S = 10
SETTINGS_FILE = "tank-settings.json"
controls = None
display = None
_network_runtime = None
_clock_synced = False
_last_clock_attempt = None
_uptime_ms = 0
_uptime_tick = time.ticks_ms()
SAMPLES = 7
SENSOR_TIMEOUT_MS = 600
MIN_VALID_DISTANCE_MM = 20
MAX_VALID_DISTANCE_MM = 4500

MAX_SEND_FAILURES = 10
DEBUG = True

# Blynk:
# V1: Water level (%)
# V2: Distance (cm)
# V3: Water volume (litres)
# V5, V6: Filled water height (cm)
# V8: Application version (String)
# V7: Increase interval on either switch transition
# V17: Decrease interval on either switch transition
# V11: Saved monitoring pause (seconds)

led = Pin("LED", Pin.OUT)

uart = UART(
    0,
    baudrate=9600,
    tx=Pin(0),
    rx=Pin(1)
)

wlan = network.WLAN(network.STA_IF)


def update_uptime():
    global _uptime_ms, _uptime_tick
    now = time.ticks_ms()
    _uptime_ms += time.ticks_diff(now, _uptime_tick)
    _uptime_tick = now


def log(*parts):
    if _network_runtime is not None and _network_runtime.is_worker():
        _network_runtime.post_event(("log", parts))
        return
    update_uptime()
    if _clock_synced:
        # Keep the RTC in UTC; apply IST only when formatting output.
        stamp = time.gmtime(time.time() + 19800)
        prefix = "%04d-%02d-%02d %02d:%02d:%02d IST" % stamp[:6]
    else:
        prefix = "uptime %d.%03ds; clock unsynced" % (
            _uptime_ms // 1000, _uptime_ms % 1000
        )
    print("[%s] %s" % (prefix, " ".join(str(part) for part in parts)))


def sync_clock():
    global _clock_synced, _last_clock_attempt
    if not wlan.isconnected():
        return
    now = time.ticks_ms()
    retry_ms = 21600000 if _clock_synced else 300000
    if _last_clock_attempt is not None and time.ticks_diff(
        now, _last_clock_attempt
    ) < retry_ms:
        return
    _last_clock_attempt = now
    ota.feed()
    try:
        import ntptime
        ntptime.timeout = 2
        ntptime.settime()
        _clock_synced = True
        log("Clock synchronized over NTP")
    except Exception as exc:
        log("Clock sync unavailable:", type(exc).__name__)
    finally:
        ota.feed()



# Embedded TankUI glyph subset derived from DejaVu Sans.
# Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
# Upstream-Name: DejaVu fonts
# Upstream-Author: Stepan Roh <src@users.sourceforge.net> (original author),
#                   see /usr/share/doc/fonts-dejavu-core/AUTHORS for full list
# Source: https://dejavu-fonts.github.io/
#
# Files: *
# Copyright: Copyright (c) 2003 by Bitstream, Inc. All Rights Reserved.
#  Bitstream Vera is a trademark of Bitstream, Inc.
#  DejaVu changes are in public domain.
# License: bitstream-vera
#  Permission is hereby granted, free of charge, to any person obtaining a copy
#  of the fonts accompanying this license ("Fonts") and associated
#  documentation files (the "Font Software"), to reproduce and distribute the
#  Font Software, including without limitation the rights to use, copy, merge,
#  publish, distribute, and/or sell copies of the Font Software, and to permit
#  persons to whom the Font Software is furnished to do so, subject to the
#  following conditions:
#  .
#  The above copyright and trademark notices and this permission notice shall
#  be included in all copies of one or more of the Font Software typefaces.
#  .
#  The Font Software may be modified, altered, or added to, and in particular
#  the designs of glyphs or characters in the Fonts may be modified and
#  additional glyphs or characters may be added to the Fonts, only if the fonts
#  are renamed to names not containing either the words "Bitstream" or the word
#  "Vera".
#  .
#  This License becomes null and void to the extent applicable to Fonts or Font
#  Software that has been modified and is distributed under the "Bitstream
#  Vera" names.
#  .
#  The Font Software may be sold as part of a larger software package but no
#  copy of one or more of the Font Software typefaces may be sold by itself.
#  .
#  THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
#  OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF MERCHANTABILITY,
#  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF COPYRIGHT, PATENT,
#  TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL BITSTREAM OR THE GNOME
#  FOUNDATION BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, INCLUDING
#  ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL DAMAGES,
#  WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF
#  THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM OTHER DEALINGS IN THE
#  FONT SOFTWARE.
#  .
#  Except as contained in this notice, the names of Gnome, the Gnome
#  Foundation, and Bitstream Inc., shall not be used in advertising or
#  otherwise to promote the sale, use or other dealings in this Font Software
#  without prior written authorization from the Gnome Foundation or Bitstream
#  Inc., respectively. For further information, contact: fonts at gnome dot
#  org.
#

LCD_FONTS = {'title': {' ': (6, 7, 19, b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'A': (13, 14, 19, b'\x0f\x00\x0f\x80\x0f\x80\x1f\x80\x1d\xc09\xc09\xc08\xe0\x7f\xe0\x7f\xe0pp\xe0p\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'E': (11, 12, 19, b'\x7f\xc0\x7f\xc0p\x00p\x00p\x00\x7f\x80\x7f\x80p\x00p\x00p\x00\x7f\xc0\x7f\xc0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'K': (13, 14, 19, b'p\xf0q\xe0s\xc0w\x80\x7f\x00~\x00~\x00\x7f\x00s\x80q\xc0p\xe0pp\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'N': (14, 15, 19, b'xp|p|p~p~pwpsps\xf0q\xf0q\xf0p\xf0p\xf0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'R': (13, 14, 19, b'\x7f\x80\x7f\xc0q\xc0q\xc0q\xc0\x7f\x80\x7f\x80q\xc0q\xc0p\xe0p\xe0pp\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'T': (11, 12, 19, b'\xff\xe0\xff\xe0\x0e\x00\x0e\x00\x0e\x00\x0e\x00\x0e\x00\x0e\x00\x0e\x00\x0e\x00\x0e\x00\x0e\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'W': (18, 19, 19, b'a\xc3\x80q\xe3\x80q\xe3\x80q\xe7\x003g\x00;w\x00;7\x00;6\x00?>\x00\x1e>\x00\x1e\x1e\x00\x1e\x1e\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')}, 'number': {'%': (39, 40, 41, b'\x03\xf0\x00x\x00\x0f\xf8\x00x\x00\x1f\xfc\x00\xf0\x00>>\x00\xf0\x00<\x1f\x01\xe0\x00|\x1f\x03\xc0\x00|\x1f\x03\xc0\x00|\x1f\x07\x80\x00|\x1f\x07\x80\x00|\x1f\x0f\x00\x00<\x1f\x1e\x00\x00>>\x1e\x00\x00\x1f\xfc<\x00\x00\x0f\xf8<\x00\x00\x03\xf0x?\x00\x00\x00\xf8\x7f\xc0\x00\x00\xf0\xff\xe0\x00\x01\xe1\xf1\xf0\x00\x01\xe3\xe0\xf0\x00\x03\xc3\xe0\xf8\x00\x07\xc3\xe0\xf8\x00\x07\x83\xe0\xf8\x00\x0f\x03\xe0\xf8\x00\x0f\x03\xe0\xf8\x00\x1e\x03\xe0\xf0\x00>\x01\xf1\xf0\x00<\x00\xff\xe0\x00x\x00\x7f\xc0\x00x\x00?\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '-': (16, 17, 41, b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00?\xfc\x00?\xfc\x00?\xfc\x00?\xfc\x00?\xfc\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '0': (27, 28, 41, b'\x00?\x80\x00\x01\xff\xe0\x00\x03\xff\xf0\x00\x07\xff\xf8\x00\x0f\xff\xfc\x00\x0f\xe1\xfe\x00\x1f\xc0\xfe\x00\x1f\xc0\x7f\x00\x1f\xc0\x7f\x00?\x80\x7f\x00?\x80\x7f\x00?\x80\x7f\x00?\x80\x7f\x80?\x80\x7f\x80?\x80\x7f\x80?\x80\x7f\x80?\x80\x7f\x80?\x80\x7f\x00?\x80\x7f\x00?\x80\x7f\x00\x1f\xc0\x7f\x00\x1f\xc0\x7f\x00\x1f\xc0\xfe\x00\x0f\xe1\xfe\x00\x0f\xff\xfc\x00\x07\xff\xf8\x00\x03\xff\xf0\x00\x01\xff\xe0\x00\x00?\x80\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '1': (27, 28, 41, b'\x00\x00\x00\x00\x00\xff\xc0\x00\x0f\xff\xc0\x00\x0f\xff\xc0\x00\x0f\xff\xc0\x00\x0f\xff\xc0\x00\x0f\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x0f\xff\xff\x00\x0f\xff\xff\x00\x0f\xff\xff\x00\x0f\xff\xff\x00\x0f\xff\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '2': (27, 28, 41, b'\x00\xff\x80\x00\x0f\xff\xe0\x00\x1f\xff\xf0\x00\x1f\xff\xf8\x00\x1f\xff\xfc\x00\x1f\x07\xfc\x00\x1c\x01\xfe\x00\x10\x01\xfe\x00\x00\x00\xfe\x00\x00\x00\xfe\x00\x00\x00\xfe\x00\x00\x01\xfc\x00\x00\x01\xfc\x00\x00\x03\xf8\x00\x00\x07\xf8\x00\x00\x0f\xf0\x00\x00\x1f\xe0\x00\x00?\xc0\x00\x00\x7f\x80\x00\x00\xff\x00\x00\x01\xfe\x00\x00\x03\xfc\x00\x00\x07\xf8\x00\x00\x0f\xe0\x00\x00\x1f\xff\xfe\x00\x1f\xff\xfe\x00\x1f\xff\xfe\x00\x1f\xff\xfe\x00\x1f\xff\xfe\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '3': (27, 28, 41, b'\x00\xff\x80\x00\x07\xff\xf0\x00\x0f\xff\xf8\x00\x0f\xff\xfc\x00\x0f\xff\xfc\x00\x0e\x03\xfe\x00\x08\x01\xfe\x00\x00\x00\xfe\x00\x00\x00\xfe\x00\x00\x01\xfc\x00\x00\x03\xfc\x00\x01\xff\xf8\x00\x01\xff\xe0\x00\x01\xff\xf0\x00\x01\xff\xf8\x00\x01\xff\xfc\x00\x00\x03\xfe\x00\x00\x01\xfe\x00\x00\x00\xfe\x00\x00\x00\xfe\x00\x00\x00\xfe\x00\x00\x00\xfe\x00\x10\x01\xfe\x00\x1e\x03\xfe\x00\x1f\xff\xfc\x00\x1f\xff\xfc\x00\x1f\xff\xf8\x00\x1f\xff\xe0\x00\x07\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '4': (27, 28, 41, b'\x00\x00\x00\x00\x00\x0f\xf8\x00\x00\x0f\xf8\x00\x00\x1f\xf8\x00\x00?\xf8\x00\x00?\xf8\x00\x00\x7f\xf8\x00\x00\xff\xf8\x00\x00\xfb\xf8\x00\x01\xf3\xf8\x00\x01\xf3\xf8\x00\x03\xe3\xf8\x00\x07\xc3\xf8\x00\x07\xc3\xf8\x00\x0f\x83\xf8\x00\x1f\x03\xf8\x00\x1f\x03\xf8\x00>\x03\xf8\x00<\x03\xf8\x00?\xff\xff\x80?\xff\xff\x80?\xff\xff\x80?\xff\xff\x80?\xff\xff\x80\x00\x03\xf8\x00\x00\x03\xf8\x00\x00\x03\xf8\x00\x00\x03\xf8\x00\x00\x03\xf8\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '5': (27, 28, 41, b'\x00\x00\x00\x00\x0f\xff\xfc\x00\x0f\xff\xfc\x00\x0f\xff\xfc\x00\x0f\xff\xfc\x00\x0f\xff\xfc\x00\x0f\xc0\x00\x00\x0f\xc0\x00\x00\x0f\xc0\x00\x00\x0f\xc0\x00\x00\x0f\xff\x80\x00\x0f\xff\xe0\x00\x0f\xff\xf8\x00\x0f\xff\xfc\x00\x0f\xff\xfe\x00\x0f\x03\xfe\x00\x08\x01\xfe\x00\x00\x00\xff\x00\x00\x00\x7f\x00\x00\x00\x7f\x00\x00\x00\x7f\x00\x10\x00\xff\x00\x1c\x01\xfe\x00\x1f\x03\xfe\x00\x1f\xff\xfc\x00\x1f\xff\xfc\x00\x1f\xff\xf8\x00\x0f\xff\xe0\x00\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '6': (27, 28, 41, b'\x00\x0f\xe0\x00\x00\x7f\xfc\x00\x01\xff\xfe\x00\x03\xff\xfe\x00\x07\xff\xfe\x00\x07\xf8\x1e\x00\x0f\xf0\x00\x00\x0f\xe0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00\x1f\x8f\xc0\x00\x1f\xbf\xf0\x00\x1f\xff\xfc\x00?\xff\xfe\x00?\xff\xfe\x00?\xf0\xff\x00?\xe0\x7f\x00\x1f\xc0\x7f\x00\x1f\xc0\x7f\x00\x1f\xc0\x7f\x00\x1f\xc0\x7f\x00\x1f\xc0\x7f\x00\x0f\xe0\x7f\x00\x0f\xf0\xfe\x00\x07\xff\xfe\x00\x03\xff\xfc\x00\x01\xff\xf8\x00\x00\xff\xf0\x00\x00?\x80\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '7': (27, 28, 41, b'\x00\x00\x00\x00\x1f\xff\xfe\x00\x1f\xff\xfe\x00\x1f\xff\xfe\x00\x1f\xff\xfe\x00\x1f\xff\xfe\x00\x00\x00\xfe\x00\x00\x01\xfc\x00\x00\x01\xfc\x00\x00\x03\xf8\x00\x00\x03\xf8\x00\x00\x07\xf8\x00\x00\x07\xf0\x00\x00\x07\xf0\x00\x00\x0f\xe0\x00\x00\x0f\xe0\x00\x00\x1f\xc0\x00\x00\x1f\xc0\x00\x00?\x80\x00\x00?\x80\x00\x00\x7f\x00\x00\x00\x7f\x00\x00\x00\x7f\x00\x00\x00\xfe\x00\x00\x00\xfe\x00\x00\x01\xfc\x00\x00\x01\xfc\x00\x00\x03\xf8\x00\x00\x03\xf8\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '8': (27, 28, 41, b'\x00\x7f\xc0\x00\x03\xff\xf0\x00\x07\xff\xfc\x00\x0f\xff\xfc\x00\x1f\xff\xfe\x00\x1f\xe1\xfe\x00\x1f\xc0\xfe\x00\x1f\xc0~\x00\x1f\xc0~\x00\x1f\xc0\xfe\x00\x0f\xe1\xfe\x00\x07\xff\xfc\x00\x03\xff\xf0\x00\x01\xff\xe0\x00\x07\xff\xf8\x00\x0f\xff\xfc\x00\x1f\xe1\xfe\x00\x1f\xc0\x7f\x00\x1f\x80\x7f\x00?\x80\x7f\x00?\x80\x7f\x00?\x80\x7f\x00\x1f\xc0\x7f\x00\x1f\xe1\xff\x00\x1f\xff\xfe\x00\x0f\xff\xfe\x00\x07\xff\xfc\x00\x03\xff\xf0\x00\x00\x7f\xc0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '9': (27, 28, 41, b'\x00\x7f\x00\x00\x01\xff\xc0\x00\x07\xff\xf0\x00\x0f\xff\xf8\x00\x1f\xff\xfc\x00\x1f\xe1\xfc\x00\x1f\xc0\xfe\x00?\x80\xfe\x00?\x80\xfe\x00?\x80\xff\x00?\x80\xff\x00?\x80\xff\x00?\xc0\xff\x00\x1f\xe1\xff\x00\x1f\xff\xff\x00\x0f\xff\xff\x00\x07\xff\xff\x00\x03\xff\x7f\x00\x00\xfc\x7f\x00\x00\x00~\x00\x00\x00\xfe\x00\x00\x00\xfe\x00\x08\x01\xfc\x00\x0e\x07\xfc\x00\x0f\xff\xf8\x00\x0f\xff\xf0\x00\x0f\xff\xe0\x00\x07\xff\x80\x00\x01\xfe\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')}, 'height': {' ': (7, 8, 23, b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '-': (9, 10, 23, b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00~\x00~\x00~\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '.': (8, 9, 23, b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00<\x00<\x00<\x00<\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '0': (14, 15, 23, b'\x0f\x80\x1f\xe0<\xf08pxxxxxxxxxxxxxx8p<\xf0\x1f\xe0\x0f\x80\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '1': (14, 15, 23, b'\x0f\x80?\x803\x80\x03\x80\x03\x80\x03\x80\x03\x80\x03\x80\x03\x80\x03\x80\x03\x80\x03\x80\x03\x80?\xf8?\xf8\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '2': (14, 15, 23, b'\x1f\x80?\xe01\xf0\x00\xf0\x00\xf0\x00\xf0\x00\xf0\x01\xe0\x03\xc0\x07\x80\x0f\x00\x1e\x00<\x00?\xf0?\xf0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '3': (14, 15, 23, b'\x1f\xc0?\xe0!\xf0\x00\xf0\x00\xf0\x01\xe0\x0f\xc0\x0f\xe0\x01\xf0\x00\xf0\x00p\x00\xf0a\xf0\x7f\xe0\x1f\x80\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '4': (14, 15, 23, b'\x03\xe0\x03\xe0\x07\xe0\x0f\xe0\x0f\xe0\x1d\xe09\xe09\xe0q\xe0q\xe0\x7f\xf8\x7f\xf8\x01\xe0\x01\xe0\x01\xe0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '5': (14, 15, 23, b'?\xe0?\xe08\x008\x008\x00?\xc0?\xe0!\xf0\x00\xf0\x00p\x00p\x00\xf0!\xf0?\xe0\x1f\x80\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '6': (14, 15, 23, b'\x07\xe0\x0f\xf0\x1e\x10<\x008\x00{\xc0\x7f\xf0|\xf0xxxx8x8x<\xf0\x1f\xe0\x07\xc0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '7': (14, 15, 23, b'\x7f\xf0\x7f\xf0\x00\xf0\x00\xf0\x01\xe0\x01\xe0\x03\xc0\x03\xc0\x03\xc0\x07\x80\x07\x80\x0f\x00\x0f\x00\x0e\x00\x1e\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '8': (14, 15, 23, b'\x0f\xc0?\xf0<\xf08p8p<\xf0\x1f\xe0\x1f\xe0<\xf0xpxxxx<\xf0?\xf0\x0f\xc0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '9': (14, 15, 23, b'\x0f\x80\x1f\xe08\xe0x\xf0xpxxx\xf88\xf8?\xf8\x0fp\x00p\x00\xf0!\xe0?\xc0\x1f\x80\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'c': (12, 13, 23, b'\x00\x00\x00\x00\x00\x00\x00\x00\x0f\x80\x1f\xe0<\x00x\x00x\x00x\x00x\x00x\x00<\x00\x1f\xe0\x0f\x80\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'm': (21, 22, 23, b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x009\xc7\xc0?\xef\xe0<\xf9\xe08x\xe08p\xe08p\xe08p\xe08p\xe08p\xe08p\xe08p\xe0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')}, 'small': {' ': (4, 5, 14, b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '%': (12, 13, 14, b'q\x00\xdb\x00\xda\x00t\x00\x05\xc0\x0b`\x1b`\x11\xc0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '-': (5, 6, 14, b'\x00\x00\x00\x00pp\x00\x00\x00\x00\x00\x00\x00\x00'), '.': (5, 6, 14, b'\x00\x00\x00\x00\x00\x00``\x00\x00\x00\x00\x00\x00'), '/': (5, 6, 14, b'\x100  `@@\xc0\x80\x00\x00\x00\x00\x00'), '0': (8, 9, 14, b'<\x00d\x00f\x00f\x00f\x00f\x00f\x00<\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '1': (8, 9, 14, b'x\x00\x18\x00\x18\x00\x18\x00\x18\x00\x18\x00\x18\x00~\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '2': (8, 9, 14, b'|\x00\x0c\x00\x0e\x00\x0c\x00\x18\x000\x00p\x00~\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '3': (8, 9, 14, b'<\x00N\x00\x0c\x00<\x00\x0e\x00\x06\x00\x0e\x00<\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '4': (8, 9, 14, b'\x1c\x00\x1c\x00,\x00l\x00L\x00\xfe\x00\x0c\x00\x0c\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '5': (8, 9, 14, b'|\x00`\x00`\x00|\x00\x0e\x00\x06\x00\x0e\x00|\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '6': (8, 9, 14, b'>\x00`\x00`\x00|\x00f\x00f\x00f\x00<\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '7': (8, 9, 14, b'~\x00\x0e\x00\x0c\x00\x0c\x00\x18\x00\x18\x000\x000\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '8': (8, 9, 14, b'<\x00f\x00f\x00<\x00f\x00f\x00f\x00<\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '9': (8, 9, 14, b'8\x00l\x00f\x00n\x00>\x00\x06\x00\x0c\x00x\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), ':': (5, 6, 14, b'\x00\x00``\x00\x00``\x00\x00\x00\x00\x00\x00'), 'A': (9, 10, 14, b'\x1c\x00<\x00<\x006\x00f\x00\x7f\x00c\x00\xc3\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'B': (9, 10, 14, b'|\x00f\x00f\x00~\x00f\x00c\x00f\x00~\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'C': (9, 10, 14, b'\x1e\x00p\x00`\x00`\x00`\x00`\x00p\x00\x1e\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'D': (10, 11, 14, b'|\x00g\x00c\x00c\x80c\x80c\x00g\x00|\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'E': (8, 9, 14, b'~\x00`\x00`\x00|\x00`\x00`\x00`\x00~\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'F': (8, 9, 14, b'~\x00`\x00`\x00|\x00`\x00`\x00`\x00`\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'G': (10, 11, 14, b'\x1f\x00q\x00`\x00`\x00g\x00c\x00s\x00\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'H': (10, 11, 14, b'c\x00c\x00c\x00\x7f\x00c\x00c\x00c\x00c\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'I': (5, 6, 14, b'````````\x00\x00\x00\x00\x00\x00'), 'J': (5, 6, 14, b'`````````\xc0\x00\x00\x00\x00'), 'K': (9, 10, 14, b'c\x00f\x00|\x00x\x00x\x00l\x00f\x00c\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'L': (8, 9, 14, b'`\x00`\x00`\x00`\x00`\x00`\x00`\x00~\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'M': (11, 12, 14, b'q\xc0q\xc0{\xc0n\xc0n\xc0d\xc0`\xc0`\xc0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'N': (10, 11, 14, b's\x00s\x00{\x00{\x00o\x00g\x00g\x00c\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'O': (10, 11, 14, b'>\x00s\x00c\x80a\x80a\x80c\x80s\x00>\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'P': (9, 10, 14, b'|\x00f\x00g\x00f\x00|\x00`\x00`\x00`\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'Q': (10, 11, 14, b'>\x00s\x00c\x80a\x80a\x80c\x80s\x00>\x00\x06\x00\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'R': (9, 10, 14, b'|\x00f\x00f\x00f\x00|\x00f\x00f\x00c\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'S': (8, 9, 14, b'<\x00`\x00`\x00x\x00>\x00\x06\x00F\x00|\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'T': (8, 9, 14, b'\xfe\x00\x18\x00\x18\x00\x18\x00\x18\x00\x18\x00\x18\x00\x18\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'U': (9, 10, 14, b'c\x00c\x00c\x00c\x00c\x00c\x00g\x00>\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'V': (9, 10, 14, b'\xc3\x00c\x00g\x00f\x006\x00<\x00<\x00\x1c\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'W': (13, 14, 14, b'\xc60f`o`o`i`9\xc09\xc09\xc0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'X': (9, 10, 14, b'c\x00f\x00<\x00\x1c\x00\x1c\x00>\x00f\x00c\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'Y': (8, 9, 14, b'\xc7\x00f\x00<\x00<\x00\x18\x00\x18\x00\x18\x00\x18\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'Z': (8, 9, 14, b'~\x00\x0e\x00\x0c\x00\x1c\x008\x00p\x00`\x00\xfe\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'a': (8, 9, 14, b'\x00\x00\x00\x00|\x00\x0c\x00~\x00f\x00n\x00~\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'b': (8, 9, 14, b'`\x00`\x00|\x00f\x00f\x00f\x00f\x00|\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'c': (7, 8, 14, b'\x00\x00<````<\x00\x00\x00\x00\x00\x00'), 'd': (8, 9, 14, b'\x06\x00\x06\x00>\x00f\x00f\x00f\x00f\x00>\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'e': (8, 9, 14, b'\x00\x00\x00\x00<\x00f\x00~\x00`\x00`\x00>\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'f': (5, 6, 14, b'``\xf8`````\x00\x00\x00\x00\x00\x00'), 'g': (8, 9, 14, b'\x00\x00\x00\x00>\x00f\x00f\x00f\x00f\x00>\x00\x06\x00|\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'h': (8, 9, 14, b'`\x00`\x00|\x00f\x00f\x00f\x00f\x00f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'i': (4, 5, 14, b'`\x00``````\x00\x00\x00\x00\x00\x00'), 'j': (4, 5, 14, b'`\x00```````\xc0\x00\x00\x00\x00'), 'k': (8, 9, 14, b'`\x00`\x00f\x00l\x00x\x00x\x00l\x00f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'l': (4, 5, 14, b'````````\x00\x00\x00\x00\x00\x00'), 'm': (12, 13, 14, b'\x00\x00\x00\x00\x7f\xc0f\xc0f`f`f`f`\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'n': (8, 9, 14, b'\x00\x00\x00\x00|\x00f\x00f\x00f\x00f\x00f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'o': (8, 9, 14, b'\x00\x00\x00\x00<\x00f\x00f\x00f\x00f\x00<\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'p': (8, 9, 14, b'\x00\x00\x00\x00|\x00f\x00f\x00f\x00f\x00|\x00`\x00`\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'q': (8, 9, 14, b'\x00\x00\x00\x00>\x00f\x00f\x00f\x00f\x00>\x00\x06\x00\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'r': (6, 7, 14, b'\x00\x00x`````\x00\x00\x00\x00\x00\x00'), 's': (7, 8, 14, b'\x00\x00|`x|\x0cx\x00\x00\x00\x00\x00\x00'), 't': (6, 7, 14, b'``\xf8````8\x00\x00\x00\x00\x00\x00'), 'u': (8, 9, 14, b'\x00\x00\x00\x00f\x00f\x00f\x00f\x00f\x00>\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'v': (8, 9, 14, b'\x00\x00\x00\x00\xc6\x00d\x00l\x00<\x008\x008\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'w': (11, 12, 14, b'\x00\x00\x00\x00L\xc0l\x80o\x80\x7f\x803\x803\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'x': (8, 9, 14, b'\x00\x00\x00\x00l\x00|\x008\x008\x00l\x00f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'y': (8, 9, 14, b'\x00\x00\x00\x00\xc6\x00d\x00l\x00<\x008\x00\x18\x00\x10\x00p\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'z': (7, 8, 14, b'\x00\x00|\x1c8p`\xfc\x00\x00\x00\x00\x00\x00')}, 'foot': {' ': (4, 5, 13, b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), '-': (4, 5, 13, b'\x00\x00\x00\x00\xe0\x00\x00\x00\x00\x00\x00\x00\x00'), '.': (4, 5, 13, b'\x00\x00\x00\x00\x00\x00@\x00\x00\x00\x00\x00\x00'), '/': (4, 5, 13, b'   @@@\x80\x80\x00\x00\x00\x00\x00'), '0': (7, 8, 13, b'8HDDDH8\x00\x00\x00\x00\x00\x00'), '1': (7, 8, 13, b'p\x10\x10\x10\x10\x10x\x00\x00\x00\x00\x00\x00'), '2': (7, 8, 13, b'8H\x08\x080@x\x00\x00\x00\x00\x00\x00'), '3': (7, 8, 13, b'8H\x088\x08\x08x\x00\x00\x00\x00\x00\x00'), '4': (7, 8, 13, b'\x18(\x08H\xfc\x08\x08\x00\x00\x00\x00\x00\x00'), '5': (7, 8, 13, b'x@p\x08\x08\x08p\x00\x00\x00\x00\x00\x00'), '6': (7, 8, 13, b'8`@xLL8\x00\x00\x00\x00\x00\x00'), '7': (7, 8, 13, b'x\x08\x08\x10\x10  \x00\x00\x00\x00\x00\x00'), '8': (7, 8, 13, b'8HH8LLx\x00\x00\x00\x00\x00\x00'), '9': (7, 8, 13, b'pHL|\x0c\x08p\x00\x00\x00\x00\x00\x00'), ':': (4, 5, 13, b'\x00\x00@\x00\x00\x00@\x00\x00\x00\x00\x00\x00'), 'A': (7, 8, 13, b'\x10((@|D\x82\x00\x00\x00\x00\x00\x00'), 'B': (7, 8, 13, b'xDDxDDx\x00\x00\x00\x00\x00\x00'), 'C': (7, 8, 13, b'<`@@@`<\x00\x00\x00\x00\x00\x00'), 'D': (8, 9, 13, b'|\x00F\x00B\x00B\x00B\x00F\x00|\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'E': (7, 8, 13, b'|@@x@@|\x00\x00\x00\x00\x00\x00'), 'F': (6, 7, 13, b'x@@x@@@\x00\x00\x00\x00\x00\x00'), 'G': (8, 9, 13, b'<\x00b\x00@\x00N\x00B\x00b\x00<\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'H': (8, 9, 13, b'B\x00B\x00B\x00~\x00B\x00B\x00B\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'I': (3, 4, 13, b'@@@@@@@\x00\x00\x00\x00\x00\x00'), 'J': (3, 4, 13, b'@@@@@@@@\x80\x00\x00\x00\x00'), 'K': (7, 8, 13, b'DHp`PHD\x00\x00\x00\x00\x00\x00'), 'L': (6, 7, 13, b'@@@@@@|\x00\x00\x00\x00\x00\x00'), 'M': (9, 10, 13, b'c\x00c\x00E\x00U\x00Y\x00I\x00A\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'N': (8, 9, 13, b'b\x00b\x00R\x00R\x00J\x00N\x00F\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'O': (8, 9, 13, b'<\x00f\x00B\x00B\x00B\x00f\x00<\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'P': (7, 8, 13, b'xLLx@@@\x00\x00\x00\x00\x00\x00'), 'Q': (8, 9, 13, b'<\x00f\x00B\x00B\x00B\x00f\x00<\x00\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'R': (7, 8, 13, b'xLLxHDD\x00\x00\x00\x00\x00\x00'), 'S': (7, 8, 13, b'x@@8\x04\x0cx\x00\x00\x00\x00\x00\x00'), 'T': (7, 8, 13, b'\xfc\x10\x10\x10\x10\x10\x10\x00\x00\x00\x00\x00\x00'), 'U': (8, 9, 13, b'D\x00D\x00D\x00D\x00D\x00D\x008\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'V': (7, 8, 13, b'\x82DDH(8\x10\x00\x00\x00\x00\x00\x00'), 'W': (10, 11, 13, b'\x8c\x80L\x80L\x80P\x80\x13\x003\x003\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'), 'X': (7, 8, 13, b'D(8\x10(HD\x00\x00\x00\x00\x00\x00'), 'Y': (7, 8, 13, b'\x84H00\x10\x10\x10\x00\x00\x00\x00\x00\x00'), 'Z': (7, 8, 13, b'|\x04\x08\x10 @\xfc\x00\x00\x00\x00\x00\x00')}}

def _lcd_colour(r, g, b):
    value = ((r & 248) << 8) | ((g & 252) << 3) | (b >> 3)
    return ((value & 255) << 8) | (value >> 8)


def _lcd_width(_lcd_text, font):
    return sum(LCD_FONTS[font][ch][0] for ch in _lcd_text)


def _lcd_text(fb, value, x, y, font, ink):
    for ch in value:
        advance, w, h, data = LCD_FONTS[font][ch]
        stride = (w + 7) // 8
        for row in range(h):
            start = None
            for column in range(w + 1):
                lit = column < w and data[row * stride + column // 8] & (128 >> (column % 8))
                if lit and start is None:
                    start = column
                elif not lit and start is not None:
                    fb.hline(x + start, y + row, column - start, ink)
                    start = None
        x += advance


def _lcd_rounded(fb, x, y, w, h, radius, ink):
    for row in range(h):
        dy = radius - row - 1 if row < radius else row - (h - radius) if row >= h - radius else -1
        inset = radius - int((radius * radius - dy * dy) ** 0.5) if dy >= 0 else 0
        fb.hline(x + inset, y + row, w - inset * 2, ink)


def _lcd_render(fb, percent=75, height=148.5, interval=10, wifi=True, cloud=True,
           clock='14:35 IST', firmware='20261101-3'):
    bg = _lcd_colour(240, 247, 252)
    navy = _lcd_colour(15, 40, 67)
    secondary = _lcd_colour(56, 82, 105)
    line = _lcd_colour(192, 211, 226)
    white = _lcd_colour(255, 255, 255)
    pale = _lcd_colour(224, 238, 247)
    orange = _lcd_colour(245, 135, 25)
    amber = _lcd_colour(250, 203, 85)
    green = _lcd_colour(35, 179, 119)
    red = _lcd_colour(197, 47, 63)
    fb.fill(bg)
    title='WATER TANK'
    _lcd_text(fb,title,(240-_lcd_width(title,'title'))//2,9,'title',navy)
    fb.hline(12,32,216,line)
    # Cylinder-like outline with a modest cap and a _lcd_rounded inner chamber.
    _lcd_rounded(fb,25,40,38,7,3,navy)
    _lcd_rounded(fb,13,45,64,121,8,navy)
    _lcd_rounded(fb,15,47,60,117,6,white)
    _lcd_rounded(fb,17,49,56,113,5,pale)
    valid = percent is not None
    level=max(0,min(100,percent)) if valid else 0
    water=orange if level<40 else amber if level<60 else green
    fill_h=int(113*level/100)
    for row in range(113-fill_h,113):
        dy=row-(113-5) if row>=108 else -1
        inset=5-int((25-dy*dy)**0.5) if dy>=0 else 0
        fb.hline(17+inset,49+row,56-2*inset,water)
        if row<108:fb.hline(20,49+row,2,white)
    for mark in (40,80):
        y=162-int(113*mark/100)
        for x in range(17,73,6):fb.hline(x,y,min(3,73-x),navy)
        _lcd_text(fb,str(mark)+'%',80,y-5,'small',navy)
    label='LEVEL'
    _lcd_text(fb,label,145,45,'small',secondary)
    number=str(int(level+0.5))+'%' if valid else '--'
    _lcd_text(fb,number,109+(123-_lcd_width(number,'number'))//2,65,'number',navy)
    _lcd_text(fb,'FILLED HEIGHT',112,115,'small',secondary)
    htext='%.1f cm'%height if height is not None else '-- cm'
    _lcd_text(fb,htext,104+(128-_lcd_width(htext,'height'))//2,134,'height',navy)
    if not valid:
        _lcd_text(fb,'NO DATA',121,156,'small',red)
    # Compact, high-contrast cards rather than small faint outline-only labels.
    _lcd_rounded(fb,8,172,224,21,6,_lcd_colour(208,229,243))
    itext='Interval: %d s'%interval
    _lcd_text(fb,itext,(240-_lcd_width(itext,'small'))//2,177,'small',navy)
    for x,connected,label in ((8,wifi,'Wi-Fi'),(124,cloud,'Cloud')):
        _lcd_rounded(fb,x,198,108,19,5,white)
        _lcd_rounded(fb,x+5,204,5,5,2,green if connected else red)
        _lcd_text(fb,label+(' OK' if connected else ' OFF'),x+15,202,'small',navy)
    _lcd_text(fb,clock,9,226,'foot',secondary)
    version='FW '+firmware
    _lcd_text(fb,version,231-_lcd_width(version,'foot'),226,'foot',secondary)


class TankDisplay:
    """Optional ST7789 screen with a normal switch and GPIO-controlled backlight."""

    def __init__(self):
        self.enabled = getattr(device_config, "DISPLAY_ENABLED", False)
        self.rotation = getattr(device_config, "DISPLAY_ROTATION", 90)
        if self.rotation not in (0, 90, 180, 270):
            self.enabled = False
            log("Display disabled: invalid DISPLAY_ROTATION")
        self.percent = None
        self.height = None
        self.stage = "off"
        self.spi = None
        self.buffer = None
        self.fb = None
        self.row = 240
        self.key = None
        self.deadline = 0
        self.last_render = None
        self.showing = False
        self.raw_request = None
        self.request_since = 0
        self.stable_request = False
        if self.enabled:
            # Maintained switch: closed to GND requests ON. LCD VCC stays connected.
            self.switch = Pin(14, Pin.IN, Pin.PULL_UP)
            self.backlight = Pin(13, Pin.OUT, value=0)
            self._idle_pins()

    @staticmethod
    def colour(r, g, b):
        raw = ((r & 248) << 8) | ((g & 252) << 3) | (b >> 3)
        # Native RGB565 framebuffer is little-endian; ST7789 expects MSB first.
        return ((raw & 255) << 8) | (raw >> 8)

    def _idle_pins(self):
        # Supply stays on: hold reset low and keep active-low CS inactive.
        for number, level in ((9, 1), (8, 0), (10, 0), (11, 0), (12, 0)):
            Pin(number, Pin.OUT, value=level)
        Pin(28, Pin.IN, pull=None)

    def _off(self):
        self.backlight.value(0)
        if self.spi is not None:
            try:
                self._command(0x28)  # Display OFF.
                self._command(0x10)  # Sleep IN, then hold reset low below.
            except OSError:
                pass
            if hasattr(self.spi, "deinit"):
                try:
                    self.spi.deinit()
                except OSError:
                    pass
        self._idle_pins()
        self.spi = None
        self.fb = None
        self.buffer = None
        self.key = None
        self.last_render = None
        self.row = 240
        self.showing = False
        self.stage = "off"

    def update(self, percent, height):
        self.percent = percent
        self.height = height

    def _command(self, command, data=b""):
        self.cs.value(0)
        try:
            self.dc.value(0)
            self.spi.write(bytes((command,)))
            if data:
                self.dc.value(1)
                self.spi.write(data)
        finally:
            self.cs.value(1)

    def _start(self):
        import framebuf
        from machine import SPI
        self.cs = Pin(9, Pin.OUT, value=1)
        self.dc = Pin(8, Pin.OUT, value=0)
        self.reset = Pin(12, Pin.OUT, value=0)
        self.spi = SPI(
            1, baudrate=20000000, polarity=0, phase=0,
            sck=Pin(10), mosi=Pin(11), miso=Pin(28)
        )
        self.buffer = bytearray(240 * 240 * 2)
        self.fb = framebuf.FrameBuffer(self.buffer, 240, 240, framebuf.RGB565)
        self.stage = "reset-low"
        self.deadline = time.ticks_add(time.ticks_ms(), 20)

    def _initialize(self, now):
        if time.ticks_diff(now, self.deadline) < 0:
            return
        if self.stage == "start-wait":
            self._start()
        elif self.stage == "reset-low":
            self.reset.value(1)
            self.stage = "reset-high"
            self.deadline = time.ticks_add(now, 120)
        elif self.stage == "reset-high":
            self._command(0x01)  # Software reset.
            self.stage = "software-reset"
            self.deadline = time.ticks_add(now, 150)
        elif self.stage == "software-reset":
            self._command(0x11)  # Sleep out.
            self.stage = "sleep-out"
            self.deadline = time.ticks_add(now, 120)
        elif self.stage == "sleep-out":
            # ST7789 RGB565 initialization; verify panel orientation on hardware.
            for command, data in (
                (0x28, b""),
                (0x36, bytes(({0: 0x00, 90: 0x60, 180: 0xC0, 270: 0xA0}[self.rotation],))),
                (0x3A, b"\x55"),
                (0xB2, b"\x0c\x0c\x00\x33\x33"), (0xB7, b"\x35"),
                (0xBB, b"\x19"), (0xC0, b"\x2c"), (0xC2, b"\x01"),
                (0xC3, b"\x12"), (0xC4, b"\x20"), (0xC6, b"\x0f"),
                (0xD0, b"\xa4\xa1"), (0x21, b""), (0x13, b""),
            ):
                self._command(command, data)
            self.stage = "ready"
            self.key = None

    def _clock_text(self):
        if not _clock_synced:
            return "NO CLOCK"
        stamp = time.gmtime(time.time() + 19800)
        return "%02d:%02d IST" % (stamp[3], stamp[4])

    def _draw(self, wifi, online):
        interval = controls.settings.interval if controls is not None else DEFAULT_INTERVAL_S
        _lcd_render(
            self.fb, percent=self.percent, height=self.height, interval=interval,
            wifi=wifi, cloud=online, clock=self._clock_text(), firmware=APP_VERSION
        )

    def _frame(self):
        x = getattr(device_config, "DISPLAY_X_OFFSET", 0)
        y = getattr(device_config, "DISPLAY_Y_OFFSET", 0)
        self._command(0x2A, struct.pack(">HH", x, x + 239))
        self._command(0x2B, struct.pack(">HH", y, y + 239))
        self._command(0x2C)
        self.row = 0

    def poll(self):
        if not self.enabled:
            return
        try:
            now = time.ticks_ms()
            raw_request = self.switch.value() == 0
            if raw_request != self.raw_request:
                self.raw_request = raw_request
                self.request_since = now
            if time.ticks_diff(now, self.request_since) >= 50:
                self.stable_request = self.raw_request
            requested = self.stable_request
            if not requested:
                if self.stage != "off":
                    self._off()
                return
            if self.stage == "error":
                return  # Switch OFF then ON to retry a failed initialization.
            if self.stage == "off":
                self.backlight.value(0)
                self.stage = "start-wait"
                self.deadline = time.ticks_add(now, 20)
                return
            if self.stage != "ready":
                self._initialize(now)
                return
            if self.row < 240:
                # One <=11,520-byte transfer per call: ~4.6 ms at 20 MHz.
                end = min(240, self.row + 24)
                self.cs.value(0)
                try:
                    self.dc.value(1)
                    self.spi.write(memoryview(self.buffer)[self.row * 480:end * 480])
                finally:
                    self.cs.value(1)
                self.row = end
                if end == 240 and not self.showing:
                    self._command(0x29)  # Show only after the first full frame.
                    self.showing = True
                    self.backlight.value(1)
                return
            wifi = controls.wifi_ready if controls is not None and hasattr(controls, "wifi_ready") else wlan.isconnected()
            online = bool(wifi and controls is not None and controls.ready)
            interval = controls.settings.interval if controls is not None else DEFAULT_INTERVAL_S
            key = (self.percent, self.height, wifi, online, interval, self._clock_text())
            if key == self.key:
                return
            if self.last_render is not None and time.ticks_diff(now, self.last_render) < 1000:
                return
            self._draw(wifi, online)
            self._frame()
            self.key = key
            self.last_render = now
        except Exception as exc:
            self._off()
            self.stage = "error"
            log("Display disabled after error:", type(exc).__name__)


class IntervalSettings:
    """Device-local settings survive app-only OTA releases."""

    def __init__(self):
        self.interval = DEFAULT_INTERVAL_S
        try:
            with open(SETTINGS_FILE) as source:
                saved = json.load(source)
                if saved.get("interval_schema") != 2:
                    raise ValueError("Reset legacy interval once")
                value = saved["interval_s"]
            if type(value) is not int or value < 1:
                raise ValueError("Invalid saved interval")
            self.interval = max(2, ((value + 1) // 2) * 2)
            if self.interval != value:
                self._save(self.interval)
        except (OSError, ValueError, KeyError, TypeError):
            log("Interval: reset to default", self.interval, "seconds")
            self._save(self.interval)
        self.states = {}

    def _save(self, interval):
        temporary = SETTINGS_FILE + ".tmp"
        with open(temporary, "w") as target:
            json.dump({"interval_s": interval, "interval_schema": 2}, target)
            target.flush()
        os.rename(temporary, SETTINGS_FILE)
        os.sync()

    def reset_switches(self):
        # First value after each login is a baseline, never a click.
        self.states = {}

    def toggle(self, pin, value):
        if pin not in (7, 17) or value not in ("0", "1"):
            return False
        previous = self.states.get(pin)
        if previous is None or previous == value:
            self.states[pin] = value
            return False
        interval = max(2, self.interval + (2 if pin == 7 else -2))
        if interval != self.interval:
            self._save(interval)
            self.interval = interval
            log("Saved interval:", interval, "seconds")
        self.states[pin] = value
        return True


class BlynkControls:
    """Blynk binary protocol over TLS; receive both edges without HTTP polling."""

    def __init__(self, settings, on_command=None, on_ready=None):
        self.settings = settings
        self.on_command = on_command
        self.on_ready = on_ready
        self.connection = None
        self.buffer = b""
        self.outgoing = b""
        self.ready = False
        self.message_id = 0
        self.login_id = None
        self.last_attempt = None
        self.last_rx = 0
        self.last_ping = 0
        self.restart_state = None
        self.restart_pending = False
        self.feedback_pending = True
        self.version_pending = True
        self.server = BLYNK_SERVER
        self.stage = "disconnected"
        self.last_command = None
        self.last_status = None

    @staticmethod
    def _would_block(exc):
        for name in ("SSLWantReadError", "SSLWantWriteError"):
            error_type = getattr(ssl, name, None)
            if isinstance(error_type, type) and isinstance(exc, error_type):
                return True
        code = exc.args[0] if exc.args else None
        # EAGAIN, platform ETIMEDOUT, and mbedTLS WANT_READ/WANT_WRITE.
        return code in (11, 110, 116, -26880, -26752)

    def _flush(self):
        while self.outgoing:
            ota.feed()
            try:
                count = self.connection.write(self.outgoing)
            except OSError as exc:
                if self._would_block(exc):
                    return
                raise
            if count is None:
                return
            if count == 0:
                raise OSError(104, "Blynk write closed")
            self.outgoing = self.outgoing[count:]

    def _send(self, command, body=b""):
        self.message_id = self.message_id % 65535 + 1
        packet = struct.pack(">BHH", command, self.message_id, len(body)) + body
        if len(self.outgoing) + len(packet) > 4096:
            raise OSError(105, "Blynk output queue full")
        self.outgoing += packet
        self._flush()
        return self.message_id

    def virtual_write(self, pin, value):
        if not self.ready:
            return False
        self._send(20, ("vw\x00%d\x00%s" % (pin, value)).encode())
        return True

    def publish_version(self):
        if not self.ready:
            return False
        if self.version_pending:
            self.virtual_write(8, APP_VERSION)
            self.version_pending = False
            log("Blynk v8 queued:", APP_VERSION)
        return True

    def _disconnect(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
        self.connection = None
        self.ready = False
        self.buffer = b""
        self.outgoing = b""
        self.feedback_pending = True
        self.version_pending = True
        self.restart_state = None
        self.restart_pending = False
        self.settings.reset_switches()

    def _connect(self):
        self.last_attempt = time.ticks_ms()
        raw = socket.socket()
        try:
            self.stage = "dns"
            raw.settimeout(3)
            address = socket.getaddrinfo(
                self.server, 443, 0, socket.SOCK_STREAM
            )[0][-1]
            ota.feed()
            self.stage = "tcp"
            raw.connect(address)
            ota.feed()
            # MicroPython SSLSocket has no settimeout(); TLS uses this socket.
            raw.settimeout(2)
            # Keep the original hostname for TLS. Do not override trust checks.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self.stage = "tls"
            self.connection = context.wrap_socket(
                raw, server_hostname=self.server
            )
            ota.feed()
            # TLS may buffer decrypted bytes even when the TCP fd isn't readable.
            # Read it directly in nonblocking mode; None means idle, not EOF.
            self.connection.setblocking(False)
            # Blynk hardware login is 29; command 2 is not device login.
            self.message_id = 0
            self.stage = "login"
            self.last_command = None
            self.last_status = None
            self.login_id = self._send(29, BLYNK_TOKEN.encode())
            self.last_rx = time.ticks_ms()
            self.last_ping = self.last_rx
        except Exception:
            if self.connection is None:
                raw.close()
            self._disconnect()
            raise

    def _handle(self, command, message_id, body, status=None):
        self.last_command = command
        self.last_status = status
        if command == 0:
            if message_id == self.login_id:
                if status != 200:
                    raise OSError(status, "Blynk control login rejected")
                self.login_id = None
                self.ready = True
                self.stage = "online"
                self.settings.reset_switches()
                if self.on_ready is not None:
                    self.on_ready()
                self._send(
                    17,
                    b"ver\x00tank-pico-1\x00h-beat\x0060"
                    b"\x00buff-in\x001024\x00dev\x00rp2-py"
                )
                # Blynk MSG_HW_SYNC is 16, as used by sync_virtual().
                self._send(16, b"vr\x007\x0017\x009")
                self.feedback_pending = True
                log("Blynk interval controls connected")
            return
        if command == 41:
            # Cloud can redirect a valid token to its home regional server.
            parts = body.split(b"\x00")
            if len(parts) != 2:
                raise ValueError("Invalid Blynk redirect")
            server = parts[0].decode()
            if (
                not server.endswith(".blynk.cloud")
                or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in server)
                or parts[1] != b"443"
            ):
                raise ValueError("Unsupported Blynk redirect")
            self.server = server
            self._disconnect()
            self.last_attempt = None
            log("Blynk regional redirect:", server)
            return
        if command == 6:
            self.outgoing += struct.pack(">BHH", 0, message_id, 200)
            self._flush()
            return
        if command != 20 or not self.ready:
            return
        parts = body.split(b"\x00")
        if len(parts) != 3 or parts[0] != b"vw":
            return
        try:
            pin = int(parts[1])
            value = parts[2].decode()
        except (ValueError, UnicodeError):
            return
        if self.on_command is not None:
            if pin in (7, 17, 9) and value in ("0", "1"):
                self.on_command(pin, value)
            return
        if pin == 9:
            if value in ("0", "1"):
                if self.restart_state == "0" and value == "1":
                    self.restart_pending = True
                self.restart_state = value
            return
        if self.settings.toggle(pin, value):
            self.feedback_pending = True

    def _receive(self):
        try:
            data = self.connection.read(1024)
        except OSError as exc:
            if self._would_block(exc):
                return
            raise
        if data is None:
            return
        if data == b"":
            raise OSError(104, "Blynk control connection closed")
        self.last_rx = time.ticks_ms()
        self.buffer += data
        while len(self.buffer) >= 5:
            command, message_id, length = struct.unpack(">BHH", self.buffer[:5])
            if message_id == 0:
                raise ValueError("Invalid Blynk message ID")
            if command == 0:
                self.buffer = self.buffer[5:]
                self._handle(command, message_id, b"", status=length)
                continue
            if length > 1024:
                raise ValueError("Blynk control frame too large")
            if len(self.buffer) < 5 + length:
                break
            body = self.buffer[5:5 + length]
            self.buffer = self.buffer[5 + length:]
            self._handle(command, message_id, body)
            if self.connection is None:
                return

    def pump(self):
        if not wlan.isconnected():
            self._disconnect()
            return
        try:
            now = time.ticks_ms()
            if self.connection is None:
                if self.last_attempt is not None and time.ticks_diff(
                    now, self.last_attempt
                ) < 15000:
                    return
                self._connect()
            self._flush()
            self._receive()
            if self.connection is None:
                return
            now = time.ticks_ms()
            deadline = 90000 if self.ready else 10000
            if time.ticks_diff(now, self.last_rx) > deadline:
                raise OSError(110, "Blynk login/heartbeat timeout")
            if self.ready:
                if time.ticks_diff(now, self.last_ping) >= 10000:
                    self._send(6)
                    self.last_ping = now
                if self.feedback_pending:
                    self.virtual_write(11, self.settings.interval)
                    self.feedback_pending = False
                self.publish_version()
                if self.restart_pending:
                    log("Remote restart requested on V9")
                    self.restart_pending = False
                    machine.reset()
            self._flush()
        except Exception as exc:
            # Never log token-bearing network payloads or URLs.
            code = exc.args[0] if exc.args and type(exc.args[0]) is int else None
            log(
                "Blynk connection failed: stage=%s type=%s code=%s command=%s status=%s"
                % (self.stage, type(exc).__name__, code,
                   self.last_command, self.last_status)
            )
            self._disconnect()
        finally:
            ota.feed()


class _LocalLock:
    def acquire(self, wait=True):
        return True

    def release(self):
        pass


class _NetworkSettings:
    """Worker-owned projection; never writes settings files."""
    def __init__(self, interval):
        self.interval = interval

    def reset_switches(self):
        pass


class NetworkRuntime:
    """Main owns sensing/settings/SPI. One worker exclusively owns networking."""
    def __init__(self, settings):
        try:
            import _thread
            self.thread = _thread
        except ImportError:
            self.thread = None
        self.main_id = self.thread.get_ident() if self.thread else None
        self.lock = self.thread.allocate_lock() if self.thread else _LocalLock()
        self.storage = self.thread.allocate_lock() if self.thread else _LocalLock()
        self.settings = settings
        self._interval = settings.interval
        self._ready = False
        self.wifi_ready = False
        self.events = []
        self.overflow = False
        self._latest = (0, None)
        self._sequence = 0
        self._sent_sequence = -1
        self.restart_state = None
        self.allow_ota = False
        self.ota_ready = False
        self.running = False
        self.client = None
        self.last_wifi_attempt = None
        self.last_wifi_observation = None
        self.last_ota_attempt = None

    @property
    def ready(self):
        if not self.wifi_ready or not self._ready:
            return False
        # Show stale cloud state as offline even if a worker request is delayed.
        return self.client is not None and time.ticks_diff(
            time.ticks_ms(), self.client.last_rx
        ) <= 90000

    def is_worker(self):
        return self.thread is not None and self.thread.get_ident() != self.main_id

    def post_event(self, event):
        self.lock.acquire()
        try:
            if len(self.events) >= 64:
                self.overflow = True
                return False
            self.events.append(event)
            return True
        finally:
            self.lock.release()

    def _command(self, pin, value):
        if not self.post_event(("command", pin, value)):
            raise OSError(105, "Control queue overflow")

    def _connected(self):
        self._sent_sequence = -1
        if not self.post_event(("baseline",)):
            raise OSError(105, "Control queue overflow")

    def submit_telemetry(self, values):
        self.lock.acquire()
        try:
            self._sequence += 1
            # A single immutable snapshot replaces any older unsent reading.
            self._latest = (self._sequence, values)
        finally:
            self.lock.release()

    def invalidate_telemetry(self):
        self.submit_telemetry(None)

    def pump(self):
        """Main-thread service: no DNS, connect, TLS, HTTP or retry sleeps."""
        now = time.ticks_ms()
        if self.last_wifi_observation is None or time.ticks_diff(
            now, self.last_wifi_observation
        ) >= 500:
            # Local radio status query only; no request or connection attempt.
            self.wifi_ready = wlan.isconnected()
            self.last_wifi_observation = now
        if not self.storage.acquire(False):
            return  # OTA owns flash; defer commands, never pause local sensing.
        try:
            self.lock.acquire()
            try:
                events = self.events
                self.events = []
                overflow = self.overflow
                self.overflow = False
            finally:
                self.lock.release()
            if overflow:
                self.settings.reset_switches()
                self.restart_state = None
                log("Control queue overflow; ignoring batch and resynchronizing")
                events = [event for event in events if event[0] == "log"]
            for event in events:
                if event[0] == "log":
                    log(*event[1])
                elif event[0] == "baseline":
                    self.settings.reset_switches()
                    self.restart_state = None
                elif event[0] == "command":
                    pin, value = event[1:]
                    if pin == 9:
                        if self.restart_state == "0" and value == "1":
                            self.running = False
                            log("Remote restart requested on V9")
                            machine.reset()
                        self.restart_state = value
                    else:
                        self.settings.toggle(pin, value)
            self.lock.acquire()
            try:
                self._interval = self.settings.interval
            finally:
                self.lock.release()
        finally:
            self.storage.release()

    def start(self):
        if self.thread is None:
            log("Cannot start isolated networking: _thread unavailable")
            return False
        original_feed = ota.feed
        def main_feed_only():
            # A blocked/hung main loop must not be hidden by worker watchdog feeds.
            if self.thread.get_ident() == self.main_id:
                original_feed()
        ota.feed = main_feed_only
        try:
            self.thread.stack_size(16384)
            self.running = True
            self.thread.start_new_thread(self._run, ())
            log("Network worker started; local monitoring does not wait for Wi-Fi")
            return True
        except Exception as exc:
            self.running = False
            ota.feed = original_feed
            log("Network worker unavailable:", type(exc).__name__)
            return False

    def stop(self):
        # The worker closes its own sockets after any in-flight call returns.
        self.running = False
        self._ready = False

    def _wifi_tick(self):
        self.wifi_ready = wlan.isconnected()
        if self.wifi_ready:
            return
        self._ready = False
        if self.client.connection is not None:
            self.client._disconnect()
        now = time.ticks_ms()
        if self.last_wifi_attempt is not None and time.ticks_diff(
            now, self.last_wifi_attempt
        ) < 30000:
            return
        self.last_wifi_attempt = now
        wlan.active(True)
        wlan.disconnect()
        wlan.connect(SSID, PASSWORD)
        log("Background Wi-Fi connection requested")

    def step(self):
        """Worker-only; blocking network calls never hold the shared-state lock."""
        if self.client is None:
            self.client = BlynkControls(
                _NetworkSettings(self._interval),
                on_command=self._command, on_ready=self._connected
            )
        self._wifi_tick()
        if not self.wifi_ready:
            return
        self.lock.acquire()
        try:
            interval = self._interval
        finally:
            self.lock.release()
        if self.client.settings.interval != interval:
            self.client.settings.interval = interval
            self.client.feedback_pending = True
        self.client.pump()
        self._ready = self.client.ready
        if not self._ready:
            return
        # Connect/handshake may have taken time. Re-read after it, not before it.
        self.lock.acquire()
        try:
            sequence, values = self._latest
        finally:
            self.lock.release()
        # Do not append repeated historical measurements under backpressure.
        # One existing in-flight batch may complete; the next batch is latest only.
        if values is not None and sequence != self._sent_sequence and not self.client.outgoing:
            percent, distance, liters, height = values
            for pin, value in ((1, percent), (2, distance), (3, liters), (5, height), (6, height)):
                self.client.virtual_write(pin, "%.1f" % value)
            self._sent_sequence = sequence
        sync_clock()
        if not self.allow_ota or self.ota_ready:
            return
        now = time.ticks_ms()
        if self.last_ota_attempt is not None and time.ticks_diff(
            now, self.last_ota_attempt
        ) < getattr(device_config, "OTA_INTERVAL_S", 300) * 1000:
            return
        self.last_ota_attempt = now
        if not getattr(device_config, "OTA_RSA_N", ""):
            return
        self.storage.acquire()
        try:
            # Existing updater retains signature/hash verification and its journal.
            # Do not use ota.poll(): reset must be requested on the main thread.
            self.ota_ready = ota.check_update()
        except Exception as exc:
            log("Background OTA check failed:", type(exc).__name__)
        finally:
            self.storage.release()

    def _run(self):
        try:
            while self.running:
                try:
                    self.step()
                except Exception as exc:
                    self._ready = False
                    if self.client is not None:
                        self.client._disconnect()
                    log("Network worker retry:", type(exc).__name__)
                time.sleep_ms(50)
        finally:
            self._ready = False
            if self.client is not None:
                self.client._disconnect()


def service_controls():
    update_uptime()
    ota.feed()
    if controls is not None:
        controls.pump()
    if display is not None:
        display.poll()
    ota.feed()


def wait_interval():
    # Re-evaluate the interval as switches change; keep the watchdog alive.
    elapsed_ms = 0
    previous = time.ticks_ms()
    while elapsed_ms < controls.settings.interval * 1000:
        service_controls()
        now = time.ticks_ms()
        elapsed_ms += time.ticks_diff(now, previous)
        previous = now
        if elapsed_ms < controls.settings.interval * 1000:
            time.sleep_ms(100)


def read_distance_mm():
    # Clear old UART data.
    while uart.any():
        uart.read()

    buf = b""
    end = time.ticks_add(time.ticks_ms(), SENSOR_TIMEOUT_MS)

    while time.ticks_diff(end, time.ticks_ms()) > 0:
        if uart.any():
            data = uart.read()
            if data:
                buf += data

        i = buf.find(b"\xff")

        while i != -1 and len(buf) - i >= 4:
            high = buf[i + 1]
            low = buf[i + 2]
            checksum = buf[i + 3]

            expected = (0xFF + high + low) & 0xFF

            if expected == checksum:
                distance = high * 256 + low

                if (
                    MIN_VALID_DISTANCE_MM
                    <= distance
                    <= MAX_VALID_DISTANCE_MM
                ):
                    return distance

            i = buf.find(b"\xff", i + 1)

        buf = buf[-8:]
        service_controls()
        time.sleep_ms(10)

    return None


def get_distance_mm():
    values = []

    for _ in range(SAMPLES):
        distance = read_distance_mm()

        if distance is not None:
            values.append(distance)

        service_controls()
        time.sleep_ms(100)

    if not values:
        return None

    values.sort()
    return values[len(values) // 2]


def calculate_water_level(distance):
    span = EMPTY_DISTANCE_MM - FULL_DISTANCE_MM

    if span <= 0:
        raise ValueError("Invalid tank distance configuration")

    percent = (
        (EMPTY_DISTANCE_MM - distance) * 100.0 / span
    )
    percent = max(0.0, min(100.0, percent))

    liters = (percent / 100.0) * TANK_CAPACITY_LITERS
    return percent, liters


def send_version():
    return controls.ready


def calculate_filled_height_cm(distance):
    span = EMPTY_DISTANCE_MM - FULL_DISTANCE_MM
    if span <= 0:
        raise ValueError("Invalid tank distance configuration")
    return max(0.0, min(span, EMPTY_DISTANCE_MM - distance)) / 10.0


def send_blynk(percent, dist_cm, liters, filled_height_cm):
    controls.submit_telemetry((percent, dist_cm, liters, filled_height_cm))
    log("Latest local reading submitted; cloud", "online" if controls.ready else "offline")
    return controls.ready


def main():
    global controls, display, _network_runtime
    settings = IntervalSettings()
    controls = NetworkRuntime(settings)
    _network_runtime = controls
    display = TankDisplay()
    log("==============================")
    log("PICO 2 W WATER TANK MONITOR - " + APP_VERSION)
    log("Local monitoring is independent of network availability")
    log("==============================")
    if not controls.start():
        # Reject an unsupported/memory-starved trial rather than permanently
        # confirming an app that has lost its remote update path.
        raise RuntimeError("Cannot start isolated network worker")
    confirmed = False

    try:
        while True:
            service_controls()
            distance = get_distance_mm()
            if distance is None:
                display.update(None, None)
                controls.invalidate_telemetry()
                log("Sensor reading unavailable; display marked NO DATA")
                led.off()
            else:
                percent, liters = calculate_water_level(distance)
                dist_cm = distance / 10.0
                filled_height_cm = calculate_filled_height_cm(distance)
                display.update(percent, filled_height_cm)
                log("------------------------------")
                log("Distance: %d mm" % distance)
                log("Water level: %.1f %%" % percent)
                log("Water volume: %.1f L" % liters)
                log("Filled water height: %.1f cm" % filled_height_cm)
                send_blynk(percent, dist_cm, liters, filled_height_cm)
                led.on()
            if not confirmed:
                # A healthy local cycle confirms a trial even with no network.
                ota.mark_healthy()
                confirmed = True
                controls.allow_ota = True
            if controls.ota_ready and controls.storage.acquire(False):
                try:
                    controls.running = False
                    log("Verified OTA staged; restarting at local cycle boundary")
                    machine.reset()
                finally:
                    controls.storage.release()
            ota.feed()
            wait_interval()
    finally:
        controls.stop()
