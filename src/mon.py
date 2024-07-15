import glob
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import requests
import re
import tempfile
import matplotlib.pyplot as plt
import os
import cv2


###############################################
## fns for telegram message sending
###############################################
def process_image_and_send(config,image):
    # Save the image to a temporary directory
    temp_dir = tempfile.mkdtemp()
    temp_image_path = os.path.join(temp_dir, config.monitor_bot_name+datetime.now().strftime("%Y-%m-%d %H:%M:%S")+'.png')
    
    # Check the type of the image and save accordingly
    try:
        if isinstance(image, plt.Figure):
            image.savefig(temp_image_path)
        else:
            cv2.imwrite(temp_image_path, image)
    except Exception as e:
        raise ValueError("Unsupported image type. Use a Matplotlib figure or OpenCV image.") from e

    # Send the image
    response = send_photo(config,temp_image_path)
    # could check for success here

    # Delete the image file and directory now that its been sent
    os.remove(temp_image_path)
    os.rmdir(temp_dir)
    return response

def send_message(config,message):
    send_url = f'https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage'
    data = {'chat_id': config.telegram_chat_id, 'text': config.monitor_bot_name+':  '+message}    
    response = requests.post(send_url, data=data).json()
    if not(response['ok']):
        print("Message not sent")
    return response['ok']

def send_photo(config,file, caption=""):
    """
    Sends a file:
    
    :param bot_token: Telegram Bot API token
    :param chat_id: Chat ID
    :param file: file name
    :param caption:  optional, add a caption
    :return: resp.
    """    
    params = {'chat_id': config.telegram_chat_id, 'caption': caption}
    try:
        file_opened = open(file,'rb')
    except:
        return None
    files = {'photo': file_opened}
    send_url = f'https://api.telegram.org/bot{config.telegram_bot_token}/sendPhoto'
    response = requests.post(send_url, params, files=files)
    return response.json()


###############################################
## fns for filename handling and misc
###############################################
def parse_date_from_filename_single_tracks(filename):
    patterns = [
        '%Y%m%d_%H%M%S',
        '%Y-%m-%d_%H-%M'
    ]
    
    # Define regex to identify possible date patterns
    regex_patterns = [
        r'(\d{8}_\d{6})',  # Matches YYYYMMDD_HHMMSS
        r'(\d{4}-\d{2}-\d{2}_\d{2}-\d{2})'  # Matches YYYY-MM-DD_HH-MM
    ]
    
    for pattern, regex in zip(patterns, regex_patterns):
        match = re.search(regex, filename)
        if match:
            date_str = match.group(1)
            date_obj = datetime.strptime(date_str, pattern)
            return date_obj


def find_most_recent_files(base_directory, sub_directories, file_type):
    """Finds the most recent files across subdirectories within the latest date directory."""
    # List all date directories in the base directory
    current_year = str(datetime.now().year) 
    date_dirs = [os.path.join(base_directory, d) for d in os.listdir(base_directory)
             if os.path.isdir(os.path.join(base_directory, d)) and d.startswith(current_year)]    
    if not date_dirs:
        return None

    # Find the latest date directory
    latest_date_dir = max(date_dirs)

    # Check each camera subdirectory within the latest date directory
    most_recent_files = []
    for subdir in sub_directories:
        path = os.path.join(latest_date_dir, subdir)
        files = glob.glob(os.path.join(path,'*'+file_type))
        if files:
            latest_file = max(files, key=os.path.getmtime)
            most_recent_files.append(latest_file)
        else:
            most_recent_files.append(None)

    return most_recent_files

def get_latest_processed_files(basename, numhours):
    current_time = datetime.now()

    filtered_files = []  # after filtering based on the timestamps
    # Get the list of files
    list_of_files = sorted(glob.glob(basename + '*.pkl'))

    # Iterate over the files and filter based on the timestamp in the filename
    for file in list_of_files:
        # Extract the timestamp from the filename
        timestamp_str = file.replace(basename,'').replace('.pkl', '')
        # timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d_%H-%M')
        timestamp = parse_date_from_filename_single_tracks(timestamp_str)

        # Check if the file's timestamp is within the specified number of hours
        if current_time - timestamp <= timedelta(hours=numhours):
            filtered_files.append(file)

    return sorted(filtered_files)

def mean_cam_timestamp(ts):
    # timestamps for two cameras.  calculate the difference, and if its too large, return nan
    if len(ts)>1:
        diff = ts.values[1]-ts.values[0]
        if diff>pd.Timedelta(minutes=60):
            return np.nan
    return ts.mean()

