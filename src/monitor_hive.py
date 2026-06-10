#!/home/beesbook/miniconda3/envs/beesbook/bin/python3
import glob
import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from datetime import timedelta
from bb_binary import parse_video_fname
from bb_behavior.tracking import detect_markers_in_video

BASE_DIR             = "/mnt/trove/beesbook2026/single_video_frames"
WINDOW_DAYS          = 7
TREATMENT_DAYS       = {1, 2}    # tue=1, wed=2
BIN_MIN              = 15        # 15-min bins, one image per bin
ROLL_WIN             = 24        # rolling mean over 24 bins = 6 hours
CACHE_DIR            = "/home/beesbook/bb_monitor/cache"

HIVES = [
    {"label": "Hive A", "cam_rear": "cam-0", "cam_front": "cam-1"},
    {"label": "Hive B", "cam_rear": "cam-2", "cam_front": "cam-3"},
    {"label": "Hive C", "cam_rear": "cam-4", "cam_front": "cam-5"},
    {"label": "Hive D", "cam_rear": "cam-6", "cam_front": "cam-7"},
]

COLORS = {
    "untagged_rear":  "steelblue",
    "untagged_front": "cornflowerblue",
    "tagged_rear":    "coral",
    "tagged_front":   "tomato",
    "cumul_rear":     "firebrick",
    "cumul_front":    "darkorange",
}


def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def cache_file_path(cam):
    return os.path.join(CACHE_DIR, f"{cam}_hive_cache.pkl")


def load_cache(cam):
    ensure_cache_dir()
    cache_path = cache_file_path(cam)
    if os.path.exists(cache_path):
        try:
            cache = pd.read_pickle(cache_path)
            if isinstance(cache, dict) and "images" in cache:
                return cache
        except Exception:
            pass
    return {"images": []}


def save_cache(cam, cache):
    ensure_cache_dir()
    try:
        pd.to_pickle(cache, cache_file_path(cam))
    except Exception:
        pass


def detect_bees(image_path):
    tag_pixel_diameter = 38.0
    try:
        cam_id, _, _ = parse_video_fname(image_path)
        _, video_dataframe = detect_markers_in_video(
            image_path,
            tag_pixel_diameter=tag_pixel_diameter,
            cam_id=cam_id,
            confidence_filter=0.001,
            use_parallel_jobs=False,
            progress=None,
        )
    except Exception:
        return None

    df = video_dataframe
    df = df[df["localizerSaliency"] >= 0.5]

    tagged_ids     = set()
    untagged_count = len(df[df["detection_type"] != "TaggedBee"])

    for _, row in df[df["detection_type"] == "TaggedBee"].iterrows():
        if row["beeID"] is not None:
            tagged_ids.add(int(np.array(row["beeID"]).argmax()))

    return tagged_ids, untagged_count


