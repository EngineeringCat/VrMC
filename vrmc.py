"""VrMC: music controls on your wrist in SteamVR.

Shows what's playing (Windows media controls, e.g. YouTube Music in a browser), previous / play-pause /
next, a volume slider for the music app only, and SHARE + a second slider that mix the music into your
VRChat mic (see music_share.py). Touch it with the other controller's tip, or laser-click it while the
SteamVR dashboard is open. To move it: hold the trigger on an empty part of the panel, move, let go
(the spot is remembered, relative to your wrist).
"""
import asyncio
import ctypes
import io
import json
import math
import os
import sys
import tempfile
import threading
import time

import glfw
import numpy as np
import openvr
from OpenGL import GL
from bidi.algorithm import get_display  # this one also mirrors brackets in right-to-left text
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

APP_KEY = "vrmc"
LOG = os.path.join(tempfile.gettempdir(), "vrmc.log")
PREFER = ("youtube", "pear", "music")  # preferred media apps
W, H = 600, 500
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + msg + "\n")


def font(name, size):
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except OSError:
        return ImageFont.load_default()


F_TITLE, F_ARTIST, F_ICON = font("segoeuib.ttf", 34), font("segoeui.ttf", 26), font("seguisym.ttf", 64)
F_VOL = font("seguisym.ttf", 44)
F_SMALL = font("segoeui.ttf", 18)
# Segoe UI covers Latin, Cyrillic, Hebrew, Arabic... but not CJK or emoji (those turn into boxes), so text
# is drawn in runs, each with a font that has its characters
TITLE_FONTS = {"text": F_TITLE, "ja": font("YuGothB.ttc", 34), "ko": font("malgunbd.ttf", 34),
               "emoji": font("seguiemj.ttf", 30)}
ARTIST_FONTS = {"text": F_ARTIST, "ja": font("YuGothM.ttc", 26), "ko": font("malgun.ttf", 26),
                "emoji": font("seguiemj.ttf", 23)}


def script(c):
    o = ord(c)
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "ko"
    if o >= 0x1F000 or 0x2600 <= o <= 0x27BF:
        return "emoji"
    if 0x2E80 <= o <= 0xFFEF:  # CJK, kana, fullwidth forms
        return "ja"
    return "text"


def runs(text):
    out = []
    for c in text:
        k = script(c)
        if out and (out[-1][1] == k or c == " "):  # spaces join the run they're in
            out[-1][0] += c
        else:
            out.append([c, k])
    return out


def text_width(d, text, fonts):
    return sum(d.textlength(chunk, font=fonts[k]) for chunk, k in runs(text))


def draw_text(d, x, baseline, text, fonts, max_w, fill):
    """Draw mixed-script text on one baseline, cut with "…" to fit max_w. Pillow lays text out left to
    right only, so Hebrew / Arabic are put into visual order first (cut first, so "…" ends the title)."""
    if text_width(d, text, fonts) > max_w:
        while text and text_width(d, text + "…", fonts) > max_w:
            text = text[:-1]
        text += "…"
    for chunk, k in runs(get_display(text)):
        d.text((x, baseline), chunk, font=fonts[k], fill=fill, anchor="ls", embedded_color=k == "emoji")
        x += d.textlength(chunk, font=fonts[k])

BTN_Y0, BTN_Y1 = 190, 285          # previous / play-pause / next
VOL_Y0, VOL_Y1 = 300, 385          # your volume row:    [mute]  [slider] [pct]
SHARE_Y0, SHARE_Y1 = 400, 485      # others' volume row: [SHARE] [slider] [pct]
MUTE_X1, SLIDER_X0, SLIDER_X1 = 120, 155, 465   # same x layout in both rows
RED, BTN, BG = (255, 0, 60), (40, 40, 48), (18, 18, 22)


# --- volume: only the music app (like its slider in the Windows volume mixer) ---
# Perceptual curve: loudness ~ amplitude^0.6 (Stevens' law), so amplitude = slider^(1/0.6) makes every
# part of the slider change loudness by the same amount. 50% = half as loud, 0% = silent.
CURVE = 1 / 0.6


