import glob
import json
import os
import urllib.request
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from datetime import timedelta

BASE_DIR = "/mnt/trove/beesbook2026/temperature"

# pairs of hives per machine, shown together in one panel
PAIRS = [
    {
        "label": "Hives A & B",
        "hives": [
            {"machine": "cirrus", "label": "A", "col_rear": 1, "col_front": 2},
            {"machine": "cirrus", "label": "B", "col_rear": 3, "col_front": 4},
        ],
    },
    {
        "label": "Hives C & D",
        "hives": [
            {"machine": "thria", "label": "C", "col_rear": 1, "col_front": 2},
            {"machine": "thria", "label": "D", "col_rear": 3, "col_front": 4},
        ],
    },
]

# one color pair per hive in the panel
HIVE_COLORS = [
    {"rear": "steelblue",  "front": "cornflowerblue"},   # first hive in pair
    {"rear": "coral",      "front": "tomato"},            # second hive in pair
]

WINDOW_DAYS          = 7
TREATMENT_DAYS       = {1, 2}        # tue=1, wed=2 (python weeks start monday)
RESAMPLE_MIN         = 5
EMA_SPAN             = max(1, 20 // RESAMPLE_MIN)   # smooth over ~20 min, temp changes slowly
TEMP_MIN             = 0.0    # sensor returns -127 on bad read and 85 on startup, skip those
TEMP_MAX             = 50.0
COLUMN_NAMES         = ["Time", "S1", "S2", "S3", "S4"]
WEATHER_LAT          = 52.4667       # Berlin Dahlem Dorf
WEATHER_LON          = 13.2833


def fetch_weather(start_time, end_time):
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
           f"&past_days={WINDOW_DAYS}&hourly=temperature_2m,precipitation"
           f"&timezone=Europe%2FBerlin")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        times  = pd.to_datetime(data["hourly"]["time"])
        temp   = pd.Series(data["hourly"]["temperature_2m"], index=times, dtype=float)
        precip = pd.Series(data["hourly"]["precipitation"],  index=times, dtype=float)
        temp   = temp[(temp.index   >= start_time) & (temp.index   <= end_time)]
        precip = precip[(precip.index >= start_time) & (precip.index <= end_time)]
        return temp, precip
    except Exception:
        return None, None


