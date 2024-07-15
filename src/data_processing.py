import os
import pandas as pd
import pickle
from datetime import datetime, timedelta
from meteostat import Hourly
import json
import src.mon as mon
import numpy as np

detection_types = ['BeeInCell', 'TaggedBee', 'UnmarkedBee', 'UpsideDownBee']

###############################################
## fns to filter and calculate things from the data
###############################################
def round_down_to_nearest_hour(dt_series):
    # Ensure datetime series is timezone-naive for accurate calculations
    dt_series = pd.to_datetime(dt_series)
    if dt_series.dt.tz is not None:
        dt_series = dt_series.dt.tz_convert(None)

    # Round down to the nearest hour by flooring the datetime to the hour
    dt_series = dt_series.dt.floor('h')
    return dt_series

def calculate_speed(group):
    max_speed = 200 # pixels/frame, max
    group = group.sort_values(by='timestamp_posix')
    group['speed'] = np.sqrt(group['x_pixels'].diff()**2 + group['y_pixels'].diff()**2) / group['timestamp_posix'].diff()
    # Set speed to NaN where cam_id changes
    group['speed'] = np.where(group['cam_id'] != group['cam_id'].shift(), np.nan, group['speed'])
    # Set speed to NaN over a threshold, 
    group.loc[group['speed']>max_speed,'speed'] = np.nan
    
    return group['speed'].mean()

def calculate_stats(group):
    num_det = group['num_detections'].sum()
    if num_det>0:
        weighted_speed = np.sum(group['average_speed']*group['num_detections'])/num_det
    else:
        weighted_speed = np.nan    
    return pd.Series({
        'counts': group.shape[0],
        'detect_per_frame': num_det/6/60,
        'avg_speed_per_bee': group['average_speed'].mean(),
        'avg_speed_weighted': weighted_speed
    })


def filter_tracks(results, confidence_threshold=0.9, min_detections=5):    
    df_meancounts = pd.DataFrame(columns=['timestamp', 'timestamp_type', 'cam_id']+detection_types)    
    df_alltracks = pd.DataFrame()

    for result in results:
        video_dataframe, tracks_df, cam_id = result
        video_dataframe['cam_id'] = cam_id
        tracks_df['cam_id'] = cam_id
        # all detections, average per frame. calculate and save in a df
        meancounts = video_dataframe.groupby(['frameIdx', 'detection_type']).size().unstack(fill_value=0).mean()
        for detection_type in detection_types:
            df_meancounts.at[cam_id, detection_type] = meancounts[detection_type]        
        df_meancounts.at[cam_id, 'cam_id'] = cam_id 
        df_meancounts.at[cam_id, 'timestamp'] = video_dataframe['video_start_timestamp'].reset_index(drop=True)[0]
        df_meancounts['timestamp_type'] = 'camera_filename'
        

        df_alltracks = pd.concat((df_alltracks,tracks_df))
    # assign hive_id
    df_alltracks.loc[(df_alltracks['cam_id']==0)|(df_alltracks['cam_id']==1),'hive_id'] = 'A'
    df_alltracks.loc[(df_alltracks['cam_id']==2)|(df_alltracks['cam_id']==3),'hive_id'] = 'B'
    df_meancounts.loc[(df_meancounts['cam_id']==0)|(df_meancounts['cam_id']==1),'hive_id'] = 'A'
    df_meancounts.loc[(df_meancounts['cam_id']==2)|(df_meancounts['cam_id']==3),'hive_id'] = 'B'    
    
    df_alltracks = df_alltracks[df_alltracks['bee_id_confidence']>confidence_threshold]
    
    # number of detections per unique bee - both hives
    num_detections = df_alltracks.groupby('bee_id')['bee_id'].apply(len)
    df_perbee = pd.DataFrame(num_detections).rename(columns={"bee_id": 'num_detections'}).reset_index()
    
    # filter bees detected in both hives, remove these
    df_perbee['hive'] = df_alltracks.groupby('bee_id')['hive_id'].apply(lambda x: x.mode()[0]).values
    # filter out bees with only 1 detection.
    df_perbee = df_perbee[df_perbee['num_detections'] >= min_detections]

    # filter df_alltracks to only keep bee_id where hive_id is the 'dominant_hive'
    df_alltracks = df_alltracks.merge(df_perbee[['bee_id', 'hive']], on='bee_id')
    df_alltracks = df_alltracks[df_alltracks['hive_id'] == df_alltracks['hive']]
    df_alltracks = df_alltracks.drop(columns=['hive'])

    # if there is a duplicate timestamp for a bee, keep the last one (which will have the higher confidence)
    df_alltracks.sort_values(['timestamp_posix', 'detection_confidence'], ascending=[True, True], inplace=True)
    df_alltracks = df_alltracks.drop_duplicates(subset=['bee_id', 'timestamp_posix'],keep='last')
    
    return df_meancounts, df_alltracks

