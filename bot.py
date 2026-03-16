import logging
import csv
#
import psutil
import subprocess
import platform
import socket
import os  # добавить наверху
#
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import requests
from io import BytesIO
import asyncio
import time
from dotenv import load_dotenv
import json
from pathlib import Path

load_dotenv()

# === настройки ===
TOKEN = os.getenv("BOT_TOKEN")

CSV_FILE = "/home/it/iotmon/logs/ble_2026-03.csv"
MOTION_CSV_FILE = "/home/it/iotmon/logs/motion_log.csv"
SUBSCRIBERS_FILE = Path("/home/it/iotmon/subscribers.json")

CAMERA_URL = "http://192.168.1.64/ISAPI/Streaming/channels/101/picture"
CAMERA_USER = os.getenv("CAM_USR")
CAMERA_PASS = os.getenv("CAM_PWD")

LAST_SNAPSHOT = Path("/mnt/usbflash/snapshot.jpg")

PHOTO_RATE_LIMIT_SECONDS = 5
LAST_PHOTO_TIME = {}

# соответствие MAC -> имя датчика
SENSOR_NAMES = {
    "A4:C1:38:8A:FB:E8": "On the 2nd Floor",
    "A4:C1:38:A0:1E:A5": "In Workshop",
    "A4:C1:38:10:18:1B": "Near Entrance",

}

# включаем логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

def load_subscribers() -> list[int]:
    if not SUBSCRIBERS_FILE.exists():
        return []
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [int(x) for x in data]
    except Exception as e:
        logger.error(f"Ошибка чтения subscribers.json: {e}")
        return []


def save_subscribers(subscribers: list[int]) -> None:
    SUBSCRIBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(set(subscribers)), f, ensure_ascii=False, indent=2)

def get_camera_photo():
    response = requests.get(
        CAMERA_URL,
        auth=(CAMERA_USER, CAMERA_PASS),
        timeout=10
    )
    response.raise_for_status()

    photo = BytesIO(response.content)
    photo.name = "snapshot.jpg"
    photo.seek(0)
    return photo

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers = load_subscribers()

    if chat_id not in subscribers:
        subscribers.append(chat_id)
        save_subscribers(subscribers)
        await update.message.reply_text("✅ This chat is now subscribed to motion alerts.")
    else:
        await update.message.reply_text("ℹ️ This chat is already subscribed.")


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers = load_subscribers()

    if chat_id in subscribers:
        subscribers.remove(chat_id)
        save_subscribers(subscribers)
        await update.message.reply_text("❌ This chat has been unsubscribed from motion alerts.")
    else:
        await update.message.reply_text("ℹ️ This chat was not subscribed.")


async def subscribers_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers = load_subscribers()
    await update.message.reply_text(f"👥 Subscribers: {len(subscribers)}")

def get_last_rows(filename, num_rows=6):
    try:
        with open(filename, newline="") as f:
            rows = list(csv.reader(f))
            if len(rows) <= 1:
                return []
            header, data = rows[0], rows[1:]
            return data[-num_rows:]
    except Exception as e:
        logger.error(f"Ошибка чтения CSV: {e}")
        return []

async def last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Пытаемся прочитать параметр, если он есть
    try:
        count = int(context.args[0]) if context.args else 1
    except ValueError:
        count = 3

    if count < 1:
        count = 1
    elif count > 9:
        count = 9

    # Сколько строк читать? Чтобы захватить достаточно записей
    # Предположим, что в одной группе ~3 записи (датчиков)
    rows_to_read = count * 6

    rows = get_last_rows(CSV_FILE, rows_to_read)

    if not rows:
        await update.message.reply_text("Нет данных в CSV.")
        return

    data_by_datetime = {}
    for row in rows:
        if len(row) < 7:
            continue
        date, time, mac, temp, hum, bat = row[0], row[1], row[3], row[4], row[5], row[6]
        dt_key = f"{date} {time}"
        sensor_name = SENSOR_NAMES.get(mac.upper(), f"Неизвестный ({mac})")
        line = f"{sensor_name}: 🌡 {temp}°C 💧 {hum}% "
        if dt_key not in data_by_datetime:
            data_by_datetime[dt_key] = []
        data_by_datetime[dt_key].append(line)

    messages = []
    for dt_key in sorted(data_by_datetime.keys(), reverse=True):
        sensors_text = "\n".join(data_by_datetime[dt_key])
        messages.append(f"Time: {dt_key}\n{sensors_text}")

    text = "\n\n".join(messages[:count])
    await update.message.reply_text(text)

################################################################################################################################
################################################################################################################################
def get_load_avg():
    try:
        load1, load5, load15 = os.getloadavg()
        return load1, load5, load15
    except (AttributeError, OSError):
        return None, None, None


# Получаем температуру CPU (зависит от системы)
def get_cpu_temperature():
    try:
        # Linux: читаем из /sys/class/thermal/thermal_zone0/temp
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_str = f.readline()
            temp_c = int(temp_str) / 1000
            return f"{temp_c:.1f}°C"
    except FileNotFoundError:
        # Для других систем можно попробовать psutil.sensors_temperatures()
        temps = psutil.sensors_temperatures()
        if temps:
            for name, entries in temps.items():
                for entry in entries:
                    if entry.current:
                        return f"{entry.current:.1f}°C"
        return "Температура недоступна"

# Получаем загрузку CPU в процентах
def get_cpu_load():
    return psutil.cpu_percent(interval=1)

# Получаем память (в МБ)
def get_memory():
    mem = psutil.virtual_memory()
    total_mb = mem.total / 1024 / 1024
    available_mb = mem.available / 1024 / 1024
    return total_mb, available_mb