def music_volumes():
    """Volume controls of the music app's audio sessions (same app SHARE copies)."""
    return [s.SimpleAudioVolume for s in AudioUtilities.GetAllSessions()
            if s.Process and music_share.is_music_process(s.Process.name())]


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


# --- drawing ---
AMBER = (255, 160, 0)


def render(title, artist, playing, art, vol, muted, sharing, share_pct, pressed=None, share_problem=None):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    x = 20
    if art:
        a = art.resize((150, 150))
        mask = Image.new("L", a.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 150, 150), 14, fill=255)
        img.paste(a, (20, 20), mask)
        x = 190
    draw_text(d, x, 80, title or "Nothing playing", TITLE_FONTS, W - x - 20, "white")
    draw_text(d, x, 125, artist or "", ARTIST_FONTS, W - x - 20, (180, 180, 190))
    for i, ic in enumerate(["⏮", "⏸" if playing else "▶", "⏭"]):
        cx0, cx1 = i * W // 3 + 10, (i + 1) * W // 3 - 10
        d.rounded_rectangle((cx0, BTN_Y0, cx1, BTN_Y1), 18, fill=RED if pressed == i else BTN)
        d.text(((cx0 + cx1 - d.textlength(ic, font=F_ICON)) / 2, BTN_Y0 + 5), ic, font=F_ICON, fill="white")

    def slider_row(y0, y1, value, active, caption, caption_fill=(150, 150, 160)):
        cy = (y0 + y1) // 2
        d.text((SLIDER_X0, y0 - 2), caption, font=F_SMALL, fill=caption_fill)
        d.rounded_rectangle((SLIDER_X0, cy - 8, SLIDER_X1, cy + 8), 8, fill=(55, 55, 65))
        kx = SLIDER_X0 + (SLIDER_X1 - SLIDER_X0) * value / 100
        d.rounded_rectangle((SLIDER_X0, cy - 8, kx, cy + 8), 8, fill=RED if active else (110, 110, 120))
        d.ellipse((kx - 18, cy - 18, kx + 18, cy + 18), fill="white")
        d.text((SLIDER_X1 + 30, cy - 20), f"{value}%", font=F_ARTIST, fill="white")
        return cy

    # your volume: [mute] [slider] [pct]
    cy = slider_row(VOL_Y0, VOL_Y1, vol, not muted, "you hear")
    d.rounded_rectangle((10, VOL_Y0, MUTE_X1, VOL_Y1), 18, fill=RED if pressed == "mute" or muted else BTN)
    ic = "🔇" if muted else "🔊"
    d.text(((10 + MUTE_X1 - d.textlength(ic, font=F_VOL)) / 2, cy - 26), ic, font=F_VOL, fill="white")
    # what people in VRChat hear: [SHARE] [slider] [pct]
    broken = sharing and share_problem
    cy = slider_row(SHARE_Y0, SHARE_Y1, share_pct, sharing and not broken,
                    f"others hear nothing: {share_problem}" if broken else "others hear",
                    AMBER if broken else (150, 150, 160))
    d.rounded_rectangle((10, SHARE_Y0, MUTE_X1, SHARE_Y1), 18,
                        fill=AMBER if broken else RED if pressed == "share" or sharing else BTN)
    for line, ty in (("SHARE", cy - 32), ("ON" if sharing else "OFF", cy + 2)):
        d.text(((10 + MUTE_X1 - d.textlength(line, font=F_ARTIST)) / 2, ty), line, font=F_ARTIST, fill="white")
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
            d.rectangle((x0, cy + 25, x0 + seg_w - 3, cy + 31), fill=RED if i < lit else (45, 45, 55))


def meter_level(peak):
    """Peak amplitude -> 0..1 on a -48..0 dB scale (how loud it sounds, not raw amplitude)."""
    return 0.0 if peak <= 0 else max(0.0, min(1.0, 1 + 20 * math.log10(peak) / 48))


# --- media session polling (own thread + asyncio loop: WinRT calls are async) ---
class Now:
    title = artist = ""
    playing = False
    art = None
    session = None
    vol, muted = 0, False
    poll_now = False


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


