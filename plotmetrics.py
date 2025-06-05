try:
    import user_config as config
except:
    print("Could not import user-defined config (user_config.py). Falling back to default config.")
    import default_config as config

from datetime import datetime
import matplotlib.pyplot as plt
from time import sleep
import src.data_processing as dp
import src.plotting as pt
import src.mon as mon

# Directories and settings
savedir = '/mnt/local_storage/beesbook2024/single_tracks/'
rpi_savedir = '/pi/single_tracks/'
temperature_data_folder = '/home/beesbook/bb2024/bb_temploggers/data/'
numdays = 6
window_size_hours = 24  # for counting unique id's detected
station_id = '10381'  # weather station ID, for use in meteostat
feedercamhives = {'f0': 'A', 'f1': 'B'}

def load_data_and_send_plots():
    # Load and process detections data
    df_results, df_meancounts, df_perbee = dp.get_detections_data(savedir, numdays, window_size_hours=window_size_hours)
    # RPi counts at feeder
    dfcounts = dp.get_rpi_feeder_cam_data(rpi_savedir, numdays)
    # Weather data
    hourdata = dp.get_weather_data(station_id, numdays)
    # Temperature data
    combined_data, hex_codes_dict, label_dict = dp.get_temperature_data(temperature_data_folder, numdays)

    # get min and max dates that have data, for any sources
    # List of DataFrame and column name pairs
    df_time_columns = [
        (hourdata.reset_index(), 'time'),
        (combined_data, 'Time'),
        (dfcounts, 'video_start_timestamp'),
        (df_results, 'timestamp_of_segment')
    ]

    # Initialize mintime and maxtime with None
    mintime = None
    maxtime = None

    for df, column in df_time_columns:
        if not df.empty:
            current_min = df[column].min()
            current_max = df[column].max()
            
            if mintime is None or current_min < mintime:
                mintime = current_min
            if maxtime is None or current_max > maxtime:
                maxtime = current_max

    ### PLOT 1:  detections and speed
    f, ax = plt.subplots(3, 1, figsize=(5, 10), sharex=True)
    # Unique bees detected
    a=ax[0]
    pt.plot_detections(a,df_results,'counts_window')
    a.set_ylabel('Tagged bees\n('+str(window_size_hours)+' hr window)',fontsize=14)
    # Number of detections
    a=ax[1]
    pt.plot_detections(a,df_results,'TotalCounts')
    a.set_ylabel('Total detections\nper image',fontsize=14)
    # Speed
    a=ax[2]
    pt.plot_detections(a,df_results,'avg_speed_per_bee') 
    # pt.plot_detections(a,df_results,'avg_speed_weighted') #  this and per_bee show about the same thing
    a.set_ylabel('Speed (pixels/sec)',fontsize=14)
    ### common formatting
    pt.common_plot_formatting(ax,mintime,maxtime,window_size_hours)  
    plt.suptitle(str(datetime.now())[5:-7],y=0.98)
    plt.tight_layout()
    mon.process_image_and_send(config,f)
    plt.close()


    ### PLOT 1:  Weather and temperature data
    f, ax = plt.subplots(3, 1, figsize=(5, 10), sharex=True)
    # Feeder cam counts
    a=ax[0]
    feedercamhives = {'f0':'A', 'f1':'B'}
    pt.plot_feedercam_counts(a,dfcounts,feedercamhives,minutes_to_avg=15)
    # Plot temperature and precipitation
    a=ax[1]
    pt.plot_temp_and_precip(a,hourdata)
    # plot temperature in the hives
    a=ax[2]
    pt.plot_temperature_sensors(a,combined_data,label_dict)
    ### common formatting
    pt.common_plot_formatting(ax,mintime,maxtime,window_size_hours)
    plt.suptitle(str(datetime.now())[5:-7],y=0.98)
    plt.tight_layout()
    mon.process_image_and_send(config,f)
    plt.close()

if __name__ == "__main__":
    while True:
        lasttime = datetime.now()
        load_data_and_send_plots()
        # correct time to wait by any script processing time
        current_time = datetime.now()
        time_to_wait = config.timer_image_saving*60 - (current_time-lasttime).total_seconds()
        if time_to_wait>0:  # it could be <0 if processing takes a really long time
            sleep(time_to_wait) 