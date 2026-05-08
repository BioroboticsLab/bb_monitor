import importlib
import importlib.util
import os
import sys
import tempfile
from datetime import datetime

import cv2
import requests


def load_config_from_path(path):
    """Load a Python config module from an explicit filesystem path."""
    spec = importlib.util.spec_from_file_location(f"cfg_{os.path.basename(path)}", path)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    return cfg


def get_config(default_module="default_config", user_module="user_config"):
    """Load config: CLI-arg path -> user_module -> default_module."""
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
        try:
            return load_config_from_path(config_path)
        except Exception as e:
            print(f"Failed to import CLI config at '{config_path}': {e}")
    try:
        return importlib.import_module(user_module)
    except ImportError:
        print(f"Could not import {user_module}.py. Falling back to {default_module}.")
        return importlib.import_module(default_module)


def process_image_and_send(config, image):
    temp_dir = tempfile.mkdtemp()
    temp_image_path = os.path.join(
        temp_dir,
        config.monitor_bot_name + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + '.png',
    )
    cv2.imwrite(temp_image_path, image)

    response = send_photo(config, temp_image_path)

    os.remove(temp_image_path)
    os.rmdir(temp_dir)
    return response


def send_message(config, message):
    send_url = f'https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage'
    data = {'chat_id': config.telegram_chat_id, 'text': config.monitor_bot_name + ':  ' + message}
    response = requests.post(send_url, data=data).json()
    if not response['ok']:
        print("Message not sent")
    return response['ok']


def send_photo(config, file, caption=""):
    params = {'chat_id': config.telegram_chat_id, 'caption': caption}
    try:
        file_opened = open(file, 'rb')
    except:
        return None
    files = {'photo': file_opened}
    send_url = f'https://api.telegram.org/bot{config.telegram_bot_token}/sendPhoto'
    response = requests.post(send_url, params, files=files)
    return response.json()
