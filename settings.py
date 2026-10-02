"""Settings shared by the overlay (reads them live) and settings_ui.py (edits them)."""
import json
import os

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULTS = {
    "hand": "left",          # which wrist the panel is on; the other hand touches it
    "width_m": 0.13,         # panel width in metres
    "offset_x": 0.0,         # panel position relative to the controller, metres
    "offset_y": 0.03,
    "offset_z": 0.12,        # +z = toward you (along the arm)
    "tilt_deg": -50,         # tilt toward your face
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
    os.replace(tmp, PATH)  # atomic: the overlay never reads a half-written file


def mtime():
    try:
        return os.path.getmtime(PATH)
    except OSError:
        return 0
