# VrMC

**VR media controls on your wrist.** A SteamVR overlay that shows what's playing and lets you control
it without leaving VR, and optionally lets people in VRChat hear your music.

![The VrMC panel](preview.png)

## Features

- **Now playing**: title, artist and album art for whatever is in the Windows media controls
  (YouTube Music in a browser, Spotify, Pear Desktop...).
- **⏮ ⏯ ⏭** previous, play/pause, next.
- **You hear**: a volume slider and mute for the music app only, not your headset or VRChat.
- **SHARE + others hear**: mixes your music into your VRChat mic, with its own volume, independent of
  yours. Off every time VrMC starts.
- **Level bars** under both sliders.
- **Touch or laser**: poke it with your other controller, or laser-click it while the dashboard is open.
- **Grab to move**: hold grip or trigger on an empty spot and move it. The position is remembered.
- **Any language**: Hebrew and Arabic (right to left), Japanese, Chinese, Korean, Cyrillic and emoji,
  also mixed in one title.
- **Starts with SteamVR**, and quits with it.

## Install

Needs Windows 10 (2004+) or 11, SteamVR, and Python 3.12+.

```
pip install -r requirements.txt
```

Start SteamVR, then double-click `run.bat`. The first run registers VrMC to start with SteamVR from then
on (turn it off in SteamVR Settings → Startup/Shutdown → Choose Startup Overlay Apps).

## Use

The panel appears on your **left wrist**.

| | |
| --- | --- |
| **Press a button** | Touch it with your right controller's tip, or laser-click it (dashboard open) |
| **Move the panel** | Touch an empty spot (title area, gaps), hold grip or trigger, move, let go |
| **Volume** | Drag the *you hear* slider; 🔊 mutes the music app |
| **Share your music** | Tap **SHARE**, set *others hear* (see below) |

Both sliders use a perceptual curve: 50% sounds about half as loud.

### Letting people in VRChat hear your music

1. In VRChat → Settings → Audio, set **Microphone** to **Steam Streaming Microphone** and turn
   **noise suppression off** (it removes music as noise).
2. Tap **SHARE** on the panel.

VrMC mixes your real mic (your Windows default mic) and a copy of the music app's audio into that
virtual mic. You keep hearing the music normally.

- While VRChat uses that mic, your voice goes through VrMC: if VrMC isn't running, VRChat hears
  nothing. Switch VRChat back to your normal mic in that case.
- SHARE copies everything the music app plays, for example other tabs in the same browser.

## Settings

The panel position is saved to `settings.json` when you let go of it. A few more values can be edited
there by hand (restart VrMC afterwards):

| Key | Default | What it does |
| --- | --- | --- |
| `hand` | `"left"` | Which wrist the panel is on (`"left"` / `"right"`) |
| `width_m` | `0.13` | Panel width in metres |
| `poke_tip_m` | `0.05` | How far in front of the controller its "fingertip" is |
| `poke_press_m` | `0.015` | How close the fingertip must get to press |
| `mic` | `""` | Mic to mix into VRChat; empty = Windows default |
| `others_pct` | `50` | Starting *others hear* level |
| `panel_pose` | | Saved position; delete it to reset the panel |

Problems are logged to `%TEMP%\vrmc.log`.

## How it works

- `vrmc.py`: the overlay. It reads Windows media sessions, controls the music app's own volume
  (like the Windows volume mixer), and draws the panel into an OpenGL texture that it hands to SteamVR,
  the way OpenVR-SpaceCalibrator and OVR Advanced Settings do. That's why updates don't blink. The grab
  button comes from SteamVR Input (`input/`), so it doesn't take buttons away from your game.
- `music_share.py`: captures only the music app's audio (Windows process loopback), mixes it with
  your mic, and plays the result into the Steam Streaming Microphone.

## Known issues

- SHARE shows ON even if capturing the music failed; check `%TEMP%\vrmc.log`.
- Music sharing doesn't recognise Pear Desktop yet.

## License

Public domain ([Unlicense](LICENSE)).
