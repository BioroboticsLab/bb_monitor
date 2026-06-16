#!/home/beesbook/miniconda3/envs/beesbook/bin/python3
import glob
import os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from collections import defaultdict
from datetime import timedelta
from bb_binary import parse_video_fname
from bb_behavior.tracking import detect_markers_in_video
from bb_utils.ids import BeesbookID

BASE_DIR          = "/mnt/trove/beesbook2026/single_video_frames"
CACHE_DIR         = "/home/beesbook/bb_monitor/cache/history"
SAVE_PATH         = "/home/beesbook/bb_monitor/figs/population_history.png"
TREATMENT_DAYS    = {1, 2}    # tue=1, wed=2
BIN_MIN           = 15        # 15-min bins, one image per bin
ROLL_WIN_UNTAGGED = 24        # rolling mean over 24 bins = 6 hours
ROLL_WIN_TAGGED   = 96        # rolling window over 96 bins = 24 hours
MIN_SNAPSHOTS     = 3         # ID must appear in >= 3 bins within 24h to be counted

HIVES = [
    {"label": "Hive A", "cam_left": "cam-0", "cam_right": "cam-1"},
    {"label": "Hive B", "cam_left": "cam-2", "cam_right": "cam-3"},
    {"label": "Hive C", "cam_left": "cam-4", "cam_right": "cam-5"},
    {"label": "Hive D", "cam_left": "cam-6", "cam_right": "cam-7"},
]

COLORS = {
    "untagged_left":  "steelblue",
    "untagged_right": "cornflowerblue",
    "tagged":         "coral",
}


def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def cache_file_path(cam):
    return os.path.join(CACHE_DIR, f"{cam}_history_cache.pkl")


def load_cache(cam):
    ensure_cache_dir()
    path = cache_file_path(cam)
    if os.path.exists(path):
        try:
            cache = pd.read_pickle(path)
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
            try:
                bee_id = BeesbookID.from_bb_binary(np.array(row["beeID"])).as_ferwar()
                tagged_ids.add(bee_id)
            except Exception:
                pass

    return tagged_ids, untagged_count


def load_cam_data(cam):
    # load all available images for this camera, not just a 7-day window
    cam_dir   = os.path.join(BASE_DIR, cam)
    png_files = sorted(glob.glob(os.path.join(cam_dir, f"{cam}_*.png")))
    if not png_files:
        return None

    cache         = load_cache(cam)
    cached_images = {img["file_path"]: img for img in cache.get("images", [])}

    candidate_files = []
    for f in png_files:
        base  = os.path.basename(f)
        parts = base.split("_")
        if len(parts) >= 2:
            ts_str = parts[1].split(".")[0]
            try:
                clip_time = pd.to_datetime(ts_str, format="%Y%m%dT%H%M%S")
                candidate_files.append({"clip_time": clip_time, "file_path": f})
            except Exception:
                continue

    if not candidate_files:
        return None

    updated_images  = []
    files_to_process = []
    for item in candidate_files:
        f = item["file_path"]
        try:
            mtime = os.path.getmtime(f)
        except OSError:
            continue
        cached = cached_images.get(f)
        if (cached is None or cached.get("file_mtime") != mtime
                or "tagged_ids" not in cached or "untagged_count" not in cached):
            files_to_process.append(item)
        else:
            updated_images.append(cached)

    for item in files_to_process:
        f         = item["file_path"]
        clip_time = item["clip_time"]
        res       = detect_bees(f)
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

    freq     = f"{BIN_MIN}min"
    u_series = pd.Series({img["clip_time"]: img["untagged_count"] for img in updated_images})
    raw_u    = u_series.resample(freq).mean()
    smooth_u = raw_u.rolling(window=ROLL_WIN_UNTAGGED, center=True, min_periods=1).mean()

    return smooth_u, updated_images


