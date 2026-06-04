import glob
import time
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from datetime import timedelta

BASE_DIR = "/mnt/trove/beesbook2026/feeder_scales"
SCALES = [
    {"dir": "feedercama", "label": "Scale A", "letter": "A"},
    {"dir": "feedercamb", "label": "Scale B", "letter": "B"},
    {"dir": "feedercamc", "label": "Scale C", "letter": "C"},
    {"dir": "feedercamd", "label": "Scale D", "letter": "D"},
]
WINDOW_DAYS = 7
UPDATE_INTERVAL_SECONDS = 3600
TREATMENT_DAYS = {1, 2}        # tue=1, wed=2 (python weeks start monday)
RESAMPLE_MIN = 5               # group readings into 5 min bins
EMA_SPAN = max(1, 10 // RESAMPLE_MIN)        # smooth over ~10 min
PEAK_ROLL_WIN = max(1, 30 // RESAMPLE_MIN)   # window size for catching spikes
PEAK_THRESHOLD_G = 200         # if a bin is >200g off from neighbors, skip it
DIFF_MAX_G = 20                # anything higher is probably a refill or the pi rebooted
DIFF_MIN_G = -100              # sudden drop bigger than this = feeder got bumped or fell


def load_scale_data(scale):
    pattern = f"{BASE_DIR}/{scale['dir']}/weight_data_scale{scale['letter']}_*.csv"
    files = sorted(glob.glob(pattern))
    if not files:
        return None

    latest_df = pd.read_csv(files[-1])
    latest_df["Time"] = pd.to_datetime(latest_df["Time"], errors="coerce")
    latest_df = latest_df.dropna(subset=["Time"])
    if latest_df.empty:
        return None
    end_time = latest_df["Time"].max()
    start_time = end_time - timedelta(days=WINDOW_DAYS)

    start_date_str = start_time.strftime("%Y-%m-%d")
    prefix = f"{BASE_DIR}/{scale['dir']}/weight_data_scale{scale['letter']}_"
    relevant_files = [f for f in files if f[len(prefix):len(prefix)+10] >= start_date_str]

    chunks = []
    for f in relevant_files:
        try:
            df = pd.read_csv(f)
            df["Time"] = pd.to_datetime(df["Time"], errors="coerce")
            df = df.dropna(subset=["Time"])
            chunks.append(df)
        except Exception:
            pass
    if not chunks:
        return None

    combined = pd.concat(chunks, ignore_index=True)
    combined = combined[(combined["Time"] >= start_time) & (combined["Time"] <= end_time)]
    combined = combined.sort_values("Time")

    # median per bin gets rid of most of the noise without much effort
    resampled = (
        combined.set_index("Time")["Weight_g"]
        .resample(f"{RESAMPLE_MIN}min").median()
        .dropna()
        .reset_index()
    )
    resampled.columns = ["Time", "Weight_g"]

    # second pass to catch anything still weird after the median
    rolling_med = resampled["Weight_g"].rolling(window=PEAK_ROLL_WIN, center=True, min_periods=1).median()
    resampled.loc[abs(resampled["Weight_g"] - rolling_med) > PEAK_THRESHOLD_G, "Weight_g"] = float("nan")
    resampled["Weight_g"] = resampled["Weight_g"].interpolate(method="linear")

    diffs = resampled["Weight_g"].diff()
    refill_mask = diffs > DIFF_MAX_G
    disturb_mask = diffs < DIFF_MIN_G
    resampled["Event"] = refill_mask | disturb_mask

    diffs[refill_mask] = float("nan")
    diffs[disturb_mask] = float("nan")

    # restart from 0 at each refill so the graph shows consumption since last fill
    segment_id = refill_mask.cumsum()
    resampled["Consumption_g"] = (-diffs.groupby(segment_id).cumsum()).clip(lower=0)

    resampled["EMA_g"] = resampled["Consumption_g"].ewm(span=EMA_SPAN, adjust=False).mean()

    return resampled, start_time, end_time


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
    fig.suptitle("Feeder Consumption — Last 7 Days", fontsize=18, fontweight="bold")

    treatment_patch = mpatches.Patch(color="#a8d5b5", alpha=0.35, label="Treatment (Tue–Wed)")
    ema_line = plt.Line2D([0], [0], color="steelblue", linewidth=2.0,
                          label=f"EMA ({EMA_SPAN * RESAMPLE_MIN} min)")
    raw_line = plt.Line2D([0], [0], color="steelblue", linewidth=0.5, alpha=0.35,
                          label=f"Raw ({RESAMPLE_MIN} min)")
    refill_line = plt.Line2D([0], [0], color="darkorange", linewidth=1.2,
                             linestyle="--", label="Refill / Feeder disturbed")
    legend_added = False

    for ax, scale in zip(axes, SCALES):
        result = load_scale_data(scale)
        ax.set_title(scale["label"], fontsize=15, fontweight="bold")
        ax.set_ylabel("Consumption (g)", fontsize=13)
        ax.grid(True, alpha=0.3, zorder=1)

        if result is None:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                    ha="center", va="center", color="gray", fontsize=13)
            continue

        data, start_time, end_time = result
        shade_treatment_days(ax, start_time, end_time)
        ax.plot(data["Time"], data["Consumption_g"],
                linewidth=0.5, color="steelblue", alpha=0.35, zorder=2)
        ax.plot(data["Time"], data["EMA_g"],
                linewidth=2.0, color="steelblue", zorder=3)

        for t in data.loc[data["Event"], "Time"]:
            ax.axvline(t, color="darkorange", linewidth=1.2, linestyle="--", zorder=4)

        ax.set_xlim(start_time, end_time)

        ax.xaxis.set_major_locator(mdates.DayLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %m-%d"))
        ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
        ax.tick_params(axis="x", which="major", rotation=30, labelsize=12)
        ax.tick_params(axis="x", which="minor", length=3)
        ax.tick_params(axis="y", labelsize=12)

        if not legend_added:
            ax.legend(handles=[treatment_patch, ema_line, raw_line, refill_line],
                      loc="upper left", fontsize=11, framealpha=0.7)
            legend_added = True

    fig.tight_layout()
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    fig.canvas.draw()
    fig.canvas.flush_events()
    print(f"Last updated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Next update in {UPDATE_INTERVAL_SECONDS // 60} minutes")


if __name__ == "__main__":
    plt.ion()
    # sized for a phone screen in portrait
    fig, axes = plt.subplots(4, 1, figsize=(7.2, 14.4))

    while True:
        draw_plot(fig, axes)
        plt.pause(UPDATE_INTERVAL_SECONDS)