def load_cam_data(cam, end_time, start_time):
    cam_dir = os.path.join(BASE_DIR, cam)
    png_files = sorted(glob.glob(os.path.join(cam_dir, f"{cam}_*.png")))
    if not png_files:
        return None

    cache = load_cache(cam)
    cached_images = {img["file_path"]: img for img in cache.get("images", [])}

    candidate_files = []
    for f in png_files:
        base = os.path.basename(f)
        parts = base.split("_")
        if len(parts) >= 2:
            timestamp_str = parts[1].split(".")[0]
            try:
                clip_time = pd.to_datetime(timestamp_str, format="%Y%m%dT%H%M%S")
                if start_time <= clip_time <= end_time:
                    candidate_files.append({"clip_time": clip_time, "file_path": f})
            except Exception:
                continue

    if not candidate_files:
        return None

    updated_images = []
    files_to_process = []
    for item in candidate_files:
        f = item["file_path"]
        try:
            mtime = os.path.getmtime(f)
        except OSError:
            continue
        cached = cached_images.get(f)
        # reprocess if new, mtime changed, or old cache entry missing fields
        if cached is None or cached.get("file_mtime") != mtime or "tagged_ids" not in cached or "untagged_count" not in cached:
            files_to_process.append(item)
        else:
            updated_images.append(cached)

    for item in files_to_process:
        f = item["file_path"]
        clip_time = item["clip_time"]
        res = detect_bees(f)
        if res is not None:
            tagged_ids, untagged_count = res
            updated_images.append({
                "clip_time":      clip_time,
                "file_path":      f,
                "file_mtime":     os.path.getmtime(f),
                "tagged_ids":     tagged_ids,
                "untagged_count": untagged_count,
            })
            print(f"Processed {f}: {len(tagged_ids)} tagged IDs, {untagged_count} untagged")
        else:
            print(f"Failed to process {f}")

    updated_images = sorted(updated_images, key=lambda c: c["clip_time"])

    if files_to_process or len(updated_images) != len(cache.get("images", [])):
        save_cache(cam, {"images": updated_images})

    if not updated_images:
        return None

    freq = f"{BIN_MIN}min"

    # untagged: mean count per bin, smoothed
    u_series = pd.Series({img["clip_time"]: img["untagged_count"] for img in updated_images})
    raw_u    = u_series.resample(freq).mean().fillna(0)
    smooth_u = raw_u.rolling(window=ROLL_WIN, center=True, min_periods=1).mean()

    # tagged: union all IDs seen within each bin, then smooth the count
    id_series = pd.Series({img["clip_time"]: img["tagged_ids"] for img in updated_images})
    bins      = id_series.resample(freq).apply(
        lambda x: set().union(*x) if len(x) > 0 else set()
    )
    raw_t    = bins.apply(len).rename("raw")
    smooth_t = raw_t.rolling(window=ROLL_WIN, center=True, min_periods=1).mean()

    # cumulative unique tagged IDs seen since midnight, resets each day
    seen_today  = set()
    current_day = None
    cumulative  = pd.Series(index=bins.index, dtype=float)
    for ts, ids in bins.items():
        day = ts.normalize()
        if day != current_day:
            seen_today  = set()
            current_day = day
        seen_today |= ids
        cumulative[ts] = len(seen_today)

    return smooth_u, raw_t, smooth_t, cumulative


def shade_treatment_days(ax, start_time, end_time):
    current = start_time.normalize()
    while current <= end_time:
        if current.dayofweek in TREATMENT_DAYS:
            ax.axvspan(current, current + timedelta(days=1),
                       color="#a8d5b5", alpha=0.35, zorder=0)
        current += timedelta(days=1)


def get_global_times():
    max_time = None
    for hive in HIVES:
        for cam in [hive["cam_rear"], hive["cam_front"]]:
            cam_dir = os.path.join(BASE_DIR, cam)
            png_files = sorted(glob.glob(os.path.join(cam_dir, f"{cam}_*.png")))
            if png_files:
                last_file = os.path.basename(png_files[-1])
                parts = last_file.split("_")
                if len(parts) >= 2:
                    timestamp_str = parts[1].split(".")[0]
                    try:
                        clip_time = pd.to_datetime(timestamp_str, format="%Y%m%dT%H%M%S")
                        if max_time is None or clip_time > max_time:
                            max_time = clip_time
                    except Exception:
                        pass

    if max_time is None:
        max_time = pd.Timestamp.now()

    start_time = max_time - pd.Timedelta(days=WINDOW_DAYS)
    return max_time, start_time