def compute_union_rolling(left_images, right_images):
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

    # forward fill through gaps and recovery window so missing data
    # does not show as a population drop
    gap_threshold = pd.Timedelta(minutes=BIN_MIN * 2)
    recovery      = pd.Timedelta(minutes=BIN_MIN * ROLL_WIN_TAGGED)
    image_times   = sorted(set(img["clip_time"] for img in all_images))
    for i in range(len(image_times) - 1):
        t0, t1 = image_times[i], image_times[i + 1]
        if t1 - t0 > gap_threshold:
            before_gap = result[result.index <= t0]
            if before_gap.empty:
                continue
            last_count   = before_gap.iloc[-1]
            recovery_end = t1 + recovery
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
    # find the earliest and latest timestamps across all cameras
    min_time = None
    max_time = None
    for hive in HIVES:
        for cam in [hive["cam_left"], hive["cam_right"]]:
            cam_dir   = os.path.join(BASE_DIR, cam)
            png_files = sorted(glob.glob(os.path.join(cam_dir, f"{cam}_*.png")))
            for f, is_last in [(png_files[0], False), (png_files[-1], True)] if png_files else []:
                parts  = os.path.basename(f).split("_")
                if len(parts) >= 2:
                    try:
                        t = pd.to_datetime(parts[1].split(".")[0], format="%Y%m%dT%H%M%S")
                        if min_time is None or t < min_time:
                            min_time = t
                        if max_time is None or t > max_time:
                            max_time = t
                    except Exception:
                        pass
    if max_time is None:
        max_time = pd.Timestamp.now()
    if min_time is None:
        min_time = max_time - pd.Timedelta(days=7)
    return min_time, max_time


def draw_plot():
    os.makedirs(os.path.dirname(SAVE_PATH), exist_ok=True)

    start_time, end_time = get_global_times()
    n_days = max(1, (end_time - start_time).days + 1)

    # scale figure width with number of days so labels stay readable
    fig_width = max(10, n_days)
    fig, axes = plt.subplots(4, 1, figsize=(fig_width, 14.4))
    fig.suptitle(
        f"Hive Population History  {start_time.strftime('%b %d')} – {end_time.strftime('%b %d, %Y')}",
        fontsize=18, fontweight="bold",
    )

    treatment_patch = mpatches.Patch(color="#a8d5b5", alpha=0.35, label="Treatment (Tue–Wed)")
    legend_added    = False

    for ax, hive in zip(axes, HIVES):
        ax.set_title(hive["label"], fontsize=15, fontweight="bold")
        ax.set_ylabel("Untagged bees", fontsize=10, color="steelblue")
        ax.tick_params(axis="y", labelcolor="steelblue", labelsize=10)
        ax.grid(True, alpha=0.3, zorder=1)
        shade_treatment_days(ax, start_time, end_time)

        ax2 = ax.twinx()
        ax2.set_ylabel("Tagged unique IDs (rolling 24h)", fontsize=10, color="coral")
        ax2.tick_params(axis="y", labelcolor="coral", labelsize=10)

        left_data  = load_cam_data(hive["cam_left"])
        right_data = load_cam_data(hive["cam_right"])

        left_images  = left_data[1]  if left_data  is not None else None
        right_images = right_data[1] if right_data is not None else None

        if left_data is None and right_data is None:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                    ha="center", va="center", color="gray", fontsize=13)
            ax2.set_yticks([])
        else:
            if left_data is not None:
                smooth_u_l, _ = left_data
                ax.plot(smooth_u_l.index, smooth_u_l.values, lw=2.0,
                        color=COLORS["untagged_left"], zorder=3, label="Untagged (left)")

            if right_data is not None:
                smooth_u_r, _ = right_data
                ax.plot(smooth_u_r.index, smooth_u_r.values, lw=2.0,
                        color=COLORS["untagged_right"], zorder=3, label="Untagged (right)")

            rolling_union = compute_union_rolling(left_images, right_images)
            if rolling_union is not None:
                ax2.plot(rolling_union.index, rolling_union.values, lw=2.0,
                         color=COLORS["tagged"], zorder=3, label="Tagged unique IDs")

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
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %b-%d"))
        ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
        ax.tick_params(axis="x", which="major", rotation=30, labelsize=10)
        ax.tick_params(axis="x", which="minor", length=3)

    fig.tight_layout()
    fig.savefig(SAVE_PATH, dpi=150, bbox_inches="tight")
    print(f"Saved to {SAVE_PATH}")


if __name__ == "__main__":
    draw_plot()
