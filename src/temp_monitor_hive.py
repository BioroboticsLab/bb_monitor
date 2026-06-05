import os
import glob
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches


BASE_DIR = "/mnt/trove/beesbook2026/single_video_frames"

# CAMS = [
#     {
#         "dir": "Hives A & B",
#         "hives": [
#             {"machine": "cirrus", "label": "A", "col_rear": 1, "col_front": 2},
#             {"machine": "cirrus", "label": "B", "col_rear": 3, "col_front": 4},
#         ],
#     },
#     {
#         "label": "Hives C & D",
#         "hives": [
#             {"machine": "thria", "label": "C", "col_rear": 1, "col_front": 2},
#             {"machine": "thria", "label": "D", "col_rear": 3, "col_front": 4},
#         ],
#     },
# ]

# # one color pair per hive in the panel
# HIVE_COLORS = [
#     {"rear": "steelblue",  "front": "cornflowerblue"},   # first hive in pair
#     {"rear": "coral",      "front": "tomato"},            # second hive in pair
# ]

CACHE_DAYS = 15
CACHE_DIR = "cache"

def load_cache(cam):
    """Returns the cached data for a given camera.
    The cache data contains the list of clips that have already been processed
    """

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{cam}_hive_cache.pkl")
    if os.path.exists(cache_path):
        try:
            cache = pd.read_pickle(cache_path)
            # TODO: Check why
            if isinstance(cache, dict) and "data" in cache:
                return cache
        except Exception:
            pass
    return {"data": None}
                              
def load_hive_data(cam):
    png_files = sorted(glob.glob(
        f"{BASE_DIR}/{cam}/{cam}_*.png"
    ))
    if not png_files:
        return None
    
    end_time = pd.to_datetime(os.path.basename(png_files[-1]).split("_")[-1].split(".")[0], format="%Y%m%dT%H%M%S")
    history_start_time = end_time - pd.Timedelta(days=CACHE_DAYS)
    # cache = load_cache(cam)
    cached_clips = load_cache(cam)     #TODO: add something else to the cache if needed

    # filter files to only include those within the cache window
    candidate_files = []
    for f in png_files:
        base = os.path.basename(f)
        timestamp_str = base[len(cam)+1 : len(cam)+16]
        if timestamp_str >= history_start_time.strftime("%Y%m%d%H%M%S"):
            candidate_files.append(f)


    updated_clips = []
    files_to_process = []
    for f in candidate_files:
        if cached_clips != candidate_files:
            files_to_process.append(f)
        
    


            

def draw_plot(fig, axes, save_path):
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    fig.clf()
    axes = fig.subplots(4, 1)
    fig.suptitle("Number of bees in the hive — Last 7 Days", fontsize=18, fontweight="bold")

    tratment_patch = mpatches.Patch(color="#a8d5b5", alpha=0.35, label="Treatment (Tue–Wed)")
    # untagged_line  =
    # tagged_line    = 

    legend_added = False

    for ax in axes:
        result = load_hive_data()