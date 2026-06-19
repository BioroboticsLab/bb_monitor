#!/home/beesbook/miniconda3/envs/beesbook/bin/python3
"""
Test: daily consumption bar chart for feeder scales.
Uses overnight weight reference (01:00-04:00) as the stable anchor per day
since bees are inactive and temperature is stable during that window.
daily_consumption = overnight_ref[yesterday] - overnight_ref[today]
"""

import glob
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from datetime import timedelta

BASE_DIR          = "/mnt/trove/beesbook2026/feeder_scales"
SAVE_PATH         = "/home/beesbook/bb_monitor/figs/daily_consumption_test.png"
WINDOW_DAYS       = 7
RESAMPLE_MIN      = 5
PEAK_ROLL_WIN     = max(1, 30 // RESAMPLE_MIN)
PEAK_THRESHOLD_G  = 200
DIFF_MAX_G        = 20
DIFF_MIN_G        = -30
FILL_G            = 246    # 123g sugar + 123g water
TREATMENT_DAYS    = {1, 2} # tue=1, wed=2

SCALES = [
    {"dir": "feedercama", "label": "Scale A", "letter": "A"},
    {"dir": "feedercamb", "label": "Scale B", "letter": "B"},
    {"dir": "feedercamc", "label": "Scale C", "letter": "C"},
    {"dir": "feedercamd", "label": "Scale D", "letter": "D"},
]


def load_clean(scale):
    files = sorted(glob.glob(f"{BASE_DIR}/{scale['dir']}/weight_data_scale{scale['letter']}_*.csv"))
    if not files:
        return None

    latest = pd.read_csv(files[-1])
    latest["Time"] = pd.to_datetime(latest["Time"], errors="coerce")
    latest = latest.dropna(subset=["Time"])
    if latest.empty:
        return None
    end_time   = latest["Time"].max()
    start_time = end_time - timedelta(days=WINDOW_DAYS)

    chunks = []
    for f in files:
        try:
            df = pd.read_csv(f)
            df["Time"] = pd.to_datetime(df["Time"], errors="coerce")
            df = df.dropna(subset=["Time"])
            if df["Time"].max() >= start_time:
                chunks.append(df)
        except Exception:
            pass
    if not chunks:
        return None

    combined = pd.concat(chunks).query("Time >= @start_time and Time <= @end_time").sort_values("Time")

    resampled = (
        combined.set_index("Time")["Weight_g"]
        .resample(f"{RESAMPLE_MIN}min").median()
        .dropna()
        .reset_index()
    )
    resampled.columns = ["Time", "Weight_g"]

    # spike removal
    rolling_med = resampled["Weight_g"].rolling(window=PEAK_ROLL_WIN, center=True, min_periods=1).median()
    spike_mask  = abs(resampled["Weight_g"] - rolling_med) > PEAK_THRESHOLD_G
    resampled.loc[spike_mask, "Weight_g"] = float("nan")
    resampled["Weight_g"] = resampled["Weight_g"].interpolate(method="linear")

    # detect refill/disturbance events
    diffs        = resampled["Weight_g"].diff()
    refill_mask  = diffs > DIFF_MAX_G
    disturb_mask = diffs < DIFF_MIN_G
    resampled["event"] = refill_mask | disturb_mask | spike_mask

    return resampled, start_time, end_time


def daily_overnight_ref(resampled):
    # median weight between 01:00 and 04:00 each day — most stable period
    df = resampled.copy()
    df["hour"] = df["Time"].dt.hour
    df["date"] = df["Time"].dt.date

    overnight = df[df["hour"].between(1, 3)].groupby("date")["Weight_g"].median()
    return overnight


def compute_daily_consumption(resampled, start_time, end_time):
    overnight = daily_overnight_ref(resampled)
    if overnight.empty or len(overnight) < 2:
        return pd.Series(dtype=float), pd.Series(dtype=bool)

    # daily consumption = drop in overnight reference from one day to the next
    # negative means a refill happened that day — mark it and show 0
    raw_diff    = overnight.diff()           # today - yesterday (will be negative for consumption)
    consumption = (-raw_diff).clip(lower=0, upper=FILL_G)
    had_refill  = raw_diff > 20             # overnight ref went UP = refill between readings

    return consumption, had_refill


fig, axes = plt.subplots(4, 1, figsize=(10, 14))
fig.suptitle("Daily Feeder Consumption — Last 7 Days", fontsize=14, fontweight="bold")

treatment_patch = mpatches.Patch(color="#a8d5b5", alpha=0.8,  label="Treatment day (Tue–Wed)")
refill_patch    = mpatches.Patch(color="darkorange", alpha=0.6, label="Refill detected")
legend_added    = False

for ax, scale in zip(axes, SCALES):
    ax.set_title(scale["label"], fontsize=12, fontweight="bold")
    ax.set_ylabel("Consumption (g)", fontsize=10)
    ax.grid(True, alpha=0.3, axis="y")
    ax.axhline(FILL_G, color="gray", lw=1, linestyle="--", alpha=0.5, label=f"Max fill ({FILL_G}g)")

    result = load_clean(scale)
    if result is None:
        ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                ha="center", va="center", color="gray", fontsize=13)
        continue

    resampled, start_time, end_time = result
    consumption, had_refill = compute_daily_consumption(resampled, start_time, end_time)

    if consumption.empty:
        ax.text(0.5, 0.5, "insufficient data", transform=ax.transAxes,
                ha="center", va="center", color="gray", fontsize=13)
        continue

    dates  = pd.to_datetime(consumption.index)
    values = consumption.values

    bar_colors = []
    for d in dates:
        if d.dayofweek in TREATMENT_DAYS:
            bar_colors.append("#a8d5b5")
        else:
            bar_colors.append("steelblue")

    bars = ax.bar(dates, values, color=bar_colors, width=0.7, zorder=3, alpha=0.85)

    # mark days where overnight ref went up (refill between readings)
    for d, is_refill in zip(dates, had_refill.values):
        if is_refill:
            ax.bar(d, FILL_G * 0.05, bottom=0, color="darkorange",
                   width=0.7, alpha=0.6, zorder=4)
            ax.text(d, FILL_G * 0.06, "refill", ha="center", va="bottom",
                    fontsize=7, color="darkorange")

    # label each bar with the gram value
    for bar, val in zip(bars, values):
        if val > 2:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                    f"{val:.0f}g", ha="center", va="bottom", fontsize=8, fontweight="bold")

    ax.set_xlim(pd.Timestamp(start_time.date()) - timedelta(hours=12),
                pd.Timestamp(end_time.date())   + timedelta(hours=12))
    ax.set_ylim(0, FILL_G * 1.1)
    ax.xaxis.set_major_locator(plt.matplotlib.dates.DayLocator())
    ax.xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter("%a %b-%d"))
    ax.tick_params(axis="x", rotation=30, labelsize=9)
    ax.tick_params(axis="y", labelsize=9)

    if not legend_added:
        ax.legend(handles=[treatment_patch, refill_patch,
                           plt.Line2D([0],[0], color="gray", lw=1, ls="--", alpha=0.5, label=f"Max fill ({FILL_G}g)")],
                  loc="upper right", fontsize=9, framealpha=0.8)
        legend_added = True

fig.tight_layout()
fig.savefig(SAVE_PATH, dpi=150, bbox_inches="tight")
print(f"Saved to {SAVE_PATH}")