async def poll_media():
    mgr = await SessionManager.request_async()
    art_key, art_until = None, 0  # keep re-reading the cover until art_until: apps send the title first
    while True:
        try:
            s = Now.session = pick_session(mgr)
            music_share.music_app = s.source_app_user_model_id if s else ""
            title = artist = ""
            playing = False
            if s:
                props = await s.try_get_media_properties_async()
                title, artist = props.title, props.artist
                playing = s.get_playback_info().playback_status == PlaybackStatus.PLAYING
                if (title, artist) != art_key:
                    art_key, Now.art, art_until = (title, artist), None, time.time() + 6
                if props.thumbnail and time.time() < art_until:
                    new = await read_thumb(props.thumbnail)
                    if new is not None and (Now.art is None or new.tobytes() != Now.art.tobytes()):
                        Now.art = new
            Now.title, Now.artist, Now.playing = title, artist, playing
            if not dragging:
                Now.vol, Now.muted = get_volume()  # picks up changes made in the Windows volume mixer
        except Exception as e:
            log(f"media poll failed: {e!r}")
        for _ in range(10):  # 0.5 s, or sooner after a button press
            if Now.poll_now:
                Now.poll_now = False
                break
            await asyncio.sleep(0.05)


media_loop = asyncio.new_event_loop()


def media_call(method):
    """Run a session method like try_skip_next_async on the media thread."""
    async def call():
        await method()
        Now.poll_now = True
    asyncio.run_coroutine_threadsafe(call(), media_loop)


# --- input: touch (other controller's tip) and laser both end up in press() / drag() ---
dragging = None        # "you" / "others" while a slider is held
pressed, pressed_until = None, 0
cfg = settings.load()  # panel position (saved when you let go of it), plus values editable in settings.json
share_pct = cfg["others_pct"]
music_share.share_gain = (share_pct / 100) ** CURVE


def slider_pct(x):
    return round(max(0, min(1, (x - SLIDER_X0) / (SLIDER_X1 - SLIDER_X0))) * 100)


def drag(x):
    global share_pct
    p = slider_pct(x)
    if dragging == "you" and p != Now.vol:
        Now.vol = p
        set_volume(p)
    elif dragging == "others" and p != share_pct:
        share_pct = p
        music_share.share_gain = (p / 100) ** CURVE


