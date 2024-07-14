import matplotlib.pyplot as plt
import seaborn as sns
import numpy as np
import pandas as pd
import matplotlib.dates as mdates

def common_plot_formatting(ax, df_results, window_size_hours):
    for a in ax:
        a.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
        a.tick_params(axis='both', which='major', labelsize=12, rotation=90)
        for date in pd.date_range(df_results['timestamp_of_segment'].min().normalize() + pd.Timedelta(days=1), df_results['timestamp_of_segment'].max().normalize()):
            a.axvline(date, color='gray', linestyle='--', linewidth=0.7)
            if a == ax[0]:
                a.annotate(date.strftime('%m-%d'), xy=(date, 1), xycoords=('data', 'axes fraction'),
                           xytext=(5, 5), textcoords='offset points', rotation=90, va='bottom', ha='center', fontsize=12, color='k')
    a.set_xlim(left=df_results['timestamp_of_segment'].min() + pd.Timedelta(days=window_size_hours/24))
    a.set_xlim(right=df_results['timestamp_of_segment'].max() + pd.Timedelta(hours=1))

## simple version
# def plotboth(a,ycol):
#     tp = {'x': 'timestamp_of_segment', 'y': ycol}
#     sns.lineplot(**({'data': df_results}|tp|lpdict),ax=a)
#     sns.scatterplot(**({'data': last_points}|tp|spdict),ax=a)    

## break segments where there is not data
def plot_detections(a, df_results, ycol, segtime_minutes=60):
    last_points = df_results.groupby('hive_id').last().reset_index()
    lpdict = {'hue': 'hive_id', 'style': 'hive_id', 'dashes': False}
    spdict = {'hue': 'hive_id', 'marker': 'o', 's': 50, 'legend': False}

    tp = {'x': 'timestamp_of_segment', 'y': ycol}
    
    df_temp = df_results.copy()
    nanfillsback = []
    nanfillsfwd = []
    for hive in ['A','B']:
        dfsel = df_temp[df_temp['hive_id']==hive].copy()
        dfsel.loc[dfsel.index,'time_diff'] = dfsel['timestamp_of_segment'].diff().dt.total_seconds().div(60).fillna(0)
        dfsel['time_diff_forward'] = dfsel['timestamp_of_segment'].shift(-1).sub(dfsel['timestamp_of_segment']).dt.total_seconds().div(60).fillna(0)        
        nanfillsback.append(dfsel[dfsel['time_diff'] > segtime_minutes].index)
        nanfillsfwd.append(dfsel[dfsel['time_diff_forward'] > segtime_minutes].index)

        # df_temp.loc[dfsel[dfsel['time_diff'] > segtime_minutes].index, ycol] = np.nan

    fillsback = df_temp.loc[np.concatenate(nanfillsback)].copy()
    fillsback['timestamp_of_segment'] = fillsback['timestamp_of_segment'] - pd.Timedelta(minutes=1)
    fillsback[ycol] = np.nan
    fillsfwd = df_temp.loc[np.concatenate(nanfillsfwd)].copy()
    fillsfwd['timestamp_of_segment'] = fillsfwd['timestamp_of_segment'] + pd.Timedelta(minutes=1)
    fillsfwd[ycol] = np.nan    

    # concat and sort
    df_temp = pd.concat((df_temp,fillsback,fillsfwd))
    df_temp = df_temp.sort_values(by='timestamp_of_segment').reset_index(drop=True)
    
    for hive_id in df_temp['hive_id'].unique():
        hive_data = df_temp[df_temp['hive_id'] == hive_id]
        a.plot(hive_data['timestamp_of_segment'], hive_data[ycol], label=f'{hive_id}')
        hive_data = last_points[last_points['hive_id'] == hive_id]
        a.scatter(hive_data['timestamp_of_segment'], hive_data[ycol], s=50, marker='o')

def plot_feedercam_counts(a, dfcounts,feedercamhives, minutes_to_avg=15):
    # Group by 'camID' and resample by 'video_start_timestamp' with the specified interval
    resampled_df = dfcounts.groupby('camID').resample(f'{minutes_to_avg}min', on='video_start_timestamp').mean().reset_index()
    for camID, group in resampled_df.groupby('camID'):
        a.plot(group['video_start_timestamp'], group['totalcounts'], marker='.', linestyle='-', label=feedercamhives[camID])    
    a.set_ylabel('Avg counts at feeder', fontsize=14)
    a.legend(title='Hive',fontsize=12,loc=2,title_fontsize=12)         

def plot_temp_and_precip(a,hourdata):   
    a.plot(hourdata.index, hourdata['temp'], marker='.',color='grey')
    a.set_ylabel('Outside\nTemperature (°C)',fontsize=14)
    # Create a secondary y-axis for the precipitation
    ax2 = a.twinx()
    ax2.bar(hourdata.index, hourdata['prcp'], width=0.05, color='tab:blue', alpha=0.6, label='Precipitation (mm)')
    ax2.set_ylabel('Precipitation (mm)', color='tab:blue', fontsize=14)
    ax2.tick_params(axis='y', labelcolor='tab:blue')    

def plot_temperature_sensors(a,combined_data,label_dict):
    snscolors = sns.color_palette()
    # Plot each temperature sensor data with appropriate colors and line styles
    for hex_code, label in label_dict.items():
        hive = label[0]
        location = label.split()[0]
        if hive == 'A':
            color = snscolors[0]
        elif hive == 'B':
            color = snscolors[1]
            
        if 'honey' in label:
            linestyle = '-'
        elif 'brood' in label:
            linestyle = '--'
        elif 'room' in label:
            linestyle = ':'
    
        a.plot(combined_data['Time'], combined_data[hex_code], label=label, color=color, linestyle=linestyle)
    a.legend(fontsize=10, loc='lower left', ncol=3, borderpad=0.2, columnspacing=1, labelspacing=0.1,handlelength=1.5)
    a.set_ylabel('Hive/Inside\nTemperature (°C)', fontsize=14)    