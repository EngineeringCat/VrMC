"""SteamVR wrist overlay: now playing, prev / play-pause / next, and music-only volume.

Reads Windows media sessions (works with YouTube Music in a browser or Pear Desktop).
Touch the panel with the other controller's tip (or laser-click it with the dashboard open):
top row = previous / play-pause / next, bottom row = mute + volume slider + SHARE
(SHARE on = people in VRChat hear your music too, see music_share.py).
"""
import asyncio
import io
import json
import math
import os
import sys
import tempfile
import time

import numpy as np
import openvr
from PIL import Image, ImageDraw, ImageFont
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions
sys.coinit_flags = 0  # MTA: comtypes defaults to STA, which stalls the WinRT awaits
from pycaw.pycaw import AudioUtilities
import music_share
import settings

cfg = settings.load()  # panel placement, touch, mic: edit with settings_ui.py (applied live)


def hand_roles():
    """(panel hand, touching hand) as OpenVR controller roles."""
    left, right = openvr.TrackedControllerRole_LeftHand, openvr.TrackedControllerRole_RightHand
    return (left, right) if cfg["hand"] == "left" else (right, left)


PREFER = ("youtube", "pear", "music")  # preferred media apps

W, H = 600, 500
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def font(name, size):
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except OSError:
        return ImageFont.load_default()


F_TITLE, F_ARTIST, F_ICON = font("segoeuib.ttf", 34), font("segoeui.ttf", 26), font("seguisym.ttf", 64)
F_VOL = font("seguisym.ttf", 44)
F_SMALL = font("segoeui.ttf", 18)


def fit(draw, text, f, max_w):
    if draw.textlength(text, font=f) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return text + "…"


VOL_Y0, VOL_Y1 = 300, 385          # your volume row:   [mute]  [slider] [pct]
SHARE_Y0, SHARE_Y1 = 400, 485      # others' volume row: [SHARE] [slider] [pct]
MUTE_X1, SLIDER_X0, SLIDER_X1 = 120, 155, 465   # same x layout in both rows


# --- volume: only the music app (like its slider in the Windows volume mixer) ---
# Perceptual curve: loudness ~ amplitude^0.6 (Stevens' law), so amplitude = slider^(1/0.6) makes every
# part of the slider change loudness by the same amount. 50% = half as loud, 0% = silent.
CURVE = 1 / 0.6
MUSIC_PROCS = ("firefox", "pear", "youtube", "chrome", "msedge", "brave", "librewolf")
music_app = ""  # media session's app id, e.g. "firefox.exe"; set by the main loop


def music_volumes():
    app = music_app.lower()
    out = []
    for s in AudioUtilities.GetAllSessions():
        name = s.Process.name().lower() if s.Process else ""
        if name and (name == app or name.removesuffix(".exe") in app
                     or (not app.endswith(".exe") and any(p in name for p in MUSIC_PROCS))):
            out.append(s.SimpleAudioVolume)
    return out


def get_volume():
    vols = music_volumes()
    if not vols:
        return 0, False
    a = vols[0].GetMasterVolume()
    music_share.you_amp = a
    pct = round(100 * a ** (1 / CURVE))
    return max(0, min(100, pct)), bool(vols[0].GetMute())


def set_volume(pct):
    a = (max(0, min(100, pct)) / 100) ** CURVE
    music_share.you_amp = a
    for v in music_volumes():
        v.SetMasterVolume(a, None)


def toggle_mute():
    vols = music_volumes()
    if vols:
        m = not vols[0].GetMute()
        for v in vols:
            v.SetMute(m, None)


