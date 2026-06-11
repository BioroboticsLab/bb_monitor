#!/home/beesbook/miniconda3/envs/beesbook/bin/python3
# visualize detections on a single hive image to check for false positives
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from PIL import Image
from bb_binary import parse_video_fname
from bb_behavior.tracking import detect_markers_in_video
from bb_utils.ids import BeesbookID

IMAGE_PATH = "/mnt/trove/beesbook2026/single_video_frames/cam-0/cam-0_20260610T153218.114849.374Z--20260610T153318.44138.964Z.png"

tag_pixel_diameter = 38.0
cam_id, _, _ = parse_video_fname(IMAGE_PATH)
_, df = detect_markers_in_video(
    IMAGE_PATH,
    tag_pixel_diameter=tag_pixel_diameter,
    cam_id=cam_id,
    confidence_filter=0.001,
    use_parallel_jobs=False,
    progress=None,
)

df = df[df["localizerSaliency"] >= 0.5]
tagged   = df[df["detection_type"] == "TaggedBee"]
untagged = df[df["detection_type"] != "TaggedBee"]

print(f"Untagged detections: {len(untagged)}")
print(f"Tagged detections:   {len(tagged)}")

# decode IDs and print them
ids = []
for _, row in tagged.iterrows():
    if row["beeID"] is not None:
        try:
            bee_id = BeesbookID.from_bb_binary(np.array(row["beeID"])).as_ferwar()
            ids.append(bee_id)
        except Exception:
            pass

print(f"Unique decoded IDs:  {len(set(ids))}")
print(f"All decoded IDs: {sorted(set(ids))}")

# overlay detections on the image
img = np.array(Image.open(IMAGE_PATH))
fig, ax = plt.subplots(1, 1, figsize=(12, 10))
ax.imshow(img, cmap="gray")

if "xpos" in df.columns and "ypos" in df.columns:
    for _, row in untagged.iterrows():
        ax.add_patch(plt.Circle((row["xpos"], row["ypos"]), 10, color="steelblue", fill=False, lw=1.0))
    for _, row in tagged.iterrows():
        ax.add_patch(plt.Circle((row["xpos"], row["ypos"]), 12, color="coral", fill=False, lw=1.5))

ax.set_title(f"Untagged: {len(untagged)}   Tagged: {len(tagged)}   Unique IDs: {len(set(ids))}", fontsize=13)
ax.legend(handles=[
    mpatches.Patch(color="steelblue", label="Untagged"),
    mpatches.Patch(color="coral",     label="Tagged"),
], loc="upper right")
ax.axis("off")

plt.tight_layout()
plt.savefig("/home/beesbook/bb_monitor/figs/test_detection_vis.png", dpi=150, bbox_inches="tight")
print("Saved to figs/test_detection_vis.png")