def get_perbee_df(filename,filename_to_save):
    results = pickle.load(open(filename,'rb'))
    # segment timestamp
   
    df_meancounts, df_alltracks = filter_tracks(results)
    
    # number of detections per unique bee - now this will be >=2
    num_detections = df_alltracks.groupby('bee_id')['bee_id'].apply(len)
    df_perbee = pd.DataFrame(num_detections).rename(columns={"bee_id": 'num_detections'}).reset_index()
    
    # Ensure each bee_id is associated with only one hive_id
    unique_hive_ids = df_alltracks.groupby('bee_id')['hive_id'].unique().apply(lambda x: x[0]).reset_index()
    # Merge the hive_id with df_perbee
    df_perbee = df_perbee.merge(unique_hive_ids, on='bee_id')
    # speed
    df_perbee['average_speed'] = df_alltracks.groupby('bee_id').apply(calculate_speed,include_groups=False).values

    # round to the nearest 15 minutes to save timestamp of segment
    mean_ts = df_alltracks.groupby('bee_id')['video_start_timestamp'].apply(mon.mean_cam_timestamp)
    df_perbee['timestamp_of_segment'] = round_down_to_nearest_hour(mean_ts).values
    
    pickle.dump([df_meancounts, df_perbee], open(filename_to_save,'wb'))

###############################################
#### RPi and feeder cam analysis functions
###############################################
def get_average_counts(file):
    df = pd.read_pickle(file)    
    if len(df)==0:
        return pd.DataFrame()
    else:
        # Define the lambda function
        getcounts = lambda x: pd.Series({
            'totalcounts': len(x),
            'untaggedcounts': np.sum(x['detection_type']=='UnmarkedBee'),
            'taggedcounts': np.sum(x['detection_type']=='TaggedBee')
        })
        # Apply the lambda function and reset the index
        results = df.groupby(['camID','video_start_timestamp','frameIdx']).apply(getcounts,include_groups=False).reset_index()
        # Group by video_start_timestamp and calculate the average counts
        average_counts = results.groupby(['camID','video_start_timestamp']).agg({
            'totalcounts': 'mean',
            'untaggedcounts': 'mean',
            'taggedcounts': 'mean'
        }).reset_index()
        return average_counts
    

def get_detections_data(savedir, numdays, window_size_hours=24, mincounts=10, re_process=False):
    allfilenames  = mon.get_latest_processed_files(savedir,numdays*24)
    
    # check if all have been processed
    for filename in allfilenames:
        filename_to_save = savedir+'processed/'+'df_counts_and_perbee_'+filename.split('/')[-1]
        if (not(os.path.exists(filename_to_save))) | re_process:
            print('processing ',filename.split('/')[-1])
            get_perbee_df(filename,filename_to_save)
        
    files = mon.get_latest_processed_files(savedir+'processed/'+"df_counts_and_perbee_",numhours=numdays*24)
    df_meancounts = pd.DataFrame()
    df_perbee = pd.DataFrame()
    for file in files:
        mc, pb = pickle.load(open(file,'rb'))
        df_meancounts = pd.concat((df_meancounts,mc))
        df_perbee = pd.concat((df_perbee,pb))
    df_meancounts = df_meancounts.reset_index(drop=True)
    df_perbee = df_perbee.reset_index(drop=True)
    
    df_results = df_perbee.groupby(['timestamp_of_segment', 'hive_id']).apply(calculate_stats, include_groups=False).reset_index()
    df_results['timestamp_of_segment'] = pd.to_datetime(df_results['timestamp_of_segment'])
    
    # round camera times to nearest 15 min and sum over counts during this time period
    mc_to_merge = df_meancounts.copy()
    # round to nearest 15 min
    mc_to_merge['timestamp_of_segment'] = round_down_to_nearest_hour(mc_to_merge['timestamp']).values
    mc_to_merge['timestamp_of_segment'] = pd.to_datetime(mc_to_merge['timestamp_of_segment'])
    # take mean across timestamps.  do mean instead of sum, because this correct for multiple timestamps in a segment
    cameras_per_hive = 2
    mc_to_merge = mc_to_merge.groupby(['timestamp_of_segment','hive_id'])[detection_types].mean().reset_index()
    mc_to_merge['TotalCounts'] = mc_to_merge[detection_types].sum(axis=1) * cameras_per_hive  # multiple
    
    df_results = df_results.merge(mc_to_merge,on=['timestamp_of_segment','hive_id'])
    
    
    # Iterate through each row of df_results
    for idx, row in df_results.iterrows():
        # Define the current timestamp and hive_id
        timestamp = row['timestamp_of_segment']
        # Define the time window and filter data in the window
        start_time = timestamp - pd.Timedelta(hours=window_size_hours)   
        window_data = df_perbee[(df_perbee['hive_id'] == row['hive_id']) & 
                                (df_perbee['timestamp_of_segment'] >= start_time) & 
                                (df_perbee['timestamp_of_segment'] <= timestamp)]
    
        counts = window_data.groupby('bee_id')['num_detections'].sum()
        df_results.at[idx, 'counts_window'] = np.sum(counts>=mincounts) 
    return df_results, df_meancounts, df_perbee