# Получаем аптайм (в часах, минутах)
def get_uptime():
    boot_time = psutil.boot_time()
    from datetime import datetime, timedelta
    uptime_sec = (datetime.now() - datetime.fromtimestamp(boot_time)).total_seconds()
    uptime_str = str(timedelta(seconds=int(uptime_sec)))
    return uptime_str

# Получаем IP wlan0
def get_ip_wlan0():
    addrs = psutil.net_if_addrs()
    if "wlan0" in addrs:
        for addr in addrs["wlan0"]:
            if addr.family == socket.AF_INET:  # тут socket.AF_INET вместо psutil.AF_INET
                return addr.address
    return "wlan0 не найден или нет IP"

# Получаем сетевой трафик (bytes sent и received) по wlan0
def get_traffic_wlan0():
    net_io = psutil.net_io_counters(pernic=True)
    if "wlan0" in net_io:
        stats = net_io["wlan0"]
        sent_mb = stats.bytes_sent / 1024 / 1024
        recv_mb = stats.bytes_recv / 1024 / 1024
        return sent_mb, recv_mb
    return None, None

# Обработчик команды /status
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    temp = get_cpu_temperature()
    load_cpu = get_cpu_load()
    load1, load5, load15 = get_load_avg()
    total_mem, avail_mem = get_memory()
    uptime = get_uptime()
    ip = get_ip_wlan0()
    sent_mb, recv_mb = get_traffic_wlan0()

    msg = (
        f"🌡 CPU temp: {temp}\n"
        f"⚙️ CPU load (в %): {load_cpu}%\n"
        f"📊 Load average: {load1:.2f}, {load5:.2f}, {load15:.2f}\n"
        f"💾 Memory: {avail_mem:.1f}MB свободно / {total_mem:.1f}MB всего\n"
        f"📶 IP wlan0: {ip}\n"
        f"⬆️ Sent: {sent_mb:.2f} MB\n"
        f"⬇️ Received: {recv_mb:.2f} MB\n"
        f"⏱ Uptime: {uptime}"
    )
    await update.message.reply_text(msg)

########### 2302

def get_last_motion_activity(filename):
    """
    Возвращает (date, time) из последней непустой строки CSV.
    Ожидаемый формат строки:
    date, time, unix_ts, status
    или с табами (если csv записан через \t)
    """
    try:
        if not os.path.exists(filename):
            return None, None, f"Файл не найден: {filename}"

        last_line = None
        with open(filename, "r", encoding="utf-8", newline="") as f:
            for line in f:
                line = line.strip()
                if line:
                    last_line = line

        if not last_line:
            return None, None, "Файл пустой."

        # Поддержка и tab-separated, и comma-separated
        if "\t" in last_line:
            parts = [p.strip() for p in last_line.split("\t")]
        else:
            # fallback через csv.reader для обычного CSV
            parts = next(csv.reader([last_line]))

        if len(parts) < 2:
            return None, None, f"Неверный формат строки: {last_line}"

        date_str = parts[0]
        time_str = parts[1]
        return date_str, time_str, None

    except Exception as e:
        logger.exception("Ошибка чтения motion CSV")
        return None, None, f"Ошибка чтения файла: {e}"
        
def is_photo_rate_limited(user_id: int):
    now = time.time()
    last_time = LAST_PHOTO_TIME.get(user_id, 0)

    if now - last_time < PHOTO_RATE_LIMIT_SECONDS:
        remaining = PHOTO_RATE_LIMIT_SECONDS - (now - last_time)
        return True, remaining

    LAST_PHOTO_TIME[user_id] = now
    return False, 0        
        

async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    limited, remaining = is_photo_rate_limited(user_id)

    if limited:
        await update.message.reply_text(
            f"⏳ Please wait {remaining:.1f} sec before requesting another photo."
        )
        return

    try:
        img = await asyncio.to_thread(get_camera_photo)
        await update.message.reply_photo(photo=img, caption="📷 Snapshot from camera")
    except Exception as e:
        logger.exception("Ошибка получения фото с камеры")
        await update.message.reply_text(f"❌ Could not get photo: {e}")
        
async def reply_with_optional_snapshot(update: Update, text: str):
    if LAST_SNAPSHOT.exists():
        try:
            with open(LAST_SNAPSHOT, "rb") as f:
                await update.message.reply_photo(photo=f, caption=text)
            return
        except Exception as e:
            logger.exception("Ошибка отправки snapshot")
            await update.message.reply_text(f"{text}\n\n⚠️ Snapshot error: {e}")
            return

    await update.message.reply_text(text)        

async def lastactivity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str, time_str, err = get_last_motion_activity(MOTION_CSV_FILE)

    if err:
        await update.message.reply_text(f"❌ {err}")
        return

    text = (
        f"Last motion detected:\n"
        f"📅 {date_str}\n"
        f"🕒 {time_str}"
    )

    await reply_with_optional_snapshot(update, text)     

################################################################################################################################
################################################################################################################################
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 Available commands:\n"
        "/status – show temperature, load, memory, network, and uptime\n"
        "/sensors – show the latest sensors data\n"
        "/activity – show the time of the last activity from the cam\n"
        "/photo – take snapshot from camera\n"
        "/subscribe – subscribe this chat to motion alerts\n"
        "/unsubscribe – unsubscribe this chat from motion alerts\n"
        "/subscribers – show subscribers count\n"
        "/help – show this message\n"
    )
    await update.message.reply_text(msg)

################################################################################################################################




def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("sensors", last))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("activity", lastactivity))
    app.add_handler(CommandHandler("photo", photo))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("subscribers", subscribers_count))
    print("✅ bot started. send /help")
    app.run_polling()

if __name__ == "__main__":
    main()
