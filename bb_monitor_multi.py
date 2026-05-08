"""Run multiple bb_monitor configs in one process, one thread per config."""
import sys
import threading
import time
import traceback

import src.mon as mon
from bb_monitor import wait_and_get_images

RESTART_BACKOFF_SECONDS = 10


def _thread_runner(config):
    label = getattr(config, "monitor_bot_name", "?")
    while True:
        try:
            wait_and_get_images(config)
        except Exception as e:
            print(
                f"[{label}] crashed: {e}; restarting in {RESTART_BACKOFF_SECONDS}s",
                flush=True,
            )
            traceback.print_exc()
        else:
            print(
                f"[{label}] returned; restarting in {RESTART_BACKOFF_SECONDS}s",
                flush=True,
            )
        time.sleep(RESTART_BACKOFF_SECONDS)


def main():
    paths = sys.argv[1:]
    if not paths:
        print("usage: bb_monitor_multi.py <config1.py> [<config2.py> ...]")
        sys.exit(2)

    configs = [mon.load_config_from_path(p) for p in paths]
    print(
        f"[multi] starting {len(configs)} monitors: "
        f"{', '.join(c.monitor_bot_name for c in configs)}",
        flush=True,
    )

    for cfg in configs:
        t = threading.Thread(
            target=_thread_runner,
            args=(cfg,),
            name=cfg.monitor_bot_name,
            daemon=True,
        )
        t.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("[multi] received interrupt; exiting", flush=True)


if __name__ == "__main__":
    main()
