#!/home/beesbook/miniconda3/envs/beesbook/bin/python3
import os
import glob
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from bb_binary import parse_video_fname
from bb_behavior.tracking import detect_markers_in_video

BASE_DIR = "/mnt/trove/beesbook2026/single_video_frames"
WINDOW_DAYS = 7
BIN_MIN = 10    # 10-min bins
ROLL_WIN = 6    # rolling mean over 6 bins = 1 hour
CACHE_DIR = "/home/beesbook/bb_monitor/cache"

def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)

def cache_file_path(cam):
    return os.path.join(CACHE_DIR, f"{cam}_hive_cache.pkl")

def load_cache(cam):
    ensure_cache_dir()
    cache_path = cache_file_path(cam)
    if os.path.exists(cache_path):
        try:
            import pandas as pd
            cache = pd.read_pickle(cache_path)
            if isinstance(cache, dict) and "images" in cache:
                return cache
        except Exception:
            pass
    return {"images": []}

def save_cache(cam, cache):
    ensure_cache_dir()
    try:
        import pandas as pd
        pd.to_pickle(cache, cache_file_path(cam))
    except Exception:
        pass

HIVES = [
    {"label": "Hive A", "cam_rear": "cam-0", "cam_front": "cam-1"},
    {"label": "Hive B", "cam_rear": "cam-2", "cam_front": "cam-3"},
    {"label": "Hive C", "cam_rear": "cam-4", "cam_front": "cam-5"},
    {"label": "Hive D", "cam_rear": "cam-6", "cam_front": "cam-7"},
]

COLORS = {
    "untagged_rear": "steelblue",
    "untagged_front": "cornflowerblue",
    "tagged_rear": "coral",
    "tagged_front": "tomato"
}

def detect_bees(image_path):
    tag_pixel_diameter = 38.0
    try:
        cam_id, image_timestamp, video_end_time = parse_video_fname(image_path)
        frame_info, video_dataframe = detect_markers_in_video(
                    image_path,
                    tag_pixel_diameter=tag_pixel_diameter,
                    cam_id=cam_id,
                    confidence_filter=0.001,
                    use_parallel_jobs=False, progress=None)
    except:
        return None
    
    df = video_dataframe
    confidence = 0.5
    df = df[df["localizerSaliency"] >= confidence]
    df_tp_tagged = df[(df['detection_type']=='TaggedBee')]
    df_tp_untagged = df[~(df['detection_type']=='TaggedBee')]
    
    return df_tp_tagged, df_tp_untagged

    

def load_cam_data(cam, end_time, start_time):
    """Loads and processes image timestamps for a given camera."""
    cam_dir = os.path.join(BASE_DIR, cam)
    png_files = sorted(glob.glob(os.path.join(cam_dir, f"{cam}_*.png")))
    if not png_files:
        return None

    cache = load_cache(cam)
    cached_images = {img["file_path"]: img for img in cache.get("images", [])}

    candidate_files = []
    for f in png_files:
        base = os.path.basename(f)
        parts = base.split('_')
        if len(parts) >= 2:
            timestamp_str = parts[1].split('.')[0]
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
        if cached is None or cached.get("file_mtime") != mtime:
            files_to_process.append(item)
        else:
            updated_images.append(cached)

    # Process new files
    for item in files_to_process:
        f = item["file_path"]
        clip_time = item["clip_time"]
        res = detect_bees(f)
        if res is not None:
            tagged_df, untagged_df = res
            updated_images.append({
                "clip_time": clip_time,
                "file_path": f,
                "file_mtime": os.path.getmtime(f),
                "tagged_count": len(tagged_df),
                "untagged_count": len(untagged_df)
            })
            print(f"Processed {f}: tagged={len(tagged_df)}, untagged={len(untagged_df)}")
        else:
            print(f"Failed to process {f}")

    updated_images = sorted(updated_images, key=lambda c: c["clip_time"])
    
    if files_to_process or len(updated_images) != len(cache.get("images", [])):
        save_cache(cam, {"images": updated_images})

    if not updated_images:
        return None

    df_results = pd.DataFrame(updated_images).set_index("clip_time")
    
    freq = f"{BIN_MIN}min"
    
    # Resample and smooth
    raw_u = df_results["untagged_count"].resample(freq).mean().fillna(0)
    smooth_u = raw_u.rolling(window=ROLL_WIN, center=True, min_periods=1).mean()
    
    raw_t = df_results["tagged_count"].resample(freq).mean().fillna(0)
    smooth_t = raw_t.rolling(window=ROLL_WIN, center=True, min_periods=1).mean()
    
    return smooth_u, smooth_t

def get_global_times():
    max_time = None
    for hive in HIVES:
        for cam in [hive["cam_rear"], hive["cam_front"]]:
            cam_dir = os.path.join(BASE_DIR, cam)
            png_files = sorted(glob.glob(os.path.join(cam_dir, f"{cam}_*.png")))
            if png_files:
                last_file = os.path.basename(png_files[-1])
                parts = last_file.split('_')
                if len(parts) >= 2:
                    timestamp_str = parts[1].split('.')[0]
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
    fig.suptitle("Number of Bees in the Hive — Last 7 Days", fontsize=18, fontweight="bold")

    end_time, start_time = get_global_times()

    for ax, hive in zip(axes, HIVES):
        ax.set_title(hive["label"], fontsize=15, fontweight="bold")
        ax.set_ylabel("Untagged Bees", fontsize=10)
        ax.grid(True, alpha=0.3, zorder=1)

        ax2 = ax.twinx()
        ax2.set_ylabel("Tagged Bees", fontsize=10)

        rear_data = load_cam_data(hive["cam_rear"], end_time, start_time)
        if rear_data:
            smooth_u_r, smooth_t_r = rear_data
            ax.plot(smooth_u_r.index, smooth_u_r.values, lw=2.0, color=COLORS["untagged_rear"], label="Untagged (rear)")
            ax2.plot(smooth_t_r.index, smooth_t_r.values, lw=2.0, color=COLORS["tagged_rear"], label="Tagged (rear)")

        front_data = load_cam_data(hive["cam_front"], end_time, start_time)
        if front_data:
            smooth_u_f, smooth_t_f = front_data
            ax.plot(smooth_u_f.index, smooth_u_f.values, lw=2.0, color=COLORS["untagged_front"], label="Untagged (front)")
            ax2.plot(smooth_t_f.index, smooth_t_f.values, lw=2.0, color=COLORS["tagged_front"], label="Tagged (front)")

        if not rear_data and not front_data:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center", va="center", color="gray", fontsize=13)
            ax2.set_yticks([])
        else:
            lines_1, labels_1 = ax.get_legend_handles_labels()
            lines_2, labels_2 = ax2.get_legend_handles_labels()
            ax.legend(lines_1 + lines_2, labels_1 + labels_2, loc="upper left", fontsize=9, framealpha=0.7, ncol=2)
        
        ax.set_ylim(bottom=0)
        ax2.set_ylim(bottom=0)
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