def get_rpi_feeder_cam_data(savedir, numdays):
    files = mon.get_latest_processed_files(savedir, numdays*24)
    dfcounts = pd.concat([get_average_counts(f) for f in files])
    dfcounts['video_start_timestamp'] = pd.to_datetime(dfcounts['video_start_timestamp'])
    return dfcounts

def get_weather_data(station_id, numdays):
    end = datetime.now()
    start = end - timedelta(days=numdays)
    hourdata = Hourly(station_id, start, end)
    hourdata = hourdata.fetch()
    return hourdata

def get_temperature_data(data_folder, numdays):
    json_file_path = data_folder+'hexcodes_locations.json'
    with open(json_file_path, 'r') as f:
        hex_codes_dict = json.load(f)

    # Expected headers derived from the hex codes in the JSON file
    expected_headers = ['Time'] + sorted(['Temp'+key for hive in hex_codes_dict.values() for key in hive.keys()])
    
    
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=numdays)
    
    # Function to check if the first line is a header
    def is_header(file_path):
        with open(file_path, 'r') as file:
            first_line = file.readline().strip().split(',')
            return first_line[0] == 'Time'
    
    # List files from the last numdays days
    csv_files = []
    for i in range(numdays + 1):
        day = (end_date - timedelta(days=i)).strftime('%Y-%m-%d')
        file_name = f'temperature_data_{day}.csv'
        file_path = os.path.join(data_folder, file_name)
        if os.path.exists(file_path):
            csv_files.append(file_path)
    
    # Read in all data files and combine them into a single DataFrame
    data_frames = []
    for file_path in csv_files:
        if is_header(file_path):
            df = pd.read_csv(file_path, parse_dates=['Time'])
            if not(np.all(df.columns)==expected_headers):
                print('ERROR:  header columns do not match:',file_path)
        else:
            df = pd.read_csv(file_path, header=None, parse_dates=[0])
            df.columns = expected_headers
        data_frames.append(df)
    
    # Combine all data frames into one
    combined_data = pd.concat(data_frames, ignore_index=True)
    
    # Filter out rows where 'Time' column contains the string 'Time'
    combined_data = combined_data[~combined_data['Time'].astype(str).str.contains('Time')]
    
    # Ensure 'Time' column is datetime
    combined_data['Time'] = pd.to_datetime(combined_data['Time'])
    combined_data = combined_data.sort_values(by='Time')
    
    
    # 1) Remove values with temperature changes greater than max_temp_diff
    max_temp_diff = 5
    for col in combined_data.columns[1:]:  # Skip 'Time' column
        combined_data[col] = combined_data[col].astype(float)
        combined_data = combined_data[(combined_data[col].diff().abs() <= max_temp_diff) | (combined_data[col].diff().isnull())]
    
    # 2) Apply a moving average filter with a default window of 5 minutes
    combined_data.set_index('Time', inplace=True)
    avgwindow_minutes = 30
    combined_data = combined_data.rolling(str(avgwindow_minutes)+'min').mean().reset_index()

    ## label dictionary for legend labels
    label_dict = {'Temp' + key: f"{hive[-1]}: {hex_codes_dict[hive][key]}" for hive in hex_codes_dict for key in hex_codes_dict[hive]}
    label_dict = dict(sorted(label_dict.items(), key=lambda item: ('brood' not in item[1], 'honey' not in item[1], 'room' not in item[1], item[1])))
    # sort for plotting
    label_dict = dict(sorted(label_dict.items(), key=lambda item: ('brood' not in item[1], 'honey' not in item[1], 'room' not in item[1], item[1])))    

    return combined_data, hex_codes_dict, label_dict