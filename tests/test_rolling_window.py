#!/home/beesbook/miniconda3/envs/beesbook/bin/python3
# test rolling 24h unique ID count using already-cached data
# validates whether counts fall in expected range of 288-463 tagged bees
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from collections import defaultdict

CACHE_DIR  = "/home/beesbook/bb_monitor/cache/test"
MIN_SNAPSHOTS = 2    # ID must appear in at least N snapshots within the 24h window
WINDOW_H   = 24      # rolling window in hours

CAMS_HIVE_A = ["cam-0", "cam-1"]    # rear and front of hive A

for cam in CAMS_HIVE_A:
    cache_path = os.path.join(CACHE_DIR, f"{cam}_hive_cache.pkl")
    if not os.path.exists(cache_path):
        print(f"No cache found for {cam} at {cache_path}")
        continue

    cache  = pd.read_pickle(cache_path)
    images = cache.get("images", [])
    print(f"\n{cam}: {len(images)} cached images")

    # sort by time
    images = sorted(images, key=lambda x: x["clip_time"])
    times  = [img["clip_time"] for img in images]
    ids    = [img["tagged_ids"] for img in images]

    # at each snapshot, compute unique IDs seen in the last 24h with min appearances
    window = pd.Timedelta(hours=WINDOW_H)
    rolling_counts = []
    for i, t in enumerate(times):
        # find all snapshots within the last 24h
        window_ids = defaultdict(int)
        for j in range(i, -1, -1):
            if times[j] < t - window:
                break
            for bee_id in ids[j]:
                window_ids[bee_id] += 1
        # only count IDs seen in at least MIN_SNAPSHOTS within the window
        unique = sum(1 for count in window_ids.values() if count >= MIN_SNAPSHOTS)
        rolling_counts.append((t, unique))

    df = pd.DataFrame(rolling_counts, columns=["time", "unique_ids"])
    print(f"  Rolling 24h unique IDs (min {MIN_SNAPSHOTS} snapshots):")
    print(f"    min={df['unique_ids'].min()}  max={df['unique_ids'].max()}  "
          f"final={df['unique_ids'].iloc[-1]}")
    print(f"    Expected range: 288–463")

    # also show without min_snapshots filter for comparison
    rolling_counts_nofilter = []
    for i, t in enumerate(times):
        window_ids = set()
        for j in range(i, -1, -1):
            if times[j] < t - window:
                break
            window_ids |= ids[j]
        rolling_counts_nofilter.append((t, len(window_ids)))

    df_nf = pd.DataFrame(rolling_counts_nofilter, columns=["time", "unique_ids"])
    print(f"  Rolling 24h unique IDs (no filter):")
    print(f"    min={df_nf['unique_ids'].min()}  max={df_nf['unique_ids'].max()}  "
          f"final={df_nf['unique_ids'].iloc[-1]}")

# plot both cameras together
fig, axes = plt.subplots(2, 1, figsize=(10, 8))
fig.suptitle(f"Rolling {WINDOW_H}h unique tagged IDs — Hive A", fontsize=14)

for ax, cam in zip(axes, CAMS_HIVE_A):
    cache_path = os.path.join(CACHE_DIR, f"{cam}_hive_cache.pkl")
    if not os.path.exists(cache_path):
        continue

    cache  = pd.read_pickle(cache_path)
    images = sorted(cache.get("images", []), key=lambda x: x["clip_time"])
    times  = [img["clip_time"] for img in images]
    ids    = [img["tagged_ids"] for img in images]
    window = pd.Timedelta(hours=WINDOW_H)

    counts_filtered   = []
    counts_nofilter   = []
    for i, t in enumerate(times):
        window_ids    = defaultdict(int)
        window_ids_nf = set()
        for j in range(i, -1, -1):
            if times[j] < t - window:
                break
            for bee_id in ids[j]:
                window_ids[bee_id] += 1
            window_ids_nf |= ids[j]
        counts_filtered.append(sum(1 for c in window_ids.values() if c >= MIN_SNAPSHOTS))
        counts_nofilter.append(len(window_ids_nf))

    ax.plot(times, counts_nofilter, lw=1.5, color="steelblue", alpha=0.5, label="no filter")
    ax.plot(times, counts_filtered, lw=2.0, color="steelblue", label=f"min {MIN_SNAPSHOTS} snapshots")
    ax.axhline(463, color="red",    linestyle="dotted", lw=1.5, label="max (463)")
    ax.axhline(288, color="orange", linestyle="dotted", lw=1.5, label="min (288)")
    ax.set_title(cam)
    ax.set_ylabel("unique tagged IDs")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig("/home/beesbook/bb_monitor/figs/test_rolling_window.png", dpi=150, bbox_inches="tight")
print("\nSaved to figs/test_rolling_window.png")
