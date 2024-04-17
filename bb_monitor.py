try:
    import user_config as config
except:
    print("Could not import user-defined config (user_config.py). Falling back to default config.")
    import default_config as config

import requests
import datetime
import cv2
import glob
import os
import numpy as np
from time import sleep
import tempfile

def find_most_recent_files(base_directory, sub_directories, file_type):
    """Finds the most recent files across subdirectories within the latest date directory."""
    # List all date directories in the base directory
    date_dirs = [os.path.join(base_directory, d) for d in os.listdir(base_directory) if os.path.isdir(os.path.join(base_directory, d))]
    if not date_dirs:
        return None

    # Find the latest date directory
    latest_date_dir = max(date_dirs, key=os.path.getmtime)

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

def extract_first_frame(video_path):
    """Extracts the first frame from the given video file."""
    cap = cv2.VideoCapture(video_path)
    success, image = cap.read()
    cap.release()
    return image if success else None

def join_images(images):
    """Joins a list of images vertically."""
    # Remove any None items from the list
    images = [img for img in images if img is not None]
    # Check if there are any images to join
    if not images:
        return None
    # Vertical stacking
    return np.vstack(images)

def rotate_image(image, angle):
    """Rotates an image by a given angle in degrees."""
    (h, w) = image.shape[:2]
    center = (w // 2, h // 2)
    # Rotate the image by the specified angle
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(image, M, (w, h))
    return rotated

def resize_image(image, width):
    """Resizes an image to a given width while maintaining aspect ratio."""
    (height, original_width) = image.shape[:2]
    # Calculate the ratio of the new width to the old width and apply it to the height
    ratio = width / float(original_width)
    new_height = int(height * ratio)
    
    # Resize the image
    resized_image = cv2.resize(image, (width, new_height), interpolation=cv2.INTER_AREA)
    return resized_image

def add_text_to_image(image, text, position=(0.02,0.02), font_scale_relative=0.0007, font_thickness=1):
    """Adds text to an image."""
    # Calculate font scale based on image width
    (height, width) = image.shape[:2]
    font_scale = font_scale_relative * width
    position_scaled = [position[0]*width, position[1]*height]

    cv2.putText(image, text, position_scaled, cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), font_thickness, cv2.LINE_AA)
    return image

def send_message(message):
    send_url = f'https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage'
    data = {'chat_id': config.telegram_chat_id, 'text': config.monitor_bot_name+':  '+message}    
    response = requests.post(send_url, data=data).json()
    if not(response['ok']):
        print("Message not sent")
    return response['ok']

def send_photo(file, caption=""):
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

def process_image_and_send(image):
    # Save the image to a temporary directory
    temp_dir = tempfile.mkdtemp()
    temp_image_path = os.path.join(temp_dir, config.monitor_bot_name+datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")+'.png')
    cv2.imwrite(temp_image_path, image)

    # Send the image
    response = send_photo(temp_image_path)
    # could check for success here

    # Delete the image file and directory now that its been sent
    os.remove(temp_image_path)
    os.rmdir(temp_dir)
    return response

######
def wait_and_get_images():
    # initialize
    messagebot_counter = 1 

    while True:
        lasttime = datetime.datetime.now()
        sendmsgnow = (messagebot_counter==config.timer_messagebot_multiplier)

        if (config.save_images==True) or (sendmsgnow==True):
            latest_videos = find_most_recent_files(config.input_basedir, config.input_subdir_names, config.file_type)
            images = [extract_first_frame(vid) for vid in latest_videos]
            if config.save_images: # save each image to associated output directory
                for image,videoname,subdir in zip (images, latest_videos, config.input_subdir_names):
                    if image is not None:
                        image_name = os.path.splitext(os.path.basename(videoname))[0] + ".png"
                        savedir = os.path.join(config.output_basedir,subdir)
                        if not os.path.exists(savedir):
                            os.makedirs(savedir)
                        cv2.imwrite(os.path.join(savedir,image_name), image)

            if sendmsgnow:
                # Prepare to make a composite image
                # Stamp each image with its filename before joining
                stamped_images = []
                for image, videoname in zip(images, latest_videos):
                    if image is not None:
                        # Extract the filename without extension
                        filename = os.path.splitext(os.path.basename(videoname))[0]
                        # Add filename as text to the image
                        stamped_image = add_text_to_image(image, filename)
                        stamped_images.append(stamped_image)
                    else:
                        stamped_images.append(None)
                composite_image = join_images(stamped_images)
                if composite_image is not None:
                    composite_image = rotate_image(composite_image,config.rotate)
                    composite_image = resize_image(composite_image,width=config.image_width)
                    composite_image = add_text_to_image(composite_image,config.monitor_bot_name,position=(0.5,0.02),font_scale_relative=0.0012)
                    # send image to message bot
                    process_image_and_send(composite_image)
                    print('Sent image at',datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                else: # send an error message
                    send_message("Error: "+datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    print('Error at',datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                messagebot_counter = 1
            else:
                messagebot_counter = messagebot_counter + 1

        # correct time to wait by any script processing time
        current_time = datetime.datetime.now()
        time_to_wait = config.timer_messagebot_multiplier*60 - (current_time-lasttime).total_seconds()
        if time_to_wait>0:  # it could be <0 if processing takes a really long time
            sleep(time_to_wait)

if __name__ == "__main__":
    print("Starting...")
    # if send_message(config.monitor_bot_name+":  Started bb_monitor"):
    #     print("Telegram message bot connected")
    # else:
    #     print("ERROR: check message bot settings")
    
    wait_and_get_images()