def load_hive_data(hive):
    machine = hive["machine"]
    path    = f"{BASE_DIR}/{machine}/{machine}"
    files   = sorted(glob.glob(f"{path}/temperature_data_*.csv"))
    if not files:
        return None

    latest_df = pd.read_csv(files[-1], header=None, names=COLUMN_NAMES)
    latest_df["Time"] = pd.to_datetime(latest_df["Time"], format="ISO8601", errors="coerce")
    latest_df = latest_df.dropna(subset=["Time"])
    if latest_df.empty:
        return None
    end_time   = latest_df["Time"].max()
    start_time = end_time - timedelta(days=WINDOW_DAYS)

    start_date_str  = start_time.strftime("%Y-%m-%d")
    prefix          = f"{path}/temperature_data_"
    relevant_files  = [f for f in files if f[len(prefix):len(prefix) + 10] >= start_date_str]

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
        series   = series.where((series >= TEMP_MIN) & (series <= TEMP_MAX))
        resampled = series.resample(f"{RESAMPLE_MIN}min").median().dropna().reset_index()
        resampled.columns = ["Time", "Temp"]
        resampled["EMA"]  = resampled["Temp"].ewm(span=EMA_SPAN, adjust=False).mean()
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
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    fig.clf()
    axes = fig.subplots(3, 1)
    fig.suptitle("Hive Temperature — Last 7 Days", fontsize=18, fontweight="bold")

    treatment_patch = mpatches.Patch(color="#a8d5b5", alpha=0.35, label="Treatment (Tue–Wed)")

    for ax, pair in zip(axes, PAIRS):
        ax.set_title(pair["label"], fontsize=15, fontweight="bold")
        ax.set_ylabel("Temperature (°C)", fontsize=13)
        ax.grid(True, alpha=0.3, zorder=1)

        legend_handles = [treatment_patch]
        pair_start = pair_end = None

        for i, hive in enumerate(pair["hives"]):
            result = load_hive_data(hive)
            if result is None:
                continue

            rear, front, start_time, end_time = result
            if pair_start is None:
                pair_start, pair_end = start_time, end_time

            c_rear  = HIVE_COLORS[i]["rear"]
            c_front = HIVE_COLORS[i]["front"]

            ax.plot(rear["Time"],  rear["Temp"],  lw=0.5, color=c_rear,  alpha=0.25, zorder=2)
            ax.plot(rear["Time"],  rear["EMA"],   lw=2.0, color=c_rear,  zorder=3)
            ax.plot(front["Time"], front["Temp"], lw=0.5, color=c_front, alpha=0.25, zorder=2)
            ax.plot(front["Time"], front["EMA"],  lw=2.0, color=c_front, zorder=3)

            legend_handles += [
                plt.Line2D([0], [0], color=c_rear,  lw=2, label=f"Hive {hive['label']} rear"),
                plt.Line2D([0], [0], color=c_front, lw=2, label=f"Hive {hive['label']} front"),
            ]

        if pair_start is None:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                    ha="center", va="center", color="gray", fontsize=13)
            continue

        shade_treatment_days(ax, pair_start, pair_end)
        ax.set_xlim(pair_start, pair_end)
        ax.xaxis.set_major_locator(mdates.DayLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %m-%d"))
        ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
        ax.tick_params(axis="x", which="major", rotation=30, labelsize=12)
        ax.tick_params(axis="x", which="minor", length=3)
        ax.tick_params(axis="y", labelsize=12)
        ax.legend(handles=legend_handles, loc="upper left", fontsize=10, framealpha=0.7, ncol=2)

    # weather panel
    ax_w = axes[2]
    ax_w.set_title("Berlin Dahlem — Temperature & Precipitation", fontsize=15, fontweight="bold")
    ax_w.set_ylabel("Temperature (°C)", fontsize=13)
    ax_w.grid(True, alpha=0.3, zorder=1)
    ax_p = ax_w.twinx()
    ax_p.set_ylabel("Precipitation (mm/h)", fontsize=11, color="royalblue")
    ax_p.tick_params(axis="y", labelcolor="royalblue", labelsize=10)

    # reuse the window from the hive data if we got any
    all_starts = []
    all_ends   = []
    for pair in PAIRS:
        for hive in pair["hives"]:
            r = load_hive_data(hive)
            if r:
                _, _, s, e = r
                all_starts.append(s)
                all_ends.append(e)

    if all_starts:
        ws, we = min(all_starts), max(all_ends)
        temp, precip = fetch_weather(ws, we)
        shade_treatment_days(ax_w, ws, we)
        ax_w.set_xlim(ws, we)
        ax_w.xaxis.set_major_locator(mdates.DayLocator())
        ax_w.xaxis.set_major_formatter(mdates.DateFormatter("%a %m-%d"))
        ax_w.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
        ax_w.tick_params(axis="x", which="major", rotation=30, labelsize=12)
        ax_w.tick_params(axis="x", which="minor", length=3)
        ax_w.tick_params(axis="y", labelsize=12)

        if temp is not None:
            ax_w.plot(temp.index, temp.values, lw=2.0, color="darkorange", zorder=3)
            ax_p.bar(precip.index, precip.values, width=1/24, color="royalblue",
                     alpha=0.5, zorder=2, align="edge")
            ax_p.set_ylim(bottom=0)
            ax_w.legend(handles=[
                plt.Line2D([0], [0], color="darkorange", lw=2, label="Temperature (°C)"),
                mpatches.Patch(color="royalblue", alpha=0.5, label="Precipitation (mm/h)"),
                mpatches.Patch(color="#a8d5b5", alpha=0.35, label="Treatment (Tue–Wed)"),
            ], loc="upper left", fontsize=10, framealpha=0.7)
        else:
            ax_w.text(0.5, 0.5, "weather data unavailable", transform=ax_w.transAxes,
                      ha="center", va="center", color="gray", fontsize=12)

    fig.tight_layout()
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    if plt.get_backend().lower() != "agg":
        fig.canvas.draw()
        fig.canvas.flush_events()


if __name__ == "__main__":
    plt.ion()
    # 3 panels, sized for a phone screen in portrait
    fig, axes = plt.subplots(3, 1, figsize=(7.2, 12.0))

    UPDATE_INTERVAL_SECONDS = 3600
    SAVE_PATH = "/home/beesbook/bb_monitor/figs/temp_plot.png"

    while True:
        draw_plot(fig, axes, save_path=SAVE_PATH)
        print(f"Last updated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Next update in {UPDATE_INTERVAL_SECONDS // 60} minutes")
        plt.pause(UPDATE_INTERVAL_SECONDS)
