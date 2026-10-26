import network
import time
import machine
import os
import json
import socket
import ssl
import struct
from machine import Pin, UART

from device_config import SSID, PASSWORD, BLYNK_TOKEN, BLYNK_SERVER
import ota


# Application version shown in Blynk V8.
# This is separate from the integer OTA release number.
APP_VERSION = "20261026-5"

# Tank configuration
EMPTY_DISTANCE_MM = 2000
FULL_DISTANCE_MM = 20
TANK_CAPACITY_LITERS = 1000

# Sensor configuration
DEFAULT_INTERVAL_S = 10
SETTINGS_FILE = "tank-settings.json"
controls = None
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

    def __init__(self, settings):
        self.settings = settings
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


def service_controls():
    update_uptime()
    sync_clock()
    ota.feed()
    if controls is not None:
        controls.pump()
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


def connect_wifi():
    wlan.active(True)

    if wlan.isconnected():
        return True

    log("Connecting to Wi-Fi...")

    try:
        wlan.disconnect()
    except Exception:
        pass

    time.sleep(1)

    try:
        wlan.connect(SSID, PASSWORD)
    except Exception as exc:
        log("Wi-Fi connection error:", exc)
        return False

    for _ in range(30):
        if wlan.isconnected():
            log("Wi-Fi connected")
            log("IP:", wlan.ifconfig()[0])
            led.on()
            return True

        led.toggle()
        service_controls()
        time.sleep(0.5)

    log("Wi-Fi connection failed")
    led.off()
    return False


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
    return controls.publish_version()


def calculate_filled_height_cm(distance):
    span = EMPTY_DISTANCE_MM - FULL_DISTANCE_MM
    if span <= 0:
        raise ValueError("Invalid tank distance configuration")
    return max(0.0, min(span, EMPTY_DISTANCE_MM - distance)) / 10.0


def send_blynk(percent, dist_cm, liters, filled_height_cm):
    """Use the same online device session as switch input and feedback."""
    if not controls.ready:
        log("Blynk telemetry waiting for device login")
        return False
    try:
        for pin, value in (
            (1, percent), (2, dist_cm), (3, liters),
            (5, filled_height_cm), (6, filled_height_cm)
        ):
            service_controls()
            if not controls.virtual_write(pin, "%.1f" % value):
                return False
        log("Blynk sensor values queued on persistent connection")
        return True
    except OSError:
        controls._disconnect()
        return False


def main():
    global controls
    controls = BlynkControls(IntervalSettings())
    log("==============================")
    log("PICO 2 W WATER TANK MONITOR - " + APP_VERSION)
    log("==============================")

    time.sleep(2)

    while not connect_wifi():
        log("Retrying Wi-Fi in 5 seconds")
        time.sleep(5)

    send_failures = 0

    while True:
        if not wlan.isconnected():
            led.off()
            log("Wi-Fi disconnected")

            if not connect_wifi():
                time.sleep(5)
                continue

        service_controls()
        distance = get_distance_mm()

        if distance is None:
            log("Sensor reading unavailable")
            log("Check UART and sensor wiring")
            led.off()

        else:
            percent, liters = calculate_water_level(distance)
            dist_cm = distance / 10.0
            filled_height_cm = calculate_filled_height_cm(distance)

            log("------------------------------")
            log("Distance: %d mm" % distance)
            log("Water level: %.1f %%" % percent)
            log("Water volume: %.1f L" % liters)
            log("Filled water height: %.1f cm" % filled_height_cm)

            success = send_blynk(percent, dist_cm, liters, filled_height_cm)

            if success:
                send_failures = 0
                led.on()

            else:
                send_failures += 1
                led.off()

                log(
                    "Consecutive send failures:",
                    send_failures
                )

                if send_failures >= MAX_SEND_FAILURES:
                    log("Repeated Blynk failures")

                    if not DEBUG:
                        time.sleep(3)
                        machine.reset()

                    send_failures = 0

        ota.mark_healthy()

        ota.poll()
        ota.feed()
        wait_interval()
