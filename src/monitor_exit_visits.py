#!/home/beesbook/miniconda3/envs/beesbook/bin/python3
import glob
import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from datetime import timedelta

BASE_DIR = "/mnt/trove/beesbook2026/pi"
CAMS = [
    {"dir": "exitcamA", "label": "Exit A"},
    {"dir": "exitcamB", "label": "Exit B"},
    {"dir": "exitcamC", "label": "Exit C"},
    {"dir": "exitcamD", "label": "Exit D"},
]
WINDOW_DAYS          = 7
CACHE_DAYS           = 49
TREATMENT_DAYS       = {1, 2}    # tue=1, wed=2
BIN_MIN              = 10        # 10-min bins
ROLL_WIN             = 6         # rolling mean over 6 bins = 1 hour
CACHE_DIR            = "/home/beesbook/bb_monitor/cache"


def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def cache_file_path(cam):
    return os.path.join(CACHE_DIR, f"{cam['dir']}_visits_cache.pkl")


def load_cache(cam):
    ensure_cache_dir()
    cache_path = cache_file_path(cam)
    if os.path.exists(cache_path):
        try:
            cache = pd.read_pickle(cache_path)
            if isinstance(cache, dict) and "clips" in cache:
                return cache
        except Exception:
            pass
    return {"clips": []}


def save_cache(cam, cache):
    ensure_cache_dir()
    try:
        pd.to_pickle(cache, cache_file_path(cam))
    except Exception:
        pass


def parse_parquet_file(file_path):
    try:
        df = pd.read_parquet(file_path)
    except Exception:
        return []

    if "video_start_timestamp" not in df.columns or df.empty:
        return []

    clip_time = pd.to_datetime(df["video_start_timestamp"].iloc[0])
    max_simultaneous = 0
    tagged_ids = set()

    if "detection_type" in df.columns:
        det = df["detection_type"].dropna()
        u = df[det.isin({"UnmarkedBee", "UpsideDownBee"})]
        if not u.empty and "frameIdx" in u.columns:
            max_simultaneous = int(u.groupby("frameIdx").size().max())
        for _, row in df[det == "TaggedBee"].iterrows():
            if row["beeID"] is not None:
                tagged_ids.add(int(np.array(row["beeID"]).argmax()))

    return [{
        "clip_time": clip_time,
        "max_simultaneous": max_simultaneous,
        "tagged_ids": tagged_ids,
        "file_path": file_path,
        "file_mtime": os.path.getmtime(file_path),
    }]


