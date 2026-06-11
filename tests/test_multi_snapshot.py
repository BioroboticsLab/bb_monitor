#!/home/beesbook/miniconda3/envs/beesbook/bin/python3
# test whether requiring an ID to appear in N snapshots reduces false positives
import glob
import numpy as np
import matplotlib.pyplot as plt
from collections import defaultdict
from bb_binary import parse_video_fname
from bb_behavior.tracking import detect_markers_in_video
from bb_utils.ids import BeesbookID

BASE_DIR = "/mnt/trove/beesbook2026/single_video_frames"
CAM      = "cam-0"
DAY      = "20260610"

png_files = sorted(glob.glob(f"{BASE_DIR}/{CAM}/{CAM}_*{DAY}*.png"))
print(f"Found {len(png_files)} images for {DAY} on {CAM}")

# run detection, record which snapshot each ID appeared in
print("Running detection...")
id_snapshot_count = defaultdict(int)    # how many snapshots each ID appears in
snapshot_ids      = []                  # list of ID sets per snapshot

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

        ids_this_snapshot = set()
        for _, row in tagged.iterrows():
            if row["beeID"] is not None:
                bits = np.array(row["beeID"])
                try:
                    bee_id = BeesbookID.from_bb_binary(bits).as_ferwar()
                    ids_this_snapshot.add(bee_id)
                except Exception:
                    pass

        for bee_id in ids_this_snapshot:
            id_snapshot_count[bee_id] += 1
        snapshot_ids.append(ids_this_snapshot)
        print(f"  {f.split('/')[-1]}: {len(ids_this_snapshot)} unique IDs")
    except Exception as e:
        print(f"  Failed {f}: {e}")

# sweep min appearances threshold
print("\nMin appearances sweep:")
print(f"{'Min snapshots':>14}  {'Unique IDs kept':>16}  {'IDs removed':>12}")
results = []
all_ids = set(id_snapshot_count.keys())
for min_snap in range(1, 8):
    kept    = {k for k, v in id_snapshot_count.items() if v >= min_snap}
    removed = len(all_ids) - len(kept)
    results.append((min_snap, len(kept), removed))
    print(f"{min_snap:>14}  {len(kept):>16}  {removed:>12}")

# also show distribution of how often IDs appear
counts = list(id_snapshot_count.values())
print(f"\nID appearance distribution:")
for n in range(1, 10):
    num = sum(1 for c in counts if c == n)
    print(f"  appears in exactly {n} snapshot(s): {num} IDs")
print(f"  appears in 10+ snapshots: {sum(1 for c in counts if c >= 10)} IDs")

# plot
fig, axes = plt.subplots(1, 2, figsize=(12, 5))
fig.suptitle(f"Multi-snapshot filter — {CAM}, {DAY}, {len(png_files)} images", fontsize=13)

ax = axes[0]
ax.plot([r[0] for r in results], [r[1] for r in results], marker="o", color="steelblue")
ax.axhline(463, color="red",    linestyle="dotted", lw=1.5, label="max tagged bees (463)")
ax.axhline(288, color="orange", linestyle="dotted", lw=1.5, label="min tagged bees (288)")
ax.set_xlabel("min snapshots an ID must appear in")
ax.set_ylabel("cumulative unique IDs kept")
ax.set_title("Unique IDs vs min appearances")
ax.legend()
ax.grid(True, alpha=0.3)

ax = axes[1]
bins = range(1, max(counts) + 2)
ax.hist(counts, bins=bins, color="steelblue", edgecolor="white", align="left")
ax.axvline(2, color="red", linestyle="dashed", label="min=2 cutoff")
ax.set_xlabel("number of snapshots ID appears in")
ax.set_ylabel("number of IDs")
ax.set_title("Distribution of ID appearances")
ax.legend()
ax.grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig("/home/beesbook/bb_monitor/figs/test_multi_snapshot.png", dpi=150, bbox_inches="tight")
print("\nSaved to figs/test_multi_snapshot.png")
