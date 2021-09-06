# Constants
PUBLISH_INTERVAL = 1 #minutes

# pylint: disable=unused-import
import time
import board
import busio
from digitalio import DigitalInOut, Direction, Pull
from simpleio import map_range
import neopixel
from adafruit_pm25.uart import PM25_UART

# Secrets
try:
    from secrets import secrets
except ImportError:
    print("WiFi secrets are kept in secrets.py, please add them there!")
    raise

# Network
from adafruit_io.adafruit_io import IO_HTTP
import adafruit_esp32spi.adafruit_esp32spi_socket as socket
from adafruit_esp32spi import adafruit_esp32spi
from adafruit_esp32spi import adafruit_esp32spi_wifimanager

esp32_cs = DigitalInOut(board.ESP_CS)
esp32_ready = DigitalInOut(board.ESP_BUSY)
esp32_reset = DigitalInOut(board.ESP_RESET)

spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset)

status_light = neopixel.NeoPixel(board.NEOPIXEL, 1, brightness=0.2)
wifi = adafruit_esp32spi_wifimanager.ESPSPI_WiFiManager(esp, secrets, status_light)

if esp.status == adafruit_esp32spi.WL_IDLE_STATUS:
    print("ESP32 found and in idle mode")
print("Firmware vers.", esp.firmware_version)
print("MAC addr:", [hex(i) for i in esp.MAC_address])

for ap in esp.scan_networks():
    print("\t%s\t\tRSSI: %d" % (str(ap["ssid"], "utf-8"), ap["rssi"]))

print("Connecting to AP %s ..." % (secrets["ssid"]))
while not esp.is_connected:
    try:
        esp.connect_AP(secrets["ssid"], secrets["password"])
    except RuntimeError as e:
        print("could not connect to AP, retrying: ", e)
        continue
print("Connected to", str(esp.ssid, "utf-8"), "\tRSSI:", esp.rssi)
print("My IP address is", esp.pretty_ip(esp.ip_address))


io = IO_HTTP(secrets["aio_user"], secrets["aio_key"], wifi)
feed_raw_pm25 = io.get_feed("air-quality-sensor.raw-pm2-dot-5")
feed_aqi = io.get_feed("air-quality-sensor.aqi")
feed_aqi_category = io.get_feed("air-quality-sensor.category")
location_metadata = {
    "lat": secrets["latitude"],
    "lon": secrets["longitude"],
    "ele": secrets["elevation"],
}

# PM2.5
tx_pin = None
uart_1 = busio.UART(tx_pin, board.D0, baudrate=9600)
uart_2 = busio.UART(tx_pin, board.D2, baudrate=9600)
reset_pin = None
pm25_1 = PM25_UART(uart_1, reset_pin)
pm25_2 = PM25_UART(uart_2, reset_pin)
print("Found PM2.5 sensor, reading data...")

elapsed_minutes = 0
prv_mins = 0


def sample_aq_sensors():
    """Samples PM2.5 sensor
    over a 2.3 second sample rate.
    """
    aq_reading = 0
    aq_samples = []

    # initial timestamp
    time_start = time.monotonic()
    # sample pm2.5 sensor over 2.3 sec sample rate
    while time.monotonic() - time_start <= 2.3:
        try:
            aqdata_1 = pm25_1.read()
            aqdata_2 = pm25_2.read()
            # TODO - check delta between readings, calculate accuracy
            aq_samples.append(aqdata_1["pm25 env"])
            aq_samples.append(aqdata_2["pm25 env"])
        except RuntimeError:
            print("Unable to read from sensor, retrying...")
            continue
        # pm sensor output rate of 1s
        time.sleep(1)
    # average sample reading / # samples
    for sample in range(len(aq_samples)):
        aq_reading += aq_samples[sample]
    aq_reading = aq_reading / len(aq_samples)
    print(aq_samples)
    aq_samples.clear()
    return aq_reading


def calculate_aqi(pm_sensor_reading):
    """Returns a calculated air quality index (AQI)
    and category as a tuple.
    NOTE: The AQI returned by this function should ideally be measured
    using the 24-hour concentration average. Calculating a AQI without
    averaging will result in higher AQI values than expected.
    :param float pm_sensor_reading: Particulate matter sensor value.

    """
    # Check sensor reading using EPA breakpoint (Clow-Chigh)
    if 0.0 <= pm_sensor_reading <= 12.0:
        # AQI calculation using EPA breakpoints (Ilow-IHigh)
        aqi_val = map_range(int(pm_sensor_reading), 0, 12, 0, 50)
        aqi_cat = "Good"
    elif 12.1 <= pm_sensor_reading <= 35.4:
        aqi_val = map_range(int(pm_sensor_reading), 12, 35, 51, 100)
        aqi_cat = "Moderate"
    elif 35.5 <= pm_sensor_reading <= 55.4:
        aqi_val = map_range(int(pm_sensor_reading), 36, 55, 101, 150)
        aqi_cat = "Unhealthy for Sensitive Groups"
    elif 55.5 <= pm_sensor_reading <= 150.4:
        aqi_val = map_range(int(pm_sensor_reading), 56, 150, 151, 200)
        aqi_cat = "Unhealthy"
    elif 150.5 <= pm_sensor_reading <= 250.4:
        aqi_val = map_range(int(pm_sensor_reading), 151, 250, 201, 300)
        aqi_cat = "Very Unhealthy"
    elif 250.5 <= pm_sensor_reading <= 350.4:
        aqi_val = map_range(int(pm_sensor_reading), 251, 350, 301, 400)
        aqi_cat = "Hazardous"
    elif 350.5 <= pm_sensor_reading <= 500.4:
        aqi_val = map_range(int(pm_sensor_reading), 351, 500, 401, 500)
        aqi_cat = "Hazardous"
    else:
        print("Invalid PM2.5 concentration")
        aqi_val = -1
        aqi_cat = None
    return aqi_val, aqi_cat

while True:
    try:
        print("Fetching time...")
        cur_time = io.receive_time()
        print("Time fetched OK!")
        # Hourly reset
        if cur_time.tm_min == 0:
            prv_mins = 0
    except (ValueError, RuntimeError) as e:
        print("Failed to fetch time, retrying\n", e)
        wifi.reset()
        wifi.connect()
        continue

    if cur_time.tm_min >= prv_mins:
        print("%d min elapsed.." % elapsed_minutes)
        prv_mins = cur_time.tm_min
        elapsed_minutes += 1

    if elapsed_minutes >= PUBLISH_INTERVAL:
        print("Sampling AQI...")
        aqi_reading = sample_aq_sensors()
        print(aqi_reading)
        aqi, aqi_category = calculate_aqi(aqi_reading)
        print("AQI: %d" % aqi)
        print("Category: %s" % aqi_category)

        # Publish all values to Adafruit IO
        print("Publishing to Adafruit IO...")
        try:
            io.send_data(feed_raw_pm25["key"], str(aqi_reading), location_metadata)
            io.send_data(feed_aqi["key"], str(aqi), location_metadata)
            io.send_data(feed_aqi_category["key"], aqi_category)
            print("Published!")
        except (ValueError, RuntimeError) as e:
            print("Failed to send data to IO, retrying\n", e)
            wifi.reset()
            wifi.connect()
            continue
        # Reset timer
        elapsed_minutes = 0
    time.sleep(30)
