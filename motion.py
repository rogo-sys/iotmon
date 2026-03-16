import requests
from requests.auth import HTTPDigestAuth
import xml.etree.ElementTree as ET
import time
from datetime import datetime
import os
import subprocess
from dotenv import load_dotenv
import json
import asyncio
from io import BytesIO
from telegram import Bot
from pathlib import Path

load_dotenv()



ip = "192.168.1.64"
user = os.getenv("CAM_USR")
pwd  = os.getenv("CAM_PWD")

url = f"http://{ip}/ISAPI/Event/notification/alertStream"

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUBSCRIBERS_FILE = Path("/home/it/iotmon/subscribers.json")
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "motion_log.csv"

COOLDOWN_SEC = 41
cooldown_until = 0.0

# --- RTSP + запись ---
RTSP_URL = f"rtsp://{user}:{pwd}@192.168.1.64:554/Streaming/Channels/101"
VIDEO_BASE_DIR = Path("/mnt/usbflash/vidssub")   # базовая папка для видео
CLIP_SECONDS = 40

def load_subscribers() -> list[int]:
    if not SUBSCRIBERS_FILE.exists():
        return []
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [int(x) for x in data]
    except Exception as e:
        print("subscribers read error:", e)
        return []


def get_camera_snapshot() -> BytesIO:
    response = requests.get(
        f"http://{ip}/ISAPI/Streaming/channels/101/picture",
        auth=HTTPDigestAuth(user, pwd),
        timeout=10,
    )
    response.raise_for_status()

    photo = BytesIO(response.content)
    photo.name = "snapshot.jpg"
    photo.seek(0)
    return photo


async def send_motion_alert_ptb(caption: str):
    subscribers = load_subscribers()
    if not subscribers:
        print("No subscribers for motion alerts")
        return

    print("BOT_TOKEN ok:", bool(BOT_TOKEN))
    print("Subscribers:", load_subscribers())
    bot = Bot(token=BOT_TOKEN)

    try:
        photo = get_camera_snapshot()
    except Exception as e:
        print("snapshot error:", e)
        for chat_id in subscribers:
            try:
                await bot.send_message(chat_id=chat_id, text=f"{caption}\n\n❌ Snapshot error: {e}")
            except Exception as send_err:
                print(f"send_message error to {chat_id}: {send_err}")
        return

    for chat_id in subscribers:
        try:
            photo.seek(0)
            await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
            print(f"Alert sent to {chat_id}")
        except Exception as e:
            print(f"send_photo error to {chat_id}: {e}")


def log_motion(dt_text: str, status: int = 1) -> None:
    try:
        dt = datetime.fromisoformat(dt_text) if dt_text else datetime.now().astimezone()
    except Exception:
        dt = datetime.now().astimezone()

    date_s = dt.strftime("%Y-%m-%d")
    time_s = dt.strftime("%H:%M:%S")
    unix_s = int(dt.timestamp())

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{date_s},{time_s},{unix_s},{status}\n")

def record_clip(event_dt_text: str | None = None) -> Path | None:
    """
    Стартует ffmpeg на CLIP_SECONDS и пишет mp4 в подпапку YYYY-MM-DD.
    Возвращает путь к файлу (или None, если не смогли стартовать).
    """
    # Для имени файла удобнее взять время события, но если оно кривое — берём now
    try:
        dt = datetime.fromisoformat(event_dt_text) if event_dt_text else datetime.now().astimezone()
    except Exception:
        dt = datetime.now().astimezone()

    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H-%M-%S")

    out_dir = VIDEO_BASE_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{time_str}.mp4"

    cmd = [
        "ffmpeg",
        "-y",                 # перезапись, если вдруг совпало имя
        "-rtsp_transport", "tcp",
        "-i", RTSP_URL,
        "-t", str(CLIP_SECONDS),
        "-c", "copy",
        str(out_file),
    ]

    try:
        # НЕ блокируем основной цикл чтения событий
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return out_file
    except Exception as e:
        print("ffmpeg start error:", e)
        return None

# ????????? ????
#caption = "?? test alert"
#asyncio.run(send_motion_alert_ptb(caption))
#raise SystemExit


r = requests.get(
    url,
    auth=HTTPDigestAuth(user, pwd),
    stream=True,
    timeout=(5, None),
    headers={"Accept": "application/xml"}
)

print("HTTP:", r.status_code)
if r.status_code != 200:
    print("Response (first 500 chars):")
    print(r.text[:500])
    raise SystemExit

def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag

buf = []
in_msg = False

for raw in r.iter_lines(chunk_size=4096):
    if not raw:
        continue

    line = raw.decode("utf-8", errors="ignore").strip()

    if "<EventNotificationAlert" in line:
        in_msg = True
        buf = [line]
        continue

    if in_msg:
        buf.append(line)

        if "</EventNotificationAlert>" in line:
            xml_text = "".join(buf)
            in_msg = False

            try:
                root = ET.fromstring(xml_text)
                data = {}
                for el in root.iter():
                    data[strip_ns(el.tag)] = (el.text or "").strip()

                event_type  = data.get("eventType", "")
                event_state = data.get("eventState", "")
                dt          = data.get("dateTime", "")

                now = time.time()
                if now < cooldown_until:
                    continue

                if event_state == "active":
                    print(f"[{dt}] MOTION (VMD): {event_state}")
                    log_motion(dt, 1)

                    # --- ДОБАВЛЕНО: запись клипа ---

                    caption = f"🚨 Motion detected\n📅 {dt}"
                    asyncio.run(send_motion_alert_ptb(caption)) 

                    out_file = record_clip(dt)
                    if out_file:
                        print(f"🎥 clip started: {out_file}")



                    cooldown_until = now + COOLDOWN_SEC

            except Exception as e:
                print("XML parse error:", e)
                print("XML:", xml_text[:300], "...")
