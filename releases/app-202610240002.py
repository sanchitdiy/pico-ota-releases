import network
import time
import machine
from machine import Pin, UART

try:
    import urequests as requests
except ImportError:
    import requests

from device_config import SSID, PASSWORD, BLYNK_TOKEN, BLYNK_SERVER
import ota


# Application version shown in Blynk V8.
# This is separate from the integer OTA release number.
APP_VERSION = "20261024-2"

# Tank configuration
EMPTY_DISTANCE_MM = 2000
FULL_DISTANCE_MM = 20
TANK_CAPACITY_LITERS = 1000

# Sensor configuration
UPDATE_INTERVAL_S = 2
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

led = Pin("LED", Pin.OUT)

uart = UART(
    0,
    baudrate=9600,
    tx=Pin(0),
    rx=Pin(1)
)

wlan = network.WLAN(network.STA_IF)


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
        ota.feed()
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
        ota.feed()
        time.sleep_ms(10)

    return None


def get_distance_mm():
    values = []

    for _ in range(SAMPLES):
        distance = read_distance_mm()

        if distance is not None:
            values.append(distance)

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
            response = requests.get(url)

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

        ota.feed()
        time.sleep_ms(200)

    return all_ok


def main():
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
        time.sleep(UPDATE_INTERVAL_S)