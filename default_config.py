# Monitor bot details
monitor_bot_name = "Hive X"  # name to be displayed on images for monitor bot.  e.g. Hive 1 or Feeder/Exit
telegram_bot_token = "FILL IN API TOKEN"
telegram_chat_id = "FILL IN TELEGRAM CHAT ID"

# Setting timers to wait for checking directories 
timer_image_saving = 1  # in minutes.  Time to wait before getting most recent video and associated image
timer_messagebot_multiplier = 1  # integer.  Multiplies timer_image_saving
 
 # Whether or not to save images
save_images = False 
# List of input and output directories.  Can include multiple cameras
input_basedir = "/Users/jacob/Desktop/v_output_dir"  # this should be set to the same as "output_directory" bb_imgstorage_nfs
input_subdir_names = ["cam0","cam1"]
# images will be saved in subdirectories under output_basedir, with the subdir names
# can be "" if not saving images
output_basedir = "/Users/jacob/Desktop/frames" # images will be saved in subdirectories under this, with the subdir names
file_type = "avi"

# Image formatting for message bot
rotate = 90 # angle for rotating joined camera images.  Use 90 (or -90?) for main cameras, 0 for feeder/exit
image_width = 1024

 