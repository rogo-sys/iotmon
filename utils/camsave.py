# -*- coding: utf-8 -*-
import subprocess
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


CAMERA_USER = os.getenv("CAM_USR")
CAMERA_PASS = os.getenv("CAM_PWD")

rtsp_url = f"rtsp://{CAMERA_USER}:{CAMERA_PASS}@192.168.1.64:554/Streaming/Channels/102"

# Дата (год-месяц-день) для подпапки
date_str = datetime.now().strftime("%Y-%m-%d")
# Время (часы-минуты-секунды) для имени файла
time_str = datetime.now().strftime("%H-%M-%S")

# Создаём папку если её нет
#output_dir = os.path.join(os.getcwd(), date_str)
output_dir = os.path.join("/mnt/usbflash/vids", date_str)
os.makedirs(output_dir, exist_ok=True)

# Полный путь к файлу
output_file = os.path.join(output_dir, f"{time_str}.mp4")


# ffmpeg пишет поток как есть (без перекодирования) и обрезает по 60 секунд
cmd = [
    "ffmpeg",
    "-i", rtsp_url,
    "-t", "60",
    "-c", "copy",
    output_file
]

subprocess.run(cmd)
print(f"✅ Запись завершена: {output_file}")

