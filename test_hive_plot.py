#!/home/beesbook/miniconda3/envs/beesbook/bin/python3
# quick sanity check: run hive monitoring on 1 day of data and save the plot
import src.monitor_hive as hive
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# override to 1 day and a separate cache so we dont touch the real one
hive.WINDOW_DAYS = 1
hive.CACHE_DIR   = "/home/beesbook/bb_monitor/cache/test"

SAVE_PATH = "/home/beesbook/bb_monitor/figs/test_hive_plot.png"

fig, axes = plt.subplots(4, 1, figsize=(7.2, 14.4))
hive.draw_plot(fig, axes, save_path=SAVE_PATH)
print(f"Saved to {SAVE_PATH}")
