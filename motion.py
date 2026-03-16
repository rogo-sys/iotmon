# добавил zabbix


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
print("CAM_IP:", os.getenv("CAM_IP"))
print("CAM_USR:", os.getenv("CAM_USR"))
print("ZBX_SERVER:", os.getenv("ZBX_SERVER"))

# =========================
# CAMERA / STREAM
# =========================
IP = os.getenv("CAM_IP")
USER = os.getenv("CAM_USR")
PWD = os.getenv("CAM_PWD")

ALERT_STREAM_URL = f"http://{IP}/ISAPI/Event/notification/alertStream"
SNAPSHOT_URL = f"http://{IP}/ISAPI/Streaming/channels/101/picture"
RTSP_URL = f"rtsp://{USER}:{PWD}@{IP}:554/Streaming/Channels/101"

# =========================
# TELEGRAM
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUBSCRIBERS_FILE = Path("/home/it/iotmon/subscribers.json")

# =========================
# PATHS
# =========================
BASE_DIR = Path(__file__).resolve().parent

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_PATH = LOG_DIR / "motion_log.csv"

VIDEO_BASE_DIR = Path("/mnt/usbflash/vidssub")
VIDEO_BASE_DIR.mkdir(parents=True, exist_ok=True)

# =========================
# TIMINGS
# =========================
COOLDOWN_SEC = 15          # большой cooldown только 1 раз после старта события
LOCAL_CHECK_SEC = 5       # окно проверки: осталось ли движение
CLIP_SECONDS = 15         # длина видео
RECONNECT_DELAY = 5
HTTP_TIMEOUT = (10, 60)

# =========================
# STATES
# =========================
STATE_IDLE = "idle"
STATE_COOLDOWN = "cooldown"
STATE_CHECK_WINDOW = "check_window"

# =========================
# ZABBIX
# =========================
ZBX_SERVER = os.getenv("ZBX_SERVER")
ZBX_HOSTNAME = "rpi_nord"
ZBX_KEY = "motion.status"


def send_zabbix_simple(value):
    command = [
        "zabbix_sender",
        "-z", ZBX_SERVER,
        "-s", ZBX_HOSTNAME,
        "-k", ZBX_KEY,
        "-o", str(value)
    ]

    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print(f"Zabbix sender: host={ZBX_HOSTNAME}, key={ZBX_KEY}, value={value}")
    except FileNotFoundError:
        print("Ошибка: утилита zabbix_sender не найдена в системе.")
    except Exception as e:
        print(f"Ошибка запуска zabbix_sender: {e}")


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def parse_event(xml_text: str) -> dict | None:
    try:
        root = ET.fromstring(xml_text)
        data = {}
        for el in root.iter():
            data[strip_ns(el.tag)] = (el.text or "").strip()
        return data
    except ET.ParseError:
        return None


def dt_from_camera_text(dt_text: str | None) -> datetime:
    if dt_text:
        try:
            return datetime.fromisoformat(dt_text)
        except Exception:
            pass
    return datetime.now().astimezone()


def log_motion(dt_text: str | None, status: int) -> None:
    dt = dt_from_camera_text(dt_text)

    date_s = dt.strftime("%Y-%m-%d")
    time_s = dt.strftime("%H:%M:%S")
    unix_s = int(dt.timestamp())

    file_exists = LOG_PATH.exists()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        if not file_exists:
            f.write("date,time,unix,status\n")
        f.write(f"{date_s},{time_s},{unix_s},{status}\n")


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
        SNAPSHOT_URL,
        auth=HTTPDigestAuth(USER, PWD),
        timeout=10,
    )
    response.raise_for_status()

    photo = BytesIO(response.content)
    photo.name = "snapshot.jpg"
    photo.seek(0)
    return photo

def save_snapshot_to_disk(photo: BytesIO, save_dir: str = "/mnt/usbflash") -> str:
    Path(save_dir).mkdir(parents=True, exist_ok=True)

    #ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    #file_path = Path(save_dir) / f"snapshot_{ts}.jpg"
    file_path = Path(save_dir) / "snapshot.jpg"

    photo.seek(0)
    with open(file_path, "wb") as f:
        f.write(photo.read())

    photo.seek(0)  # чтобы потом еще можно было отправить в Telegram
    return str(file_path)    


async def send_motion_alert_ptb(caption: str):
    subscribers = load_subscribers()
    if not subscribers:
        print("No subscribers for motion alerts")
        return

    if not BOT_TOKEN:
        print("BOT_TOKEN is empty")
        return

    bot = Bot(token=BOT_TOKEN)

    try:
        photo = get_camera_snapshot()
        saved_path = save_snapshot_to_disk(photo, "/mnt/usbflash")
        print("Saved to:", saved_path)
    except Exception as e:
        print("snapshot error:", e)

    for chat_id in subscribers:
        try:
            await bot.send_message(chat_id=chat_id, text=caption)
            print(f"Text alert sent to {chat_id}")
        except Exception as e:
            print(f"send_message error to {chat_id}: {e}")