def load_cam_data(cam):
    parquet_files = sorted(glob.glob(
        f"{BASE_DIR}/*/{cam['dir']}/{cam['dir']}_*-detections-c.parquet"
    ))
    if not parquet_files:
        return None

    end_time = None
    for f in reversed(parquet_files):
        try:
            df_peek = pd.read_parquet(f)
            if "video_start_timestamp" in df_peek.columns and not df_peek.empty:
                end_time = pd.to_datetime(df_peek["video_start_timestamp"].max())
                break
        except Exception:
            continue
    if end_time is None:
        return None

    history_start_time = end_time - timedelta(days=CACHE_DAYS)
    cache = load_cache(cam)
    cached_clips = {clip["file_path"]: clip for clip in cache.get("clips", [])}

    candidate_files = []
    for f in parquet_files:
        base = os.path.basename(f)
        date_str = base[len(cam["dir"]) + 1: len(cam["dir"]) + 11]
        if date_str >= history_start_time.strftime("%Y-%m-%d"):
            candidate_files.append(f)

    updated_clips = []
    files_to_process = []
    for f in candidate_files:
        try:
            mtime = os.path.getmtime(f)
        except OSError:
            continue
        cached = cached_clips.get(f)
        if cached is None or cached.get("file_mtime") != mtime:
            files_to_process.append(f)
        else:
            updated_clips.append(cached)

    for f in files_to_process:
        updated_clips.extend(parse_parquet_file(f))

    updated_clips = [clip for clip in updated_clips
                     if clip["clip_time"] >= history_start_time]
    updated_clips = sorted(updated_clips, key=lambda c: c["clip_time"])

    if files_to_process or len(updated_clips) != len(cache.get("clips", [])):
        save_cache(cam, {"clips": updated_clips})

    plot_start = end_time - timedelta(days=WINDOW_DAYS)
    clips = [clip for clip in updated_clips
             if plot_start <= clip["clip_time"] <= end_time]
    if not clips:
        return None

    freq = f"{BIN_MIN}min"
    u_series = pd.Series({c["clip_time"]: c["max_simultaneous"] for c in clips})
    raw_u = u_series.resample(freq).max().fillna(0).rename("raw")
    smooth_u = raw_u.rolling(window=ROLL_WIN, center=True, min_periods=1).mean().rename("smooth")

    t_records = [{"clip_time": c["clip_time"], "bee_id": b}
                 for c in clips for b in c["tagged_ids"]]
    if not t_records:
        raw_t = smooth_t = pd.Series(dtype=float)
    else:
        t_df = pd.DataFrame(t_records)
        raw_t = (t_df.set_index("clip_time")
                 .groupby(pd.Grouper(freq=freq))["bee_id"]
                 .nunique().fillna(0).rename("raw"))
        smooth_t = raw_t.rolling(window=ROLL_WIN, center=True, min_periods=1).mean().rename("smooth")

    return raw_u, smooth_u, raw_t, smooth_t, plot_start, end_time


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
    axes = fig.subplots(4, 1)
    fig.suptitle("Exit Visits — Last 7 Days", fontsize=18, fontweight="bold")

    treatment_patch = mpatches.Patch(color="#a8d5b5", alpha=0.35, label="Treatment (Tue–Wed)")
    unmarked_line   = plt.Line2D([0], [0], color="steelblue", linewidth=2.0,
                                 label="Unmarked bees (max simultaneous)")
    tagged_line     = plt.Line2D([0], [0], color="coral", linewidth=2.0,
                                 label="Tagged bee visits (right axis)")
    legend_added = False

    for ax, cam in zip(axes, CAMS):
        result = load_cam_data(cam)
        ax.set_title(cam["label"], fontsize=15, fontweight="bold")
        ax.set_ylabel("Unmarked (max simultaneous)", fontsize=10, color="steelblue")
        ax.tick_params(axis="y", labelcolor="steelblue", labelsize=10)
        ax.grid(True, alpha=0.3, zorder=1)

        if result is None:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                    ha="center", va="center", color="gray", fontsize=13)
            continue

        raw_u, smooth_u, raw_t, smooth_t, start_time, end_time = result
        shade_treatment_days(ax, start_time, end_time)

        ax.plot(raw_u.index,    raw_u.values,    lw=0.5, color="steelblue", alpha=0.3, zorder=2)
        ax.plot(smooth_u.index, smooth_u.values, lw=2.0, color="steelblue", zorder=3)
        ax.set_ylim(bottom=0)

        ax2 = ax.twinx()
        ax2.plot(raw_t.index,    raw_t.values,    lw=0.5, color="coral", alpha=0.3, zorder=2)
        ax2.plot(smooth_t.index, smooth_t.values, lw=2.0, color="coral", zorder=3)
        ax2.set_ylabel("Tagged unique IDs / 10 min", fontsize=10, color="coral")
        ax2.tick_params(axis="y", labelcolor="coral", labelsize=10)
        ax2.set_ylim(bottom=0)

        ax.set_xlim(start_time, end_time)
        ax.xaxis.set_major_locator(mdates.DayLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %m-%d"))
        ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
        ax.tick_params(axis="x", which="major", rotation=30, labelsize=12)
        ax.tick_params(axis="x", which="minor", length=3)

        if not legend_added:
            ax.legend(handles=[treatment_patch, unmarked_line, tagged_line],
                      loc="upper left", fontsize=9, framealpha=0.7)
            legend_added = True

    fig.tight_layout()
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    if plt.get_backend().lower() != "agg":
        fig.canvas.draw()
        fig.canvas.flush_events()


if __name__ == "__main__":
    plt.ion()
    fig, axes = plt.subplots(4, 1, figsize=(7.2, 14.4))

    UPDATE_INTERVAL_SECONDS = 3600
    SAVE_PATH = "/home/beesbook/bb_monitor/figs/exit_visits_plot.png"

    while True:
        draw_plot(fig, axes, save_path=SAVE_PATH)
        print(f"Last updated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        print(f"Next update in {UPDATE_INTERVAL_SECONDS // 60} minutes", flush=True)
        plt.pause(UPDATE_INTERVAL_SECONDS)
