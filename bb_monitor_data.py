"""Monitor and plot real-time data: hive temperature, feeder weight, feeder visits and exit visits. Send updates via Telegram."""

import matplotlib.pyplot as plt
import src.mon as mon
import src.monitor_temp as temp
import src.monitor_weight as weight
import src.monitor_feeder_visits as feeder_visits
import src.monitor_exit_visits as exit_visits
import src.monitor_hive as hive
import pandas as pd

def main():
    config = mon.get_config(
        default_module="default_config_starter",
        user_module="user_config_starter",
    )

    TEMP_FIG_PATH         = "/home/beesbook/bb_monitor/figs/temp_plot.png"
    WEIGHT_FIG_PATH       = "/home/beesbook/bb_monitor/figs/weight_plot.png"
    FEEDER_VISITS_FIG_PATH = "/home/beesbook/bb_monitor/figs/feeder_visits_plot.png"
    EXIT_VISITS_FIG_PATH  = "/home/beesbook/bb_monitor/figs/exit_visits_plot.png"
    HIVE_FIG_PATH  = "/home/beesbook/bb_monitor/figs/hive_plot.png"
    UPDATE_INTERVAL_SECONDS = 3600

    plt.ion()
    fig_t, axes_t = plt.subplots(3, 1, figsize=(7.2, 12.0))
    fig_w, axes_w = plt.subplots(4, 1, figsize=(7.2, 14.4))
    fig_fv, axes_fv = plt.subplots(4, 1, figsize=(7.2, 14.4))
    fig_ev, axes_ev = plt.subplots(4, 1, figsize=(7.2, 14.4))
    fig_hi, axes_hi = plt.subplots(4, 1, figsize=(7.2, 14.4))

    print("Starting monitor_data loop. Press Ctrl+C to exit.")
    while True:
        try:
            temp.draw_plot(fig_t, axes_t, TEMP_FIG_PATH)
            weight.draw_plot(fig_w, axes_w, WEIGHT_FIG_PATH)
            feeder_visits.draw_plot(fig_fv, axes_fv, FEEDER_VISITS_FIG_PATH)
            exit_visits.draw_plot(fig_ev, axes_ev, EXIT_VISITS_FIG_PATH)
            hive.draw_plot(fig_hi, axes_hi, HIVE_FIG_PATH)

            mon.send_photo(config, TEMP_FIG_PATH,          caption="Hive Temperature")
            mon.send_photo(config, WEIGHT_FIG_PATH,        caption="Feeder Consumption")
            mon.send_photo(config, FEEDER_VISITS_FIG_PATH, caption="Feeder Visits")
            mon.send_photo(config, EXIT_VISITS_FIG_PATH,   caption="Exit Visits")
            mon.send_photo(config, HIVE_FIG_PATH,          caption="Hive Activity")

            print(f"Last updated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Next update in {UPDATE_INTERVAL_SECONDS // 60} minutes")
            plt.pause(UPDATE_INTERVAL_SECONDS)
        except Exception as e:
            print(f"Error in data monitor loop: {e}")
            mon.send_message(config, f"Error in data monitor loop: {e}")
            plt.pause(60)  # wait a minute before retrying


if __name__ == "__main__":
    main()