def press(x, y):
    """x, y in panel pixels, y from the top. Returns False on empty space (that starts a grab instead)."""
    global dragging, pressed, pressed_until
    if BTN_Y0 - 10 <= y <= BTN_Y1 + 5:
        if Now.session:
            zone = min(2, int(x) * 3 // W)
            s = Now.session
            media_call((s.try_skip_previous_async, s.try_toggle_play_pause_async, s.try_skip_next_async)[zone])
            pressed = zone
    elif y >= VOL_Y0 - 10:
        you_row = y < SHARE_Y0 - 7
        if MUTE_X1 + 10 < x < SLIDER_X0 - 25 or x > SLIDER_X1 + 25:  # gap or the "%" label
            return False
        if x <= MUTE_X1 + 10:
            if you_row:
                toggle_mute()
                Now.vol, Now.muted = get_volume()  # show what actually happened
                pressed = "mute"
            else:
                music_share.sharing = not music_share.sharing
                log(f"share {'ON' if music_share.sharing else 'OFF'}")
                pressed = "share"
        else:
            dragging = "you" if you_row else "others"
            drag(x)
    else:  # title / album art area
        return False
    pressed_until = time.time() + 0.25
    return True


# touch: the other controller's "fingertip" (cfg["poke_tip_m"] along its pointing direction, -z) must
# hover within POKE_ARM_M in front of the panel, then come within cfg["poke_press_m"]; it releases
# POKE_RELEASE_GAP_M past that. Sliding in from the side or passing through the arm doesn't press.
POKE_ARM_M = 0.08
POKE_RELEASE_GAP_M = 0.02
EDGE_PX = 12


def hand_roles():
    """(panel hand, touching hand) as OpenVR controller roles."""
    left, right = openvr.TrackedControllerRole_LeftHand, openvr.TrackedControllerRole_RightHand
    return (left, right) if cfg["hand"] == "left" else (right, left)


def default_pose():
    """Above the back of the wrist, tilted 50 degrees toward your face (relative to the controller)."""
    c, s = math.cos(math.radians(-50)), math.sin(math.radians(-50))
    return np.array([[1, 0, 0, 0], [0, c, -s, 0.03], [0, s, c, 0.12], [0, 0, 0, 1]])


def wrist_pose():
    """Panel pose relative to the wrist controller: where you last let go of it, else the default."""
    p = cfg.get("panel_pose")
    return np.vstack([np.array(p).reshape(3, 4), [0, 0, 0, 1]]) if p else default_pose()


def mat4(m):
    return np.vstack([np.array([[m.m[r][c] for c in range(4)] for r in range(3)]), [0, 0, 0, 1]])


def hmd34(a):
    m = openvr.HmdMatrix34_t()
    for r in range(3):
        for c in range(4):
            m.m[r][c] = float(a[r][c])
    return m


def device_poses():
    return openvr.VRSystem().getDeviceToAbsoluteTrackingPose(
        openvr.TrackingUniverseStanding, 0, openvr.k_unMaxTrackedDeviceCount)


class GrabButton:
    """Grip or trigger held, per controller, through SteamVR Input (input/actions.json). Overlay apps get
    no legacy button state, and actions don't take the buttons away from the game."""

    def __init__(self):
        inp = self.inp = openvr.VRInput()
        inp.setActionManifestPath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "input", "actions.json"))
        self.action = inp.getActionHandle("/actions/vrmc/in/grab")
        self.sets = (openvr.VRActiveActionSet_t * 1)()  # pyopenvr only accepts a ctypes array here
        self.sets[0].ulActionSet = inp.getActionSetHandle("/actions/vrmc")
        self.hands = {openvr.TrackedControllerRole_LeftHand: inp.getInputSourceHandle("/user/hand/left"),
                      openvr.TrackedControllerRole_RightHand: inp.getInputSourceHandle("/user/hand/right")}

    def update(self):
        self.inp.updateActionState(self.sets)

    def held(self, device):
        vrsys = openvr.VRSystem()
        for role, source in self.hands.items():
            if vrsys.getTrackedDeviceIndexForControllerRole(role) == device:
                d = self.inp.getDigitalActionData(self.action, source)
                return bool(d.bActive and d.bState)
        return False


def poke_point(ov, handle):
    """Other controller's tip in panel pixels: (x, y-from-top, distance_m), or None if off the panel."""
    s = openvr.VRSystem()
    dev, rel = ov.getOverlayTransformTrackedDeviceRelative(handle)
    dev = int(getattr(dev, "value", dev))
    other = s.getTrackedDeviceIndexForControllerRole(hand_roles()[1])
    if other == openvr.k_unTrackedDeviceIndexInvalid:
        return None
    poses = device_poses()
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


