import network
import time
import machine
import os
import json
import socket
import ssl
import select
import struct
from machine import Pin, UART

try:
    import urequests as requests
except ImportError:
    import requests

from device_config import SSID, PASSWORD, BLYNK_TOKEN, BLYNK_SERVER
import ota


# Application version shown in Blynk V8.
# This is separate from the integer OTA release number.
APP_VERSION = "20261024-3"

# Tank configuration
EMPTY_DISTANCE_MM = 2000
FULL_DISTANCE_MM = 20
TANK_CAPACITY_LITERS = 1000

# Sensor configuration
DEFAULT_INTERVAL_S = 10
SETTINGS_FILE = "tank-settings.json"
controls = None
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


class IntervalSettings:
    """Device-local settings survive app-only OTA releases."""

    def __init__(self):
        self.interval = DEFAULT_INTERVAL_S
        try:
            with open(SETTINGS_FILE) as source:
                value = json.load(source)["interval_s"]
            if type(value) is not int or value < 1:
                raise ValueError("Invalid saved interval")
            self.interval = value
        except (OSError, ValueError, KeyError, TypeError):
            print("Interval: using default", self.interval, "seconds")
        self.states = {}

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
        interval = max(1, self.interval + (1 if pin == 7 else -1))
        if interval != self.interval:
            temporary = SETTINGS_FILE + ".tmp"
            with open(temporary, "w") as target:
                json.dump({"interval_s": interval}, target)
                target.flush()
            os.rename(temporary, SETTINGS_FILE)
            os.sync()
            self.interval = interval
            print("Saved interval:", interval, "seconds")
        self.states[pin] = value
        return True


class BlynkControls:
    """Blynk binary protocol over TLS; receive both edges without HTTP polling."""

    def __init__(self, settings):
        self.settings = settings
        self.connection = None
        self.poller = None
        self.buffer = b""
        self.ready = False
        self.message_id = 0
        self.login_id = None
        self.last_attempt = None
        self.last_rx = 0
        self.last_ping = 0
        self.feedback_pending = True

    def _send(self, command, body=b""):
        self.message_id = self.message_id % 65535 + 1
        packet = struct.pack(">BHH", command, self.message_id, len(body)) + body
        while packet:
            ota.feed()
            count = self.connection.write(packet)
            if not count:
                raise OSError("Blynk write failed")
            packet = packet[count:]
        return self.message_id

    def _disconnect(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
        self.connection = None
        self.poller = None
        self.ready = False
        self.buffer = b""
        self.feedback_pending = True
        self.settings.reset_switches()

    def _connect(self):
        self.last_attempt = time.ticks_ms()
        raw = socket.socket()
        try:
            raw.settimeout(3)
            address = socket.getaddrinfo(
                BLYNK_SERVER, 443, 0, socket.SOCK_STREAM
            )[0][-1]
            ota.feed()
            raw.connect(address)
            ota.feed()
            # Keep the original hostname for TLS. Do not override trust checks.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self.connection = context.wrap_socket(
                raw, server_hostname=BLYNK_SERVER
            )
            ota.feed()
            self.connection.settimeout(2)
            self.poller = select.poll()
            self.poller.register(self.connection, select.POLLIN)
            self.login_id = self._send(2, BLYNK_TOKEN.encode())
            self.last_rx = time.ticks_ms()
            self.last_ping = self.last_rx
        except Exception:
            if self.connection is None:
                raw.close()
            self._disconnect()
            raise

    def _handle(self, command, message_id, body, status=None):
        if command == 0:
            if message_id == self.login_id:
                if status != 200:
                    raise OSError("Blynk control login rejected")
                self.login_id = None
                self.ready = True
                self.settings.reset_switches()
                self._send(17, b"h-beat\x0010")
                self._send(25, b"vr\x007\x0017")
                self.feedback_pending = True
                print("Blynk interval controls connected")
            return
        if command == 6:
            self.connection.write(struct.pack(">BHH", 0, message_id, 200))
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
        if self.settings.toggle(pin, value):
            self.feedback_pending = True

    def _receive(self):
        if not self.poller.poll(0):
            return
        data = self.connection.read(1024)
        if not data:
            raise OSError("Blynk control connection closed")
        self.last_rx = time.ticks_ms()
        self.buffer += data
        while len(self.buffer) >= 5:
            command, message_id, length = struct.unpack(">BHH", self.buffer[:5])
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
            self._receive()
            now = time.ticks_ms()
            if time.ticks_diff(now, self.last_rx) > 30000:
                raise OSError("Blynk controls heartbeat timeout")
            if self.ready:
                if time.ticks_diff(now, self.last_ping) >= 10000:
                    self._send(6)
                    self.last_ping = now
                if self.feedback_pending:
                    self._send(
                        20, ("vw\x0011\x00%d" % self.settings.interval).encode()
                    )
                    self.feedback_pending = False
        except Exception as exc:
            # Never log token-bearing network payloads or URLs.
            print("Blynk controls unavailable:", type(exc).__name__)
            self._disconnect()
        finally:
            ota.feed()