def render(title, artist, playing, art, vol, muted, sharing, share_pct, pressed=None):
    img = Image.new("RGBA", (W, H), (18, 18, 22, 235))
    d = ImageDraw.Draw(img)
    x = 20
    if art:
        a = art.resize((150, 150))
        mask = Image.new("L", a.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 150, 150), 14, fill=255)
        img.paste(a, (20, 20), mask)
        x = 190
    d.text((x, 40), fit(d, title or "Nothing playing", F_TITLE, W - x - 20), font=F_TITLE, fill="white")
    d.text((x, 95), fit(d, artist or "", F_ARTIST, W - x - 20), font=F_ARTIST, fill=(180, 180, 190))
    icons = ["⏮", "⏸" if playing else "▶", "⏭"]
    for i, ic in enumerate(icons):
        cx0, cx1 = i * W // 3 + 10, (i + 1) * W // 3 - 10
        fill = (255, 0, 60) if pressed == i else (40, 40, 48)
        d.rounded_rectangle((cx0, 190, cx1, 285), 18, fill=fill)
        tw = d.textlength(ic, font=F_ICON)
        d.text(((cx0 + cx1 - tw) / 2, 195), ic, font=F_ICON, fill="white")
    def slider_row(y0, y1, value, active, caption):
        cy = (y0 + y1) // 2
        d.text((SLIDER_X0, y0 - 2), caption, font=F_SMALL, fill=(150, 150, 160))
        d.rounded_rectangle((SLIDER_X0, cy - 8, SLIDER_X1, cy + 8), 8, fill=(55, 55, 65))
        kx = SLIDER_X0 + (SLIDER_X1 - SLIDER_X0) * value / 100
        d.rounded_rectangle((SLIDER_X0, cy - 8, kx, cy + 8), 8, fill=(255, 0, 60) if active else (110, 110, 120))
        d.ellipse((kx - 18, cy - 18, kx + 18, cy + 18), fill="white")
        d.text((SLIDER_X1 + 30, cy - 20), f"{value}%", font=F_ARTIST, fill="white")
        return cy

    # your volume: [mute] [slider] [pct]
    cy = slider_row(VOL_Y0, VOL_Y1, vol, not muted, "you hear")
    d.rounded_rectangle((10, VOL_Y0, MUTE_X1, VOL_Y1), 18,
                        fill=(255, 0, 60) if pressed == "mute" or muted else (40, 40, 48))
    ic = "🔇" if muted else "🔊"
    d.text(((10 + MUTE_X1 - d.textlength(ic, font=F_VOL)) / 2, cy - 26), ic, font=F_VOL, fill="white")
    # what people in VRChat hear: [SHARE] [slider] [pct]
    cy = slider_row(SHARE_Y0, SHARE_Y1, share_pct, sharing, "others hear")
    d.rounded_rectangle((10, SHARE_Y0, MUTE_X1, SHARE_Y1), 18,
                        fill=(255, 0, 60) if pressed == "share" or sharing else (40, 40, 48))
    for line, ty in (("SHARE", cy - 32), ("ON" if sharing else "OFF", cy + 2)):
        d.text(((10 + MUTE_X1 - d.textlength(line, font=F_ARTIST)) / 2, ty), line, font=F_ARTIST, fill="white")
    # rounded panel corners
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W, H), 24, fill=255)
    img.putalpha(Image.composite(img.getchannel("A"), mask, mask))
    return img


METER_SEGMENTS = 24


def draw_meters(img, you, others):
    """LED-style level bars under both sliders; levels are 0..1 (see meter_level)."""
    d = ImageDraw.Draw(img)
    seg_w = (SLIDER_X1 - SLIDER_X0) / METER_SEGMENTS
    for (y0, y1), level in (((VOL_Y0, VOL_Y1), you), ((SHARE_Y0, SHARE_Y1), others)):
        cy = (y0 + y1) // 2
        lit = round(level * METER_SEGMENTS)
        for i in range(METER_SEGMENTS):
            x0 = SLIDER_X0 + i * seg_w
            d.rectangle((x0, cy + 25, x0 + seg_w - 3, cy + 31), fill=(255, 0, 60) if i < lit else (45, 45, 55))


def meter_level(peak):
    """Peak amplitude -> 0..1 on a -48..0 dB scale (how loud it sounds, not raw amplitude)."""
    return 0.0 if peak <= 0 else max(0.0, min(1.0, 1 + 20 * math.log10(peak) / 48))