def record_clip(event_dt_text: str | None = None) -> Path | None:
    dt = dt_from_camera_text(event_dt_text)

    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H-%M-%S")

    out_dir = VIDEO_BASE_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{time_str}.mp4"

    cmd = [
        "ffmpeg",
        "-y",
        "-rtsp_transport", "tcp",
        "-i", RTSP_URL,
        "-t", str(CLIP_SECONDS),
        "-c", "copy",
        str(out_file),
    ]

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return out_file
    except Exception as e:
        print("ffmpeg start error:", e)
        return None


def handle_motion_start(event_dt_text: str):
    print(f"[{event_dt_text}] MOTION START")
    log_motion(event_dt_text, 1)

    send_zabbix_simple(1)

    pretty_dt = format_camera_datetime(event_dt_text)
    caption = f"🚨 Motion detected\n📅 {pretty_dt}"

    try:
        asyncio.run(send_motion_alert_ptb(caption))
    except Exception as e:
        print("telegram send error:", e)

    out_file = record_clip(event_dt_text)
    if out_file:
        print(f"🎥 clip started: {out_file}")

def format_camera_datetime(dt_text: str | None) -> str:
    dt = dt_from_camera_text(dt_text)
    return dt.strftime("%d/%m/%Y - %H:%M:%S")

def handle_motion_end():
    now_text = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{now_text}] MOTION END")
    log_motion(now_text, 0)
    send_zabbix_simple(0)

def reset_motion_state_on_start():
    now_text = datetime.now().astimezone().isoformat(timespec="seconds")
    #print(f"[{now_text}] STARTUP RESET -> MOTION = 0")
    log_motion(now_text, 0)
    send_zabbix_simple(0)

def main():
    state = STATE_IDLE
    cooldown_until = 0.0
    local_check_until = 0.0
    motion_seen_in_window = False

    print("Starting motion monitor...")
    print(f"CSV log: {LOG_PATH}")
    print(f"Video dir: {VIDEO_BASE_DIR}")
    print(f"Cooldown: {COOLDOWN_SEC}s | Check window: {LOCAL_CHECK_SEC}s")

    reset_motion_state_on_start()

    while True:
        try:
            with requests.get(
                ALERT_STREAM_URL,
                auth=HTTPDigestAuth(USER, PWD),
                stream=True,
                timeout=HTTP_TIMEOUT,
                headers={"Accept": "application/xml"},
            ) as response:
                response.raise_for_status()
                print("Connected to camera event stream")

                buffer = ""

                for chunk in response.iter_content(chunk_size=1024):
                    now_ts = time.time()

                    # -------------------------
                    # обработка истечения cooldown / check window
                    # -------------------------
                    if state == STATE_COOLDOWN and now_ts >= cooldown_until:
                        state = STATE_CHECK_WINDOW
                        motion_seen_in_window = False
                        local_check_until = now_ts + LOCAL_CHECK_SEC
                        print(f"--> Enter CHECK_WINDOW for {LOCAL_CHECK_SEC}s")

                    elif state == STATE_CHECK_WINDOW and now_ts >= local_check_until:
                        if motion_seen_in_window:
                            motion_seen_in_window = False
                            local_check_until = now_ts + LOCAL_CHECK_SEC
                            print(f"--> Motion still present, extend CHECK_WINDOW by {LOCAL_CHECK_SEC}s")
                        else:
                            handle_motion_end()
                            state = STATE_IDLE
                            print("--> Back to IDLE")

                    if not chunk:
                        continue

                    buffer += chunk.decode("utf-8", errors="ignore")

                    while True:
                        start_tag = "<EventNotificationAlert"
                        end_tag = "</EventNotificationAlert>"

                        start_idx = buffer.find(start_tag)
                        end_idx = buffer.find(end_tag)

                        if start_idx == -1 or end_idx == -1:
                            break

                        xml_text = buffer[start_idx:end_idx + len(end_tag)]
                        buffer = buffer[end_idx + len(end_tag):]

                        data = parse_event(xml_text)
                        if not data:
                            continue

                        event_type = data.get("eventType", "").lower()
                        event_state = data.get("eventState", "").lower()
                        event_dt = data.get("dateTime", "")

                        # игнорируем мусор / неактуальное
                        if event_type == "videoloss":
                            continue

                        if event_state != "active":
                            continue

                        # здесь считаем, что active = движение
                        if state == STATE_IDLE:
                            handle_motion_start(event_dt)
                            cooldown_until = time.time() + COOLDOWN_SEC
                            state = STATE_COOLDOWN
                            print(f"--> Enter COOLDOWN for {COOLDOWN_SEC}s")

                        elif state == STATE_COOLDOWN:
                            # во время большого cooldown просто игнорим active
                            pass

                        elif state == STATE_CHECK_WINDOW:
                            # не запускаем большой cooldown снова
                            # просто помечаем, что движение в окне было
                            motion_seen_in_window = True
                            print(f"[{event_dt}] active seen inside CHECK_WINDOW")

        except requests.RequestException as e:
            print(f"[HTTP ERROR] {e}")

            # если поток порвался во время проверки конца события,
            # лучше не закрывать событие сразу, а просто переподключиться
            # чтобы не получить ложный status=0
            time.sleep(RECONNECT_DELAY)

        except KeyboardInterrupt:
            print("\nStopped by user")
            break

        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    main()