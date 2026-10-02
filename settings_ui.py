"""Desktop settings window for the wrist panel. Every change is saved immediately and the
running panel applies it within a second, so you can adjust it while wearing the headset."""
import tkinter as tk
from tkinter import ttk

import sounddevice as sd

import settings

cfg = settings.load()
root = tk.Tk()
root.title("Wrist Media settings")
root.resizable(False, False)
frm = ttk.Frame(root, padding=14)
frm.grid()
row = 0
_save_job = None


def save_soon():
    """Debounced save: dragging a slider doesn't write the file 50 times a second."""
    global _save_job
    if _save_job:
        root.after_cancel(_save_job)
    _save_job = root.after(150, lambda: settings.save(cfg))


def section(text):
    global row
    ttk.Label(frm, text=text, font=("Segoe UI", 10, "bold")).grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 2))
    row += 1


def slider(label, key, lo, hi, scale=1.0, unit="", digits=0):
    """A labelled slider showing the setting divided by `scale` (e.g. metres shown as cm)."""
    global row
    ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w")
    var = tk.DoubleVar(value=cfg[key] / scale)
    shown = ttk.Label(frm, width=8)

    def changed(*_):
        v = round(var.get(), digits)
        shown.config(text=f"{v:g} {unit}")
        cfg[key] = round(v * scale, 4) if scale != 1 else (int(v) if digits == 0 else v)
        save_soon()

    ttk.Scale(frm, from_=lo, to=hi, variable=var, length=260, command=changed).grid(row=row, column=1, padx=8)
    shown.grid(row=row, column=2, sticky="w")
    changed()
    sliders.append((var, key, scale, changed))
    row += 1


sliders = []

section("Panel")
ttk.Label(frm, text="Wrist").grid(row=row, column=0, sticky="w")
hand = tk.StringVar(value=cfg["hand"])
hf = ttk.Frame(frm)
hf.grid(row=row, column=1, sticky="w", padx=8)
for text, val in (("Left", "left"), ("Right", "right")):
    ttk.Radiobutton(hf, text=text, value=val, variable=hand,
                    command=lambda: (cfg.update(hand=hand.get()), save_soon())).pack(side="left", padx=(0, 12))
row += 1
slider("Size", "width_m", 6, 25, 0.01, "cm", 1)
slider("Left / right", "offset_x", -10, 10, 0.01, "cm", 1)
slider("Up / down", "offset_y", -10, 10, 0.01, "cm", 1)
slider("Along the arm", "offset_z", -5, 30, 0.01, "cm", 1)
slider("Tilt", "tilt_deg", -90, 30, 1, "°")

section("Touch (other hand's controller)")
slider("Fingertip reach", "poke_tip_m", 0, 15, 0.01, "cm", 1)
slider("Press distance", "poke_press_m", 0.5, 5, 0.01, "cm", 1)

section("Sharing music in VRChat")
ttk.Label(frm, text="Your mic").grid(row=row, column=0, sticky="w")
wasapi = next(i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "Windows WASAPI")
mics = sorted({d["name"] for d in sd.query_devices()
               if d["hostapi"] == wasapi and d["max_input_channels"] > 0
               and "Steam Streaming" not in d["name"] and "CABLE" not in d["name"]})
DEFAULT_MIC = "Windows default mic"
mic = ttk.Combobox(frm, values=[DEFAULT_MIC] + mics, state="readonly", width=40)
mic.set(cfg["mic"] or DEFAULT_MIC)
mic.bind("<<ComboboxSelected>>", lambda e: (cfg.update(mic="" if mic.get() == DEFAULT_MIC else mic.get()), save_soon()))
mic.grid(row=row, column=1, columnspan=2, sticky="w", padx=8)
row += 1
slider("Others hear (start)", "others_pct", 0, 100, 1, "%")
ttk.Label(frm, text="In VRChat set the mic to \"Steam Streaming Microphone\" and turn noise suppression off.",
          foreground="#666").grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
row += 1


def reset():
    cfg.clear()
    cfg.update(settings.DEFAULTS)
    hand.set(cfg["hand"])
    mic.set(DEFAULT_MIC)
    for var, key, scale, changed in sliders:
        var.set(cfg[key] / scale)
        changed()
    settings.save(cfg)


bf = ttk.Frame(frm)
bf.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(14, 0))
ttk.Label(bf, text="Changes apply to the panel instantly.", foreground="#666").pack(side="left")
ttk.Button(bf, text="Reset to defaults", command=reset).pack(side="right")

root.mainloop()
