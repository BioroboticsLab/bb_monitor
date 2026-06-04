import glob
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from datetime import timedelta

BASE_DIR = "/mnt/trove/beesbook2026/temperature"
HIVES = [
    {"machine": "cirrus", "label": "Hive A", "col_rear": 1, "col_front": 2},
    {"machine": "cirrus", "label": "Hive B", "col_rear": 3, "col_front": 4},
    {"machine": "thria",  "label": "Hive C", "col_rear": 1, "col_front": 2},
    {"machine": "thria",  "label": "Hive D", "col_rear": 3, "col_front": 4},
]
WINDOW_DAYS = 7
TREATMENT_DAYS = {1, 2}        # tue=1, wed=2 (python weeks start monday)
RESAMPLE_MIN = 5
EMA_SPAN = max(1, 20 // RESAMPLE_MIN)   # smooth over ~20 min, temp changes slowly
TEMP_MIN = 0.0    # sensor returns -127 on bad read and 85 on startup, skip those
TEMP_MAX = 50.0
COLUMN_NAMES = ["Time", "S1", "S2", "S3", "S4"]


def load_hive_data(hive):
    machine = hive["machine"]
    path = f"{BASE_DIR}/{machine}/{machine}"
    files = sorted(glob.glob(f"{path}/temperature_data_*.csv"))
    if not files:
        return None

    latest_df = pd.read_csv(files[-1], header=None, names=COLUMN_NAMES)
    latest_df["Time"] = pd.to_datetime(latest_df["Time"], format="ISO8601", errors="coerce")
    latest_df = latest_df.dropna(subset=["Time"])
    if latest_df.empty:
        return None
    end_time = latest_df["Time"].max()
    start_time = end_time - timedelta(days=WINDOW_DAYS)

    start_date_str = start_time.strftime("%Y-%m-%d")
    prefix = f"{path}/temperature_data_"
    relevant_files = [f for f in files if f[len(prefix):len(prefix) + 10] >= start_date_str]

    chunks = []
    for f in relevant_files:
        try:
            df = pd.read_csv(f, header=None, names=COLUMN_NAMES)
            df["Time"] = pd.to_datetime(df["Time"], format="ISO8601", errors="coerce")
            for col in ["S1", "S2", "S3", "S4"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["Time"])
            chunks.append(df)
        except Exception:
            pass
    if not chunks:
        return None

    combined = pd.concat(chunks, ignore_index=True)
    combined = combined[(combined["Time"] >= start_time) & (combined["Time"] <= end_time)]
    combined = combined.sort_values("Time").set_index("Time")

    def process_sensor(series):
        series = series.where((series >= TEMP_MIN) & (series <= TEMP_MAX))
        resampled = series.resample(f"{RESAMPLE_MIN}min").median().dropna().reset_index()
        resampled.columns = ["Time", "Temp"]
        resampled["EMA"] = resampled["Temp"].ewm(span=EMA_SPAN, adjust=False).mean()
        return resampled

    rear  = process_sensor(combined[f"S{hive['col_rear']}"])
    front = process_sensor(combined[f"S{hive['col_front']}"])

    return rear, front, start_time, end_time


def shade_treatment_days(ax, start_time, end_time):
    current = start_time.normalize()
    while current <= end_time:
        if current.dayofweek in TREATMENT_DAYS:
            ax.axvspan(current, current + timedelta(days=1),
                       color="#a8d5b5", alpha=0.35, zorder=0)
        current += timedelta(days=1)


def draw_plot(fig, axes, save_path):
    fig.clf()
    axes = fig.subplots(4, 1)
    fig.suptitle("Hive Temperature — Last 7 Days", fontsize=18, fontweight="bold")

    treatment_patch = mpatches.Patch(color="#a8d5b5", alpha=0.35, label="Treatment (Tue–Wed)")
    rear_line  = plt.Line2D([0], [0], color="steelblue", linewidth=2.0, label="Rear (near window)")
    front_line = plt.Line2D([0], [0], color="coral",     linewidth=2.0, label="Front")
    legend_added = False

    for ax, hive in zip(axes, HIVES):
        result = load_hive_data(hive)
        ax.set_title(hive["label"], fontsize=15, fontweight="bold")
        ax.set_ylabel("Temperature (°C)", fontsize=13)
        ax.grid(True, alpha=0.3, zorder=1)

        if result is None:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                    ha="center", va="center", color="gray", fontsize=13)
            continue

        rear, front, start_time, end_time = result
        shade_treatment_days(ax, start_time, end_time)

        ax.plot(rear["Time"],  rear["Temp"],  linewidth=0.5, color="steelblue", alpha=0.35, zorder=2)
        ax.plot(rear["Time"],  rear["EMA"],   linewidth=2.0, color="steelblue", zorder=3)
        ax.plot(front["Time"], front["Temp"], linewidth=0.5, color="coral",     alpha=0.35, zorder=2)
        ax.plot(front["Time"], front["EMA"],  linewidth=2.0, color="coral",     zorder=3)

        ax.set_xlim(start_time, end_time)
        ax.xaxis.set_major_locator(mdates.DayLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %m-%d"))
        ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
        ax.tick_params(axis="x", which="major", rotation=30, labelsize=12)
        ax.tick_params(axis="x", which="minor", length=3)
        ax.tick_params(axis="y", labelsize=12)

        if not legend_added:
            ax.legend(handles=[treatment_patch, rear_line, front_line],
                      loc="upper left", fontsize=11, framealpha=0.7)
            legend_added = True

    fig.tight_layout()
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    fig.canvas.draw()
    fig.canvas.flush_events()


if __name__ == "__main__":
    plt.ion()
    fig, axes = plt.subplots(4, 1, figsize=(7.2, 14.4))

    SAVE_PATH = "/home/beesbook/bb_monitoring/figs/temp_plot.png"
    UPDATE_INTERVAL_SECONDS = 3600

    while True:
        draw_plot(fig, axes, save_path=SAVE_PATH)
        print(f"Last updated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Next update in {UPDATE_INTERVAL_SECONDS // 60} minutes")
        plt.pause(UPDATE_INTERVAL_SECONDS)
