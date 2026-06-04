"""Monitor and plot real-time data: feeder weight and hive temperature. Send updates via Telegram."""

import matplotlib.pyplot as plt
import src.mon as mon
import src.monitor_temp as temp
import src.monitor_weight as weight

if __name__ == "__main__":
    config = mon.get_config(
        default_module="default_config_starter",
        user_module="user_config_starter",
    )

    WEIGHT_FIG_PATH = "/home/beesbook/bb_monitoring/figs/weight_plot.png"
    TEMP_FIG_PATH = "/home/beesbook/bb_monitoring/figs/temp_plot.png"

    plt.ion()
    fig, axes = plt.subplots(4, 1, figsize=(7.2, 14.4))

    print("Starting monitor_data loop. Press Ctrl+C to exit.")
    while True:
        try:
            temp.draw_plot(fig, axes, TEMP_FIG_PATH)
            weight.draw_plot(fig, axes, WEIGHT_FIG_PATH)
            
            mon.send_photo(config, TEMP_FIG_PATH, caption="Hive Temperature")
            mon.send_photo(config, WEIGHT_FIG_PATH, caption="Feeder Consumption")
            
            plt.pause(temp.UPDATE_INTERVAL_SECONDS)
        except Exception as e:
            print(f"Error in data monitor loop: {e}")
            mon.send_message(config, f"Error in data monitor loop: {e}")
            plt.pause(60)  # wait a minute before retrying
