import sys                                    
import importlib.util                       

# First try to load a config file passed on the command line
if len(sys.argv) > 1:
    config_path = sys.argv[1]
    try:
        spec = importlib.util.spec_from_file_location("cli_config", config_path)
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
    except Exception as e:
        print(f"Failed to import CLI config at '{config_path}': {e}")
        # Fall back to user_config / default_config below
        _cli_failed = True
    else:
        _cli_failed = False
else:
    _cli_failed = True

# If no valid CLI config, try user_config, then default_config
if _cli_failed:
    try:
        import user_config as config
    except ImportError:
        print("Could not import user-defined config (user_config.py). Falling back to default config.")
        import default_config as config

from datetime import datetime
import cv2
import glob
import os
import numpy as np
from time import sleep
import src.mon as mon
from zoneinfo import ZoneInfo
from bb_binary.parsing import parse_video_fname



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
    if angle % 360 ==  90:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if angle % 360 == 270:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if abs(angle) % 360 == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    return image  # 0° or unrecognized multiple

def resize_image(image, width):
    """Resizes an image to a given width while maintaining aspect ratio."""
    (height, original_width) = image.shape[:2]
    # Calculate the ratio of the new width to the old width and apply it to the height
    ratio = width / float(original_width)
    new_height = int(height * ratio)
    
    # Resize the image
    resized_image = cv2.resize(image, (width, new_height), interpolation=cv2.INTER_AREA)
    return resized_image

def add_text_to_image(image, text, position=(0.02,0.1), font_scale_relative=0.0015, font_thickness=6):
    """Adds text to an image."""
    # Calculate font scale based on image width
    (height, width) = image.shape[:2]
    font_scale = font_scale_relative * width
    position_scaled = [int(position[0]*width), int(position[1]*height)]

    cv2.putText(image, text, position_scaled, cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), font_thickness, cv2.LINE_AA)
    return image


######
def wait_and_get_images():
    # initialize
    messagebot_counter = 1 

    while True:
        lasttime = datetime.now()
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
                        filename = os.path.basename(videoname)
                        # get camtext by pasing the filename
                        # 1) try the Basler parser
                        try:
                            cam_id, start, end = parse_video_fname(filename, format='basler')
                            # tag it as UTC then convert to CEST
                            start = start.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Berlin"))
                            hour   = f"{start.hour:02d}"
                            minute = f"{start.minute:02d}"
                            day    = f"{start.day:02d}"
                            month  = f"{start.month:02d}"
                            camtext = f"cam{cam_id}  {hour}:{minute}  {day}.{month}"
                        except Exception:
                            # 2) try the Pi‐h264 timestamp
                            try:
                                fname = os.path.basename(filename)
                                timestamp_str = fname.split('_')[-1].replace('.h264', '')
                                camname = fname.split('_')[0]
                                dt = datetime.strptime(timestamp_str, '%Y-%m-%d-%H-%M-%S')
                                # dt is naive GMT → mark & convert
                                # dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Berlin"))
                                hour   = f"{dt.hour:02d}"
                                minute = f"{dt.minute:02d}"
                                day    = f"{dt.day:02d}"
                                month  = f"{dt.month:02d}"
                                camtext = f"{camname} {hour}:{minute}  {day}.{month}"
                            except Exception:
                                # 3) fallback to bare filename
                                camtext = os.path.splitext(os.path.basename(videoname))[0]

                        # rotate image
                        image = rotate_image(image,config.rotate)
                        # Add filename as text to the image
                        stamped_image = add_text_to_image(image, camtext)
                        stamped_images.append(stamped_image)
                    else:
                        stamped_images.append(None)
                composite_image = join_images(stamped_images)
                if composite_image is not None:
                    # composite_image = rotate_image(composite_image,config.rotate)
                    composite_image = resize_image(composite_image,width=config.image_width)
                    composite_image = add_text_to_image(composite_image,config.monitor_bot_name,position=(0.4,0.12),font_scale_relative=0.002)
                    # send image to message bot
                    mon.process_image_and_send(config,composite_image)
                    print('Sent image at',datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                else: # send an error message
                    mon.send_message(config,"Error: "+datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    print('Error at',datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                messagebot_counter = 1
            else:
                messagebot_counter = messagebot_counter + 1

        # correct time to wait by any script processing time
        current_time = datetime.now()
        time_to_wait = config.timer_image_saving*60 - (current_time-lasttime).total_seconds()
        if time_to_wait>0:  # it could be <0 if processing takes a really long time
            sleep(time_to_wait)

if __name__ == "__main__":
    print("Starting...")
    # if mon.send_message(config,config.monitor_bot_name+":  Started bb_monitor"):
    #     print("Telegram message bot connected")
    # else:
    #     print("ERROR: check message bot settings")
    
    wait_and_get_images()