# --- drawing to SteamVR through an OpenGL texture (like Space Calibrator / OVR Advanced Settings):
# SetOverlayFromFile / SetOverlayRaw make SteamVR rebuild the texture and the panel blinks; updating
# our own GPU texture and handing it over with SetOverlayTexture doesn't.
class GLPanel:
    def __init__(self, ov, handle):
        if not glfw.init():
            raise RuntimeError("OpenGL (GLFW) could not start")
        glfw.window_hint(glfw.VISIBLE, glfw.FALSE)  # we only need a GL context, not a window
        self.win = glfw.create_window(16, 16, "VrMC", None, None)
        if not self.win:
            raise RuntimeError("could not create an OpenGL context")
        glfw.make_context_current(self.win)
        self.tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, W, H, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
        self.vr_tex = openvr.Texture_t()
        self.vr_tex.handle = ctypes.c_void_p(int(self.tex))
        self.vr_tex.eType = openvr.TextureType_OpenGL
        self.vr_tex.eColorSpace = openvr.ColorSpace_Auto
        self.ov, self.handle = ov, handle

    def show(self, img):
        # GL rows go bottom-up and SteamVR shows GL textures that way round: flip so the top stays on top
        data = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes()
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
        GL.glTexSubImage2D(GL.GL_TEXTURE_2D, 0, 0, 0, W, H, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
        GL.glFinish()
        self.ov.setOverlayTexture(self.handle, self.vr_tex)


CORNER_MASK = Image.new("L", (W, H), 0)
ImageDraw.Draw(CORNER_MASK).rounded_rectangle((0, 0, W, H), 24, fill=235)


def rounded(img):
    """Panel look in VR: slightly see-through with rounded corners."""
    out = img.convert("RGBA")
    out.putalpha(CORNER_MASK)
    return out


# --- SteamVR: start with SteamVR ---
def register_autostart():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        manifest = os.path.join(here, "vrmc.vrmanifest")
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump({"source": "builtin", "applications": [{
                "app_key": APP_KEY,
                "launch_type": "binary",
                "binary_path_windows": os.path.join(os.path.dirname(sys.executable), "pythonw.exe"),
                "arguments": f'"{os.path.join(here, "vrmc.py")}"',
                "is_dashboard_overlay": True,
                "strings": {"en_us": {"name": "VrMC", "description": "Music controls on your wrist"}},
            }]}, f, indent=2)
        apps = openvr.VRApplications()
        apps.addApplicationManifest(manifest, False)
        apps.setApplicationAutoLaunch(APP_KEY, True)
        if apps.isApplicationInstalled("wrist.media.yt"):  # the old wrist_media.py, now deleted
            apps.setApplicationAutoLaunch("wrist.media.yt", False)
    except Exception as e:
        log(f"autostart registration failed: {e!r}")


def main():
    global dragging, pressed
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mutex = k32.CreateMutexW(None, False, "Local\\VrMC")  # one copy only (SteamVR starts it too)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return
    openvr.init(openvr.VRApplication_Overlay)
    register_autostart()
    ov, vrsys = openvr.IVROverlay(), openvr.VRSystem()
    try:
        handle = ov.createOverlay(APP_KEY, "VrMC")
        ov.setOverlayWidthInMeters(handle, cfg["width_m"])
        ov.setOverlayInputMethod(handle, openvr.VROverlayInputMethod_Mouse)
        scale = openvr.HmdVector2_t()
        scale.v[0], scale.v[1] = W, H
        ov.setOverlayMouseScale(handle, scale)
        panel = GLPanel(ov, handle)
        ov.showOverlay(handle)
    except Exception as e:
        log(f"can't create the panel: {e!r}")
        openvr.shutdown()
        return
    try:
        buttons = GrabButton()
    except Exception as e:  # the panel still works, it just can't be grabbed by touch
        buttons = None
        log(f"SteamVR Input unavailable: {e!r}")

    music_share.mic_choice = cfg["mic"]
    music_share.start(log)
    threading.Thread(target=lambda: media_loop.run_until_complete(poll_media()), daemon=True).start()

    frame = base = shown_meters = attached_to = None
    m_you = m_others = 0.0
    shown_at, failures = 0, 0
    poking = armed = laser_down = False
    grab = None  # (controller index, panel pose relative to that controller) while you hold the panel
    held_pose = None
    ev = openvr.VREvent_t()

    def start_grab(ctrl):
        nonlocal grab, held_pose
        if attached_to is None or ctrl in (None, openvr.k_unTrackedDeviceIndexInvalid):
            return
        poses = device_poses()
        panel_now = mat4(poses[attached_to].mDeviceToAbsoluteTracking) @ wrist_pose()
        grab, held_pose = (ctrl, np.linalg.inv(mat4(poses[ctrl].mDeviceToAbsoluteTracking)) @ panel_now), panel_now

    def end_grab():
        nonlocal grab
        grab = None
        if held_pose is None or attached_to is None:
            return
        rel = np.linalg.inv(mat4(device_poses()[attached_to].mDeviceToAbsoluteTracking)) @ held_pose
        cfg["panel_pose"] = rel[:3].round(5).tolist()
        settings.save(cfg)
        ov.setOverlayTransformTrackedDeviceRelative(handle, attached_to, hmd34(rel))

    while True:
        try:
            if buttons:
                buttons.update()
            # SteamVR shutting down arrives on the system queue (not the overlay's)
            while vrsys.pollNextEvent(ev):
                if ev.eventType == openvr.VREvent_Quit:
                    vrsys.acknowledgeQuit_Exiting()
                    openvr.shutdown()
                    return

            # keep attached to the wrist (the controller may connect or get renumbered later)
            idx = vrsys.getTrackedDeviceIndexForControllerRole(hand_roles()[0])
            if idx != openvr.k_unTrackedDeviceIndexInvalid and idx != attached_to and not grab:
                ov.setOverlayTransformTrackedDeviceRelative(handle, idx, hmd34(wrist_pose()))
                attached_to = idx

            # laser (works while the SteamVR dashboard is open). With an OpenGL texture SteamVR reports
            # mouse y from the top; OpenVR-SpaceCalibrator uses it as-is too
            while ov.pollNextOverlayEvent(handle, ev)[0]:  # pyopenvr returns (ok, event)
                t, x, y = ev.eventType, ev.data.mouse.x, ev.data.mouse.y
                if t == openvr.VREvent_MouseButtonDown:
                    laser_down = True
                    if not press(x, y):
                        start_grab(ev.trackedDeviceIndex if ev.trackedDeviceIndex < 64
                                   else vrsys.getTrackedDeviceIndexForControllerRole(hand_roles()[1]))
                elif t == openvr.VREvent_MouseMove and dragging and laser_down:
                    drag(x)
                elif t in (openvr.VREvent_MouseButtonUp, openvr.VREvent_FocusLeave):
                    # FocusLeave: the laser left the panel; its button-up may never arrive here
                    laser_down, dragging = False, None
                    if grab and t == openvr.VREvent_MouseButtonUp:
                        end_grab()

            if grab:  # the panel follows the hand holding it until the trigger / grip is released
                ctrl, offset = grab
                pose = device_poses()[ctrl]
                if pose.bPoseIsValid:
                    held_pose = mat4(pose.mDeviceToAbsoluteTracking) @ offset
                    ov.setOverlayTransformAbsolute(handle, openvr.TrackingUniverseStanding, hmd34(held_pose))
                if not laser_down and not (buttons and buttons.held(ctrl)):
                    end_grab()
            else:
                # touch with the other controller's tip (works with the dashboard closed)
                try:
                    hit = poke_point(ov, handle)
                except Exception:
                    hit = None
                inside = hit and EDGE_PX < hit[0] < W - EDGE_PX and EDGE_PX < hit[1] < H - EDGE_PX
                press_m = cfg["poke_press_m"]
                was_armed, armed = armed, bool(inside and press_m < abs(hit[2]) < POKE_ARM_M)
                if inside and was_armed and not poking and abs(hit[2]) <= press_m:
                    poking = True
                    if not press(hit[0], hit[1]):  # empty space: grab if the trigger / grip is held
                        toucher = vrsys.getTrackedDeviceIndexForControllerRole(hand_roles()[1])
                        if buttons and buttons.held(toucher):
                            start_grab(toucher)
                elif poking and (not hit or abs(hit[2]) > press_m + POKE_RELEASE_GAP_M):
                    poking, dragging = False, None
                elif poking and dragging and hit:
                    drag(hit[0])

            # draw: panel re-rendered only when something on it changes, meters up to 15 fps
            if time.time() > pressed_until:
                pressed = None
            share_problem = music_share.problem()
            new_frame = (Now.title, Now.artist, Now.playing, id(Now.art), Now.vol, Now.muted,
                         music_share.sharing, share_pct, pressed, share_problem)
            if new_frame != frame:
                base = rounded(render(Now.title, Now.artist, Now.playing, Now.art, Now.vol, Now.muted,
                                      music_share.sharing, share_pct, pressed, share_problem))
                frame, shown_meters = new_frame, None
            m_you = max(meter_level(music_share.level_you), m_you - 0.03)
            m_others = max(meter_level(music_share.level_others), m_others - 0.03)
            meters = (round(m_you * METER_SEGMENTS), round(m_others * METER_SEGMENTS))
            if meters != shown_meters and (shown_meters is None or time.time() - shown_at > 1 / 15):
                img = base.copy()
                draw_meters(img, m_you, m_others)
                panel.show(img)
                shown_meters, shown_at = meters, time.time()
            failures = 0
        except Exception as e:
            failures += 1
            log(f"main loop error: {e!r}")
            if failures > 60:  # ~2 s of nothing but errors: SteamVR is gone
                log("giving up: SteamVR not responding")
                return
        time.sleep(1 / 30)


if __name__ == "__main__":
    main()