def wrist_matrix():
    m = openvr.HmdMatrix34_t()
    c, s = math.cos(math.radians(cfg["tilt_deg"])), math.sin(math.radians(cfg["tilt_deg"]))
    rows = [[1, 0, 0, cfg["offset_x"]], [0, c, -s, cfg["offset_y"]], [0, s, c, cfg["offset_z"]]]
    for r in range(3):
        for col in range(4):
            m.m[r][col] = rows[r][col]
    return m


async def read_thumb(ref):
    try:
        stream = await ref.open_read_async()
        buf = Buffer(stream.size)
        await stream.read_async(buf, buf.capacity, InputStreamOptions.READ_AHEAD)
        data = bytearray(buf.length)
        DataReader.from_buffer(buf).read_bytes(data)
        return Image.open(io.BytesIO(bytes(data))).convert("RGBA")
    except Exception:
        return None


def pick_session(mgr):
    sessions = list(mgr.get_sessions())
    for s in sessions:
        if any(p in s.source_app_user_model_id.lower() for p in PREFER):
            return s
    return mgr.get_current_session() or (sessions[0] if sessions else None)


APP_KEY = "wrist.media.yt"
LOG = os.path.join(tempfile.gettempdir(), "wrist_media.log")

# touch: tip of the other controller (cfg["poke_tip_m"] along its pointing direction, -z) must hover
# within POKE_ARM_M, then come within cfg["poke_press_m"] of the panel; it releases POKE_RELEASE_GAP_M past that
POKE_ARM_M = 0.08
POKE_RELEASE_GAP_M = 0.02
EDGE_PX = 12                                    # ignore touches right at the panel edge


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + msg + "\n")


def mat4(m):
    return np.vstack([np.array([[m.m[r][c] for c in range(4)] for r in range(3)]), [0, 0, 0, 1]])


def poke_point(ov, handle):
    """Other controller's tip in panel pixels: (x, y-from-top, distance_m), or None if off the panel."""
    s = openvr.VRSystem()
    dev, rel = ov.getOverlayTransformTrackedDeviceRelative(handle)
    dev = int(getattr(dev, "value", dev))
    other = s.getTrackedDeviceIndexForControllerRole(hand_roles()[1])
    if other == openvr.k_unTrackedDeviceIndexInvalid:
        return None
    poses = s.getDeviceToAbsoluteTrackingPose(openvr.TrackingUniverseStanding, 0, openvr.k_unMaxTrackedDeviceCount)
    if not (poses[dev].bPoseIsValid and poses[other].bPoseIsValid):
        return None
    panel = mat4(poses[dev].mDeviceToAbsoluteTracking) @ mat4(rel)
    tip = mat4(poses[other].mDeviceToAbsoluteTracking) @ np.array([0, 0, -cfg["poke_tip_m"], 1])
    lx, ly, lz, _ = np.linalg.inv(panel) @ tip
    width = cfg["width_m"]
    half_w, half_h = width / 2, width * H / W / 2
    if abs(lx) > half_w or abs(ly) > half_h:
        return None
    return (lx / width + 0.5) * W, (0.5 - ly / (2 * half_h)) * H, lz


def register_autostart():
    """Register with SteamVR so it launches us every time SteamVR starts."""
    here = os.path.dirname(os.path.abspath(__file__))
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    manifest = os.path.join(here, "wrist_media.vrmanifest")
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump({"source": "builtin", "applications": [{
            "app_key": APP_KEY,
            "launch_type": "binary",
            "binary_path_windows": pythonw,
            "arguments": f'"{os.path.join(here, "wrist_media.py")}"',
            "is_dashboard_overlay": True,
            "strings": {"en_us": {"name": "Wrist Media", "description": "YouTube Music controls on your wrist"}},
        }]}, f, indent=2)
    try:
        apps = openvr.VRApplications()
        apps.addApplicationManifest(manifest, False)
        apps.setApplicationAutoLaunch(APP_KEY, True)
    except Exception as e:
        print("autostart registration failed:", e)


