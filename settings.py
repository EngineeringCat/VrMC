"""VrMC's saved settings (settings.json). The panel position is saved when you let go of the panel;
the rest can be edited by hand in settings.json (restart VrMC after editing)."""
import json
import os

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULTS = {
    "hand": "left",          # which wrist the panel is on; the other hand touches it
    "width_m": 0.13,         # panel width in metres
    "panel_pose": None,      # 3x4 pose relative to the wrist controller; set by grabbing the panel
    "poke_tip_m": 0.05,      # how far in front of the touching controller its "fingertip" is
    "poke_press_m": 0.015,   # how close the fingertip must get to the panel to press
    "mic": "",               # "" = Windows default mic, else the device name
    "others_pct": 50,        # starting "others hear" level
}


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(cfg):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, PATH)  # atomic: never leaves a half-written file
