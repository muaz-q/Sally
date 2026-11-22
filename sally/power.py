"""Battery state, from GetSystemPowerStatus."""
import ctypes
from ctypes import wintypes


class _Status(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


def read():
    s = _Status()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return {"on_battery": False, "percent": None, "saver": False}
    return {
        "on_battery": s.ACLineStatus == 0,
        "percent": None if s.BatteryLifePercent == 255 or s.BatteryFlag in (128, 255) else s.BatteryLifePercent,
        "saver": s.SystemStatusFlag == 1,   # Windows battery saver / energy saver
    }


def saving_reason(status, threshold, already_saving=False):
    """Why live mode should rest right now, or None. Coming back needs 5% more charge than
    going off, so it does not flip back and forth around the threshold."""
    if status["saver"]:
        return "battery saver is on"
    if status["on_battery"] and status["percent"] is not None:
        if status["percent"] <= threshold + (5 if already_saving else 0):
            return f"battery is at {status['percent']}%"
    return None