async def main():
    openvr.init(openvr.VRApplication_Overlay)
    register_autostart()
    ov = openvr.IVROverlay()
    try:
        handle = ov.createOverlay(APP_KEY, "Wrist Media")
    except openvr.error_code.OverlayError:
        print("Already running.")  # another copy owns the overlay
        openvr.shutdown()
        return
    ov.setOverlayWidthInMeters(handle, cfg["width_m"])
    ov.setOverlayInputMethod(handle, openvr.VROverlayInputMethod_Mouse)
    scale = openvr.HmdVector2_t()
    scale.v[0], scale.v[1] = W, H
    ov.setOverlayMouseScale(handle, scale)
    ov.showOverlay(handle)

    mgr = await SessionManager.request_async()
    music_share.mic_choice = cfg["mic"]
    music_share.start(log)
    cfg_mtime = settings.mtime()
    base = None                    # panel without meters, re-rendered only when something on it changes
    m_you = m_others = 0.0         # meter levels (fall off smoothly)
    shown_meters, shown_at = None, 0
    png = os.path.join(tempfile.gettempdir(), "wrist_media.png")
    laser_down = False
    state, art, art_key, shown, pressed_until, pressed = None, None, None, None, 0, None
    art_until = 0  # keep re-reading the cover until then: apps send the new title before the new cover
    attached_to, last_poll, session = None, 0, None
    vol, muted, dragging, poking, armed, toggle_ready = 0, False, None, False, False, 0
    share_pct = cfg["others_pct"]  # others' slider; same curve as yours, independent of it
    music_share.share_gain = (share_pct / 100) ** CURVE
    print("Wrist media overlay running. Ctrl+C to quit.")

    def slider_pct(x):
        return round(max(0, min(1, (x - SLIDER_X0) / (SLIDER_X1 - SLIDER_X0))) * 100)

    async def press(x, y):
        """Shared by laser click and touch. y is measured from the top of the panel."""
        nonlocal vol, muted, dragging, pressed, pressed_until, toggle_ready, last_poll
        if y >= VOL_Y0 - 10:
            row_you = y < SHARE_Y0 - 7
            if x <= MUTE_X1 + 10:
                if time.time() > toggle_ready:  # stops a bounce or double-click from flipping back
                    if row_you:
                        toggle_mute()
                        muted = not muted
                        pressed = "mute"
                    else:
                        music_share.sharing = not music_share.sharing
                        log(f"share {'ON' if music_share.sharing else 'OFF'}")
                        pressed = "share"
                    pressed_until, toggle_ready = time.time() + 0.25, time.time() + 1.0
            else:
                dragging = "you" if row_you else "others"
                drag(x)
        elif session:
            zone = min(2, int(x / (W / 3)))
            pressed, pressed_until = zone, time.time() + 0.25
            await (session.try_skip_previous_async, session.try_toggle_play_pause_async,
                   session.try_skip_next_async)[zone]()
        last_poll = 0

    def drag(x):
        nonlocal vol, share_pct
        p = slider_pct(x)
        if dragging == "you" and p != vol:
            vol = p
            set_volume(vol)
        elif dragging == "others" and p != share_pct:
            share_pct = p
            music_share.share_gain = (p / 100) ** CURVE

    while True:
        # keep attached to the hand (controller may connect later)
        if settings.mtime() != cfg_mtime:  # settings window saved: apply live
            cfg_mtime = settings.mtime()
            cfg.update(settings.load())
            ov.setOverlayWidthInMeters(handle, cfg["width_m"])
            music_share.mic_choice = cfg["mic"]
            attached_to = None  # re-attach with the new hand / position / tilt
        idx = openvr.VRSystem().getTrackedDeviceIndexForControllerRole(hand_roles()[0])
        if idx != openvr.k_unTrackedDeviceIndexInvalid and idx != attached_to:
            ov.setOverlayTransformTrackedDeviceRelative(handle, idx, wrist_matrix())
            attached_to = idx

        # laser clicks (OpenVR mouse y starts at the bottom of the overlay)
        ev = openvr.VREvent_t()
        while ov.pollNextOverlayEvent(handle, ev)[0]:  # pyopenvr returns (ok, event)
            t = ev.eventType
            x, y = ev.data.mouse.x, H - ev.data.mouse.y
            if t in (openvr.VREvent_MouseButtonDown, openvr.VREvent_MouseButtonUp):
                log(f"laser click type={t} at ({x:.0f},{y:.0f})")
            try:
                if t == openvr.VREvent_MouseButtonDown:
                    laser_down = True
                    await press(x, y)
                elif t == openvr.VREvent_MouseMove and dragging and laser_down:
                    drag(x)
                elif t in (openvr.VREvent_MouseButtonUp, openvr.VREvent_FocusLeave):
                    # FocusLeave: the laser left the panel; its button-up may never be delivered here,
                    # which used to leave the slider following the laser around
                    laser_down, dragging = False, None
                elif t == openvr.VREvent_Quit:
                    openvr.shutdown()
                    return
            except Exception as e:
                log(f"action failed: {e!r}")

        # poke with the other controller's tip (works with the dashboard closed)
        try:
            hit = poke_point(ov, handle)
        except Exception:
            hit = None
        # only a press if the tip was hovering in front of the panel (not near an edge) the frame
        # before: sliding in from the side or passing through the arm doesn't count
        inside = hit and EDGE_PX < hit[0] < W - EDGE_PX and EDGE_PX < hit[1] < H - EDGE_PX
        press_m = cfg["poke_press_m"]
        was_armed, armed = armed, bool(inside and press_m < abs(hit[2]) < POKE_ARM_M)
        if inside and was_armed and not poking and abs(hit[2]) <= press_m:
            poking = True
            log(f"poke at ({hit[0]:.0f},{hit[1]:.0f})")
            try:
                await press(hit[0], hit[1])
            except Exception as e:
                log(f"poke action failed: {e!r}")
        elif poking and (not hit or abs(hit[2]) > press_m + POKE_RELEASE_GAP_M):
            poking, dragging = False, None
        elif poking and dragging and hit:
            drag(hit[0])

        if time.time() - last_poll > 0.5:
            last_poll = time.time()
            session = pick_session(mgr)
            global music_app
            music_app = session.source_app_user_model_id if session else ""
            music_share.music_app = music_app
            title = artist = ""
            playing = False
            if session:
                try:
                    props = await session.try_get_media_properties_async()
                    title, artist = props.title, props.artist
                    playing = session.get_playback_info().playback_status == PlaybackStatus.PLAYING
                    if (title, artist) != art_key:
                        art_key, art, art_until = (title, artist), None, time.time() + 6
                    if props.thumbnail and time.time() < art_until:
                        new = await read_thumb(props.thumbnail)
                        if new is not None and (art is None or new.tobytes() != art.tobytes()):
                            art = new
                except Exception:
                    pass
            if not dragging:
                try:
                    vol, muted = get_volume()  # re-read: picks up changes made in the Windows volume mixer
                except Exception:
                    pass
            state = (title, artist, playing)

        if time.time() > pressed_until:
            pressed = None
        frame = (state, vol, muted, music_share.sharing, share_pct, pressed, id(art))
        if frame != shown and state:
            base = render(*state, art, vol, muted, music_share.sharing, share_pct, pressed)
            shown, shown_meters = frame, None
        m_you = max(meter_level(music_share.level_you), m_you - 0.06)
        m_others = max(meter_level(music_share.level_others), m_others - 0.06)
        meters = (round(m_you * METER_SEGMENTS), round(m_others * METER_SEGMENTS))
        if base is not None and meters != shown_meters and time.time() - shown_at > 1 / 15:
            img = base.copy()
            draw_meters(img, m_you, m_others)
            img.save(png, compress_level=0)  # uncompressed: ~10x faster to write than default PNG
            ov.setOverlayFromFile(handle, png)
            shown_meters, shown_at = meters, time.time()

        await asyncio.sleep(1 / 30)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        openvr.shutdown()
        sys.exit(0)
