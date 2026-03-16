import asyncio
from bleak import BleakScanner
from datetime import datetime
import csv
import os
import sys
import time
from pathlib import Path

# Список MAC адресов датчиков
DEVICE_MACS = [
    "a4:c1:38:8a:fb:e8",
    "a4:c1:38:10:18:1b",
    "a4:c1:38:a0:1e:a5",
]

BASE_DIR = Path(__file__).resolve().parent   # папка, где лежит .py
LOG_DIR = BASE_DIR / "logs"
SENSORS_DIR = LOG_DIR

LOG_DIR.mkdir(parents=True, exist_ok=True)
SENSORS_DIR.mkdir(parents=True, exist_ok=True)

def mac_to_filename(mac: str) -> str:
    return mac.replace(":", "").upper()

def write_status(mac: str, value: int, unix_ts: int):
    path = SENSORS_DIR / f"sensor_{mac_to_filename(mac)}.status"
    readable_time = datetime.fromtimestamp(unix_ts).isoformat()
    path.write_text(f"{readable_time},{unix_ts},{value}\n", encoding="utf-8")

def write_last_ok(mac: str, temp: float, hum: int, bat: int, unix_ts: int, delay: float):
    path = SENSORS_DIR / f"sensor_{mac_to_filename(mac)}.csv"
    readable_time = datetime.fromtimestamp(unix_ts).isoformat()
    with open(path, "w", newline="", encoding="utf-8") as f:
        # можно без заголовка — проще для чтения
        f.write(f"{readable_time},{unix_ts},{temp},{hum},{bat},{delay}\n")

def get_csv_file(ts: datetime) -> str:
    return str(LOG_DIR / f"ble_{ts:%Y-%m}.csv")

# CSV_FILE = "log.csv"

def parse_atc(raw_bytes: bytes):
    """Парсер ATC формата (температура, влажность, батарея)."""
    if len(raw_bytes) < 10:
        return None

    data = list(raw_bytes)
    mac = ":".join(f"{b:02X}" for b in data[0:6])  # MAC (0–5)

    # Температура (байты 6–7, int16 big endian, делим на 10)
    temp_raw = (data[6] << 8) | data[7]
    if temp_raw & 0x8000:
        temp_raw = temp_raw - 0x10000
    temperature = temp_raw / 10

    humidity = data[8]  # Влажность %
    battery = data[9]   # Батарея %

    return {
        "mac": mac,
        "temperature": round(temperature, 1),
        "humidity": humidity,
        "battery": battery,
        "raw": raw_bytes.hex()
    }


async def scan_ble(duration=25):
    found_data = {}
    stop_events = {mac: asyncio.Event() for mac in DEVICE_MACS}
    loop = asyncio.get_running_loop()
    start_time = time.time()

    def detection_callback(device, adv_data):
        addr = device.address.lower()
        if addr in DEVICE_MACS and addr not in found_data:
            for uuid, data in adv_data.service_data.items():
                raw = bytes(data)
                parsed = parse_atc(raw)
                if parsed:
                    elapsed = round(time.time() - start_time, 2)
                    parsed["elapsed"] = elapsed
                    found_data[addr] = parsed
                    print(f"[{datetime.now().isoformat()}] {parsed['mac']} | "
                          f"T={parsed['temperature']}°C, H={parsed['humidity']}%, "
                          f"Bat={parsed['battery']}% | RAW={parsed['raw']} | "
                          f"Received after {elapsed} sec")
                    loop.call_soon_threadsafe(stop_events[addr].set)
                else:
                    print(f"[WARN] Invalid packet from {addr}: {raw.hex()}")

    scanner = BleakScanner(detection_callback)
    await scanner.start()

    try:
        await asyncio.wait_for(
            asyncio.gather(*(ev.wait() for ev in stop_events.values())),
            timeout=duration
        )
    except asyncio.TimeoutError:
        print(f"[INFO] Timeout: not all packets were received within {duration} seconds")

    await scanner.stop()
    return found_data


async def main():
    timestamp = datetime.now()
    
    unix_ts = int(timestamp.timestamp())
    
    results = await scan_ble(20)
    
    CSV_FILE = get_csv_file(timestamp)

    file_exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Date", "Time", "UnixTime", "MAC", 
                             "Temp (C)", "Humidity (%)", 
                             "Battery (%)", "RAW HEX",
                             "Error flag", "Delay (sec)"])

        for mac in DEVICE_MACS:
            if mac in results:
                data = results[mac]
                writer.writerow([timestamp.date().isoformat(),
                                 timestamp.time().strftime("%H:%M:%S"),
                                 unix_ts,
                                 data['mac'],
                                 data['temperature'],
                                 data['humidity'],
                                 data['battery'],
                                 data['raw'],
                                 0,
                                 data.get("elapsed", "")])
                write_last_ok(
                    data["mac"],               # MAC из пакета (upper)
                    data["temperature"],
                    data["humidity"],
                    data["battery"],
                    unix_ts,
                    data.get("elapsed", "")
                )
                write_status(mac, 0, unix_ts)
            else:
                print(f"{timestamp.isoformat()} — {mac.upper()} — ❌ No data")
                writer.writerow([timestamp.date().isoformat(),
                                 timestamp.time().strftime("%H:%M:%S"),
                                 unix_ts,
                                 mac.upper(), "", "", "", "", 1, ""])
                write_status(mac, 1, unix_ts)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by the user.")
        sys.exit(0)
