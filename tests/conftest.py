import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# bb_monitor_systemcheck does `import src.mon as mon` and calls mon.get_config() at
# import time. The real src.mon pulls in cv2 and requests and posts to Telegram, none
# of which the check logic needs. Stub it before anything imports the module under
# test, and capture what would have been sent.
_sent = []


def _install_mon_stub():
    if "src.mon" in sys.modules:
        return
    import src  # the real package, so `src.systemcheck_core` still resolves

    stub = types.ModuleType("src.mon")

    def get_config(default_module="default_config", user_module="user_config"):
        import default_config_systemcheck
        return default_config_systemcheck

    def send_message(config, message):
        _sent.append(message)
        return True

    stub.get_config = get_config
    stub.send_message = send_message
    sys.modules["src.mon"] = stub
    src.mon = stub


_install_mon_stub()