def draw_plot(fig, axes, save_path):
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    fig.clf()
    axes = fig.subplots(4, 1)
    fig.suptitle("Bees in the Hive — Last 7 Days", fontsize=18, fontweight="bold")

    end_time, start_time = get_global_times()

    treatment_patch = mpatches.Patch(color="#a8d5b5", alpha=0.35, label="Treatment (Tue–Wed)")
    legend_added = False

    for ax, hive in zip(axes, HIVES):
        ax.set_title(hive["label"], fontsize=15, fontweight="bold")
        ax.set_ylabel("Untagged bees", fontsize=10, color="steelblue")
        ax.tick_params(axis="y", labelcolor="steelblue", labelsize=10)
        ax.grid(True, alpha=0.3, zorder=1)
        shade_treatment_days(ax, start_time, end_time)

        ax2 = ax.twinx()
        ax2.set_ylabel("Tagged unique IDs (per bin / cumulative)", fontsize=10, color="coral")
        ax2.tick_params(axis="y", labelcolor="coral", labelsize=10)

        rear_data  = load_cam_data(hive["cam_rear"],  end_time, start_time)
        front_data = load_cam_data(hive["cam_front"], end_time, start_time)

        if rear_data is None and front_data is None:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                    ha="center", va="center", color="gray", fontsize=13)
            ax2.set_yticks([])
        else:
            if rear_data is not None:
                smooth_u_r, raw_t_r, smooth_t_r, cumul_r = rear_data
                ax.plot(smooth_u_r.index, smooth_u_r.values, lw=2.0, color=COLORS["untagged_rear"],  zorder=3, label="Untagged (rear)")
                ax2.plot(raw_t_r.index,   raw_t_r.values,    lw=0.5, color=COLORS["tagged_rear"],    alpha=0.3, zorder=2)
                ax2.plot(smooth_t_r.index, smooth_t_r.values, lw=2.0, color=COLORS["tagged_rear"],   zorder=3, label="Tagged/bin (rear)")
                ax2.plot(cumul_r.index,   cumul_r.values,    lw=2.0, color=COLORS["cumul_rear"],     zorder=3, label="Tagged cumul. (rear)", linestyle="dashed")

            if front_data is not None:
                smooth_u_f, raw_t_f, smooth_t_f, cumul_f = front_data
                ax.plot(smooth_u_f.index, smooth_u_f.values, lw=2.0, color=COLORS["untagged_front"], zorder=3, label="Untagged (front)")
                ax2.plot(raw_t_f.index,   raw_t_f.values,    lw=0.5, color=COLORS["tagged_front"],   alpha=0.3, zorder=2)
                ax2.plot(smooth_t_f.index, smooth_t_f.values, lw=2.0, color=COLORS["tagged_front"],  zorder=3, label="Tagged/bin (front)")
                ax2.plot(cumul_f.index,   cumul_f.values,    lw=2.0, color=COLORS["cumul_front"],    zorder=3, label="Tagged cumul. (front)", linestyle="dashed")

            ax.set_ylim(bottom=0)
            ax2.set_ylim(bottom=0)

            lines_1, labels_1 = ax.get_legend_handles_labels()
            lines_2, labels_2 = ax2.get_legend_handles_labels()
            if not legend_added:
                ax.legend([treatment_patch] + lines_1 + lines_2,
                          [treatment_patch.get_label()] + labels_1 + labels_2,
                          loc="upper left", fontsize=9, framealpha=0.7, ncol=2)
                legend_added = True
            else:
                ax.legend(lines_1 + lines_2, labels_1 + labels_2,
                          loc="upper left", fontsize=9, framealpha=0.7, ncol=2)

        ax.set_xlim(start_time, end_time)
        ax.xaxis.set_major_locator(mdates.DayLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %m-%d"))
        ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
        ax.tick_params(axis="x", which="major", rotation=30, labelsize=12)
        ax.tick_params(axis="x", which="minor", length=3)

    fig.tight_layout()
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    if plt.get_backend().lower() != "agg":
        fig.canvas.draw()
        fig.canvas.flush_events()


if __name__ == "__main__":
    plt.ion()
    fig, axes = plt.subplots(4, 1, figsize=(7.2, 14.4))

    UPDATE_INTERVAL_SECONDS = 3600
    SAVE_PATH = "/home/beesbook/bb_monitor/figs/hive_visits_plot.png"

    while True:
        draw_plot(fig, axes, save_path=SAVE_PATH)
        print(f"Last updated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        print(f"Next update in {UPDATE_INTERVAL_SECONDS // 60} minutes", flush=True)
        plt.pause(UPDATE_INTERVAL_SECONDS)
