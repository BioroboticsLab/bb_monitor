#!/home/beesbook/miniconda3/envs/beesbook/bin/python3
import glob
import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from collections import defaultdict
from datetime import timedelta
from bb_binary import parse_video_fname
from bb_behavior.tracking import detect_markers_in_video
from bb_utils.ids import BeesbookID

BASE_DIR             = "/mnt/trove/beesbook2026/single_video_frames"
WINDOW_DAYS          = 7
TREATMENT_DAYS       = {1, 2}    # tue=1, wed=2
BIN_MIN              = 15        # 15-min bins, one image per bin
ROLL_WIN_UNTAGGED    = 24        # rolling mean over 24 bins = 6 hours
ROLL_WIN_TAGGED      = 96        # rolling window over 96 bins = 24 hours
MIN_SNAPSHOTS        = 3         # ID must appear in >= 3 bins within 24h to be counted
CACHE_DIR            = "/home/beesbook/bb_monitor/cache"

HIVES = [
    {"label": "Hive A", "cam_left": "cam-0", "cam_right": "cam-1"},
    {"label": "Hive B", "cam_left": "cam-2", "cam_right": "cam-3"},
    {"label": "Hive C", "cam_left": "cam-4", "cam_right": "cam-5"},
    {"label": "Hive D", "cam_left": "cam-6", "cam_right": "cam-7"},
]

COLORS = {
    "untagged_left":  "steelblue",
    "untagged_right": "cornflowerblue",
    "cumul_left":     "coral",
    "cumul_right":    "tomato",
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

    if video_dataframe is None:
        return None

    df = video_dataframe
    df = df[df["localizerSaliency"] >= 0.5]

    tagged_ids     = set()
    untagged_count = len(df[df["detection_type"] != "TaggedBee"])

    for _, row in df[df["detection_type"] == "TaggedBee"].iterrows():
        if row["beeID"] is not None:
            try:
                bee_id = BeesbookID.from_bb_binary(np.array(row["beeID"])).as_ferwar()
                tagged_ids.add(bee_id)
            except Exception:
                pass

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
    raw_u    = u_series.resample(freq).mean()
    smooth_u = raw_u.rolling(window=ROLL_WIN_UNTAGGED, center=True, min_periods=1).mean()

    return smooth_u, updated_images


def compute_union_rolling(left_images, right_images):
    # merge images from both cameras and bin by 15 min
    all_images = (left_images or []) + (right_images or [])
    if not all_images:
        return None

    freq      = f"{BIN_MIN}min"
    id_series = pd.Series(
        [img["tagged_ids"] for img in all_images],
        index=[img["clip_time"] for img in all_images],
    )
    bins      = id_series.resample(freq).apply(
        lambda x: set().union(*x) if len(x) > 0 else set()
    )

    # for each bin, count IDs seen >= MIN_SNAPSHOTS times in the preceding rolling window
    window    = pd.Timedelta(minutes=BIN_MIN * ROLL_WIN_TAGGED)
    bins_list = list(bins.items())
    rolling   = {}
    for i, (ts, _) in enumerate(bins_list):
        window_start = ts - window
        id_counts    = defaultdict(int)
        for j in range(i, -1, -1):
            t_j, ids_j = bins_list[j]
            if t_j < window_start:
                break
            for bee_id in ids_j:
                id_counts[bee_id] += 1
        rolling[ts] = sum(1 for c in id_counts.values() if c >= MIN_SNAPSHOTS)

    result = pd.Series(rolling)

    # only forward fill for large gaps (> 6h) — the 24h rolling window naturally
    # bridges small gaps without showing a visible dip, so no fill is needed there.
    # recovery ends at t0 + 24h, not t1 + 24h, because pre-gap data ages out of
    # the window 24h after the last pre-gap image (t0), not after recording resumes (t1).
    gap_threshold = pd.Timedelta(hours=6)
    recovery      = pd.Timedelta(minutes=BIN_MIN * ROLL_WIN_TAGGED)
    image_times   = sorted(set(img["clip_time"] for img in all_images))
    for i in range(len(image_times) - 1):
        t0, t1 = image_times[i], image_times[i + 1]
        if t1 - t0 > gap_threshold:
            before_gap = result[result.index <= t0]
            if before_gap.empty:
                continue
            last_count   = before_gap.iloc[-1]
            recovery_end = t0 + recovery
            in_fill      = (result.index > t0) & (result.index < recovery_end)
            result.loc[in_fill] = last_count

    return result


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
        for cam in [hive["cam_left"], hive["cam_right"]]:
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
        ax2.set_ylabel("Tagged unique IDs (rolling 24h)", fontsize=10, color="coral")
        ax2.tick_params(axis="y", labelcolor="coral", labelsize=10)

        left_data  = load_cam_data(hive["cam_left"],  end_time, start_time)
        right_data = load_cam_data(hive["cam_right"], end_time, start_time)

        left_images  = left_data[1]  if left_data  is not None else None
        right_images = right_data[1] if right_data is not None else None

        if left_data is None and right_data is None:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                    ha="center", va="center", color="gray", fontsize=13)
            ax2.set_yticks([])
        else:
            if left_data is not None:
                smooth_u_l, _ = left_data
                ax.plot(smooth_u_l.index, smooth_u_l.values, lw=2.0, color=COLORS["untagged_left"], zorder=3, label="Untagged (left)")

            if right_data is not None:
                smooth_u_r, _ = right_data
                ax.plot(smooth_u_r.index, smooth_u_r.values, lw=2.0, color=COLORS["untagged_right"], zorder=3, label="Untagged (right)")

            rolling_union = compute_union_rolling(left_images, right_images)
            if rolling_union is not None:
                ax2.plot(rolling_union.index, rolling_union.values, lw=2.0, color=COLORS["cumul_left"], zorder=3, label="Tagged unique IDs")

            ax.set_ylim(bottom=0)
            ax2.set_ylim(bottom=0)

            lines_1, labels_1 = ax.get_legend_handles_labels()
            lines_2, labels_2 = ax2.get_legend_handles_labels()
            if not legend_added:
                ax.legend([treatment_patch] + lines_1 + lines_2,
                          [treatment_patch.get_label()] + labels_1 + labels_2,
                          loc="lower left", fontsize=9, framealpha=0.7, ncol=2)
                legend_added = True
            else:
                ax.legend(lines_1 + lines_2, labels_1 + labels_2,
                          loc="lower left", fontsize=9, framealpha=0.7, ncol=2)

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