def service_controls():
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

    print("Connecting to Wi-Fi...")

    try:
        wlan.disconnect()
    except Exception:
        pass

    time.sleep(1)

    try:
        wlan.connect(SSID, PASSWORD)
    except Exception as exc:
        print("Wi-Fi connection error:", exc)
        return False

    for _ in range(30):
        if wlan.isconnected():
            print("Wi-Fi connected")
            print("IP:", wlan.ifconfig()[0])
            led.on()
            return True

        led.toggle()
        service_controls()
        time.sleep(0.5)

    print("Wi-Fi connection failed")
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
    """Return True only when Blynk accepts the version."""
    response = None
    ota.feed()

    try:
        url = (
            "https://%s/external/api/update?token=%s&v8=%s"
            % (BLYNK_SERVER, BLYNK_TOKEN, APP_VERSION)
        )

        response = requests.get(url, timeout=5)

        print(
            "Blynk v8 = %s | HTTP %d"
            % (APP_VERSION, response.status_code)
        )

        return response.status_code == 200

    except Exception as exc:
        # Avoid printing request URLs containing credentials.
        print("Blynk version send failed:", type(exc).__name__)
        return False

    finally:
        try:
            if response is not None:
                response.close()
        finally:
            ota.feed()


def send_blynk(percent, dist_cm, liters):
    values = {
        "v1": percent,
        "v2": dist_cm,
        "v3": liters
    }

    all_ok = True

    for pin, value in values.items():
        ota.feed()

        url = (
            "https://%s/external/api/update"
            "?token=%s&%s=%.1f"
        ) % (
            BLYNK_SERVER,
            BLYNK_TOKEN,
            pin,
            value
        )

        response = None

        try:
            response = requests.get(url, timeout=5)

            print(
                "Blynk %s = %.1f | HTTP %d"
                % (pin, value, response.status_code)
            )

            if response.status_code != 200:
                all_ok = False

        except Exception as exc:
            print("Blynk %s error: %s" % (pin, exc))
            all_ok = False

        finally:
            if response is not None:
                response.close()

        service_controls()
        time.sleep_ms(200)

    return all_ok


def main():
    global controls
    controls = BlynkControls(IntervalSettings())
    print("==============================")
    print("PICO 2 W WATER TANK MONITOR - " + APP_VERSION)
    print("==============================")

    time.sleep(2)

    while not connect_wifi():
        print("Retrying Wi-Fi in 5 seconds")
        time.sleep(5)

    send_failures = 0
    version_sent = False

    while True:
        if not wlan.isconnected():
            led.off()
            print("Wi-Fi disconnected")

            if not connect_wifi():
                time.sleep(5)
                continue

        service_controls()
        distance = get_distance_mm()

        if distance is None:
            print("Sensor reading unavailable")
            print("Check UART and sensor wiring")
            led.off()

        else:
            percent, liters = calculate_water_level(distance)
            dist_cm = distance / 10.0

            print("------------------------------")
            print("Distance: %d mm" % distance)
            print("Water level: %.1f %%" % percent)
            print("Water volume: %.1f L" % liters)

            success = send_blynk(percent, dist_cm, liters)

            if success:
                send_failures = 0
                led.on()

            else:
                send_failures += 1
                led.off()

                print(
                    "Consecutive send failures:",
                    send_failures
                )

                if send_failures >= MAX_SEND_FAILURES:
                    print("Repeated Blynk failures")

                    if not DEBUG:
                        time.sleep(3)
                        machine.reset()

                    send_failures = 0

        ota.mark_healthy()

        # Retry until successful, then stop for this boot.
        if not version_sent:
            version_sent = send_version()

        ota.poll()
        ota.feed()
        wait_interval()
