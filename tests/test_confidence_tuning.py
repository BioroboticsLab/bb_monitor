#!/home/beesbook/miniconda3/envs/beesbook/bin/python3
# sweep confidence thresholds on one full day of images from one camera
# computes actual cumulative unique IDs per day at each threshold
import glob
import numpy as np
import matplotlib.pyplot as plt
from bb_binary import parse_video_fname
from bb_behavior.tracking import detect_markers_in_video
from bb_utils.ids import BeesbookID

BASE_DIR = "/mnt/trove/beesbook2026/single_video_frames"
CAM      = "cam-0"
DAY      = "20260610"    # one full day to test on

THRESHOLDS = [0.0, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4]

png_files = sorted(glob.glob(f"{BASE_DIR}/{CAM}/{CAM}_*{DAY}*.png"))
print(f"Found {len(png_files)} images for {DAY} on {CAM}")

# run detection once, store raw bit probabilities per detection
print("Running detection...")
all_detections = []    # list of (filename, list of bit arrays)
for f in png_files:
    try:
        cam_id, _, _ = parse_video_fname(f)
        _, df = detect_markers_in_video(
            f,
            tag_pixel_diameter=38.0,
            cam_id=cam_id,
            confidence_filter=0.001,
            use_parallel_jobs=False,
            progress=None,
        )
        df = df[df["localizerSaliency"] >= 0.5]
        tagged = df[df["detection_type"] == "TaggedBee"]
        bits_list = []
        for _, row in tagged.iterrows():
            if row["beeID"] is not None:
                bits_list.append(np.array(row["beeID"]))
        all_detections.append(bits_list)
        print(f"  {f.split('/')[-1]}: {len(bits_list)} tagged detections")
    except Exception as e:
        print(f"  Failed {f}: {e}")

# sweep thresholds and compute actual cumulative unique IDs for the day
print("\nThreshold sweep:")
print(f"{'Threshold':>10}  {'Detections kept':>16}  {'Cumulative unique IDs':>22}")
results = []
for thresh in THRESHOLDS:
    seen_today  = set()
    total_kept  = 0
    for bits_list in all_detections:
        for bits in bits_list:
            min_conf = float(np.min(np.abs(bits - 0.5)))
            if min_conf < thresh:
                continue
            try:
                bee_id = BeesbookID.from_bb_binary(bits).as_ferwar()
                seen_today.add(bee_id)
                total_kept += 1
            except Exception:
                pass
    results.append((thresh, total_kept, len(seen_today)))
    print(f"{thresh:>10.2f}  {total_kept:>16}  {len(seen_today):>22}")

# plot
fig, ax = plt.subplots(figsize=(9, 5))
threshs    = [r[0] for r in results]
unique_ids = [r[2] for r in results]
kept       = [r[1] for r in results]

ax.plot(threshs, unique_ids, marker="o", color="steelblue", label="cumulative unique IDs (full day)")
ax.axhline(463, color="red",    linestyle="dotted", lw=1.5, label="max tagged bees (463)")
ax.axhline(288, color="orange", linestyle="dotted", lw=1.5, label="min tagged bees (288)")
ax.set_xlabel("min bit confidence threshold")
ax.set_ylabel("cumulative unique bee IDs")
ax.set_title(f"Confidence tuning — {CAM}, {DAY}, {len(png_files)} images")
ax.legend()
ax.grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig("/home/beesbook/bb_monitor/figs/test_confidence_tuning.png", dpi=150, bbox_inches="tight")
print("\nSaved to figs/test_confidence_tuning.png")
