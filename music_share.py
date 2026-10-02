"""Let people in VRChat hear your music: mixes your mic + the music app into a virtual mic.

  your mic ─────────────┐
                        ├─> "Speakers (Steam Streaming Microphone)"
  music app (a copy) ───┘     (music only while sharing is on, at share_gain)

In VRChat pick "Microphone (Steam Streaming Microphone)". You still hear the music
normally: it is copied with Windows process loopback, not rerouted.
"""
import ctypes
import ctypes.wintypes as wt
import re
import threading
import time

import numpy as np
import psutil
import comtypes  # before sounddevice: the first library to touch COM picks the thread mode (we need MTA)
from comtypes import COMMETHOD, GUID, HRESULT, COMObject, IUnknown
from pycaw.api.audioclient import IAudioClient
from pycaw.api.audioclient.depend import WAVEFORMATEX
from pycaw.pycaw import AudioUtilities, EDataFlow, ERole, IMMDeviceEnumerator
from pycaw.constants import CLSID_MMDeviceEnumerator
import sounddevice as sd

share_gain = 0.3            # music level for others (1.0 = the song at full volume); set by the panel
you_amp = 1.0               # the music app's own volume (what "you hear" is set to); set by the panel
VIRTUAL_MIC_OUT = "Speakers (Steam Streaming Microphone)"
RATE = 48000
MAX_LAG_S = 0.15            # drop old audio beyond this so the mic doesn't drift behind

sharing = False             # flipped by the SHARE button
music_app = ""              # media session app id, e.g. "firefox.exe"; set by the overlay
mic_choice = ""             # "" = Windows default mic; set from settings
level_you = level_others = 0.0   # peak levels for the panel's meters
mic_open = False            # your mic + the virtual mic are running
capture_state = "no app"    # music capture: "ok", "no app" (music app not found) or "error"

BROWSERS = ("firefox", "chrome", "msedge", "brave", "librewolf", "opera", "vivaldi")
_ID_NOISE = {"com", "github", "microsoft", "windows", "desktop", "app", "exe"}


def _squash(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def is_music_process(proc_name):
    """Does this process belong to the media session's app (music_app)? Used for both the volume slider
    and SHARE. Session ids look like 'firefox.exe', 'Spotify.exe', 'Chrome', 'MSEdge', or for Pear Desktop
    (YouTube Music.exe) 'com.github.th-ch.youtube-music'."""
    app, name = music_app.lower(), proc_name.lower().removesuffix(".exe")
    if not app or not name:
        return False
    if app.endswith(".exe"):
        return name == app.removesuffix(".exe")
    if _squash(name) in _squash(app):  # "chrome" in "chrome", "youtubemusic" in "comgithubthchyoutubemusic"
        return True
    words = [w for w in re.split(r"[^a-z0-9]+", app) if len(w) >= 4 and w not in _ID_NOISE]
    if any(w in _squash(name) for w in words):
        return True
    # some browsers report an opaque id (a hex hash): then it's the browser
    return re.fullmatch(r"[0-9a-f]{16}", app) is not None and name in BROWSERS


def problem():
    """Why sharing can't work right now, short enough for the panel; None when it works."""
    if not mic_open:
        return "no mic"
    if capture_state == "error":
        return "can't capture music"
    if capture_state == "no app":
        return "music app not found"
    return None


# --- Windows process loopback (capture one app's audio) ---
class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [COMMETHOD([], HRESULT, "GetActivateResult",
                           (["out"], ctypes.POINTER(ctypes.c_long), "activateResult"),
                           (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)), "activatedInterface"))]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [COMMETHOD([], HRESULT, "ActivateCompleted",
                           (["in"], ctypes.POINTER(IActivateAudioInterfaceAsyncOperation), "op"))]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = []


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "data"),
                  (["out"], ctypes.POINTER(ctypes.c_uint32), "frames"),
                  (["out"], ctypes.POINTER(wt.DWORD), "flags"),
                  (["out"], ctypes.POINTER(ctypes.c_uint64), "devpos"),
                  (["out"], ctypes.POINTER(ctypes.c_uint64), "qpcpos")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], ctypes.c_uint32, "frames")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], ctypes.POINTER(ctypes.c_uint32), "frames")),
    ]


class _Done(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.event = threading.Event()

    def ActivateCompleted(self, op):
        self.event.set()


class _Blob(ctypes.Structure):
    _fields_ = [("cbSize", wt.ULONG), ("pBlobData", ctypes.c_void_p)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("blob", _Blob)]


class _LoopbackParams(ctypes.Structure):  # AUDIOCLIENT_ACTIVATION_PARAMS
    _fields_ = [("ActivationType", ctypes.c_int), ("TargetProcessId", wt.DWORD), ("Mode", ctypes.c_int)]


_activate = ctypes.WinDLL("Mmdevapi.dll").ActivateAudioInterfaceAsync
_activate.restype = HRESULT
_activate.argtypes = [wt.LPCWSTR, ctypes.POINTER(GUID), ctypes.c_void_p,
                      ctypes.POINTER(IActivateAudioInterfaceCompletionHandler),
                      ctypes.POINTER(ctypes.POINTER(IActivateAudioInterfaceAsyncOperation))]


def _open_loopback(pid):
    params = _LoopbackParams(1, pid, 0)  # PROCESS_LOOPBACK, include the app's child processes
    pv = _PropVariant(vt=65, blob=_Blob(ctypes.sizeof(params), ctypes.addressof(params)))  # VT_BLOB
    done = _Done()
    op = ctypes.POINTER(IActivateAudioInterfaceAsyncOperation)()
    _activate("VAD\\Process_Loopback", ctypes.byref(IAudioClient._iid_), ctypes.byref(pv),
              done.QueryInterface(IActivateAudioInterfaceCompletionHandler), ctypes.byref(op))
    if not done.event.wait(5):
        raise TimeoutError("process loopback activation timed out")
    hr, unk = op.GetActivateResult()
    if hr < 0:
        raise OSError(f"process loopback failed: 0x{hr & 0xFFFFFFFF:08X}")
    client = unk.QueryInterface(IAudioClient)
    fmt = WAVEFORMATEX(3, 2, RATE, RATE * 8, 8, 32, 0)  # 32-bit float stereo: no hiss when un-doing a low volume
    # LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM, 100 ms buffer
    client.Initialize(0, 0x00020000 | 0x00040000 | 0x80000000, 1_000_000, 0, ctypes.byref(fmt), None)
    event = ctypes.windll.kernel32.CreateEventW(None, False, False, None)
    client.SetEventHandle(event)
    capture = client.GetService(ctypes.byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
    client.Start()
    return client, capture, event


# --- tiny ring buffer between devices with separate clocks ---
class _Ring:
    def __init__(self, channels):
        self.buf = np.zeros((0, channels), np.float32)
        self.lock = threading.Lock()

    def write(self, frames):
        with self.lock:
            self.buf = np.concatenate([self.buf, frames])[-int(RATE * MAX_LAG_S):]

    def read(self, n):
        with self.lock:
            out, self.buf = self.buf[:n], self.buf[n:]
        if len(out) < n:
            out = np.concatenate([out, np.zeros((n - len(out), out.shape[1]), np.float32)])
        return out


_music, _mic = _Ring(2), _Ring(1)
_streams = []  # keep references so the streams aren't garbage-collected


def _music_pid():
    for p in psutil.process_iter(["name", "ppid"]):
        name = (p.info["name"] or "").lower()
        if is_music_process(name):
            try:
                parent = psutil.Process(p.info["ppid"]).name().lower()
            except psutil.Error:
                parent = ""
            if parent != name:  # the app's root process (covers its audio child process)
                return p.pid
    return None


def _music_thread():
    global capture_state, level_you
    comtypes.CoInitializeEx(0)  # MTA
    pid = client = None
    checked = 0
    while True:
        try:
            if time.time() - checked > 2:  # finding the app scans all processes: not every packet
                checked = time.time()
                want = _music_pid()
            if want != pid:
                if client:
                    client.Stop()
                client, pid = None, want
                capture_state = "no app"
                if pid:
                    client, capture, event = _open_loopback(pid)
                    capture_state = "ok"
            if not client:
                time.sleep(1)
                continue
            ctypes.windll.kernel32.WaitForSingleObject(event, 200)
            while capture.GetNextPacketSize():
                data, n, flags, _, _ = capture.GetBuffer()
                if flags & 2 or not data:  # AUDCLNT_BUFFERFLAGS_SILENT
                    frames = np.zeros((n, 2), np.float32)
                else:
                    frames = np.ctypeslib.as_array(ctypes.cast(data, ctypes.POINTER(ctypes.c_float)), (n, 2)).copy()
                capture.ReleaseBuffer(n)
                level_you = float(np.abs(frames).max()) if n else 0.0
                # Windows copies the app's audio *after* its volume: undo it so "others hear" doesn't
                # depend on "you hear" (capped at +40 dB; at 0% / muted there is nothing to recover)
                _music.write(frames / max(you_amp, 0.01))
        except Exception as e:
            if capture_state != "error":
                _log(f"music share: can't capture music: {e!r}")
            capture_state = "error"
            client = pid = want = None
            time.sleep(2)


def _wasapi_device(name, kind):
    api = next(i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "Windows WASAPI")
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] == api and d["name"] == name and d[f"max_{kind}_channels"] > 0:
            return i
    raise LookupError(f"audio device not found: {name}")


def _resolve_mic():
    """The mic to use: the one picked in settings if it's plugged in, else the Windows default."""
    if mic_choice:
        try:
            _wasapi_device(mic_choice, "input")
            return mic_choice
        except LookupError:
            pass
    e = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    mic = AudioUtilities.CreateDevice(
        e.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, ERole.eConsole.value)).FriendlyName
    if "Steam Streaming" in mic or "CABLE" in mic:  # would feed the virtual mic into itself
        raise RuntimeError(f"default mic is a virtual device ({mic}); pick your real mic in settings")
    return mic


_beat = {"mic": 0.0, "virtual mic": 0.0}  # last callback time per stream (catches frozen streams)


def _mic_in(indata, frames, t, s):
    _beat["mic"] = time.time()
    _mic.write(indata[:, :1].copy())


def _mix_out(outdata, frames, t, s):
    global level_others
    _beat["virtual mic"] = time.time()
    music = _music.read(frames) * (share_gain if sharing else 0)
    level_others = float(np.abs(music).max())
    np.clip(_mic.read(frames) + music, -1, 1, out=outdata)


def _open_streams(mic):
    wasapi = sd.WasapiSettings(auto_convert=True)
    _streams[:] = [
        sd.InputStream(device=_wasapi_device(mic, "input"), samplerate=RATE, channels=1, dtype="float32",
                       latency="low", callback=_mic_in, extra_settings=wasapi),
        sd.OutputStream(device=_wasapi_device(VIRTUAL_MIC_OUT, "output"), samplerate=RATE, channels=2,
                        dtype="float32", latency="low", callback=_mix_out, extra_settings=wasapi),
    ]
    for s in _streams:
        s.start()
    now = time.time()
    _beat.update({"mic": now, "virtual mic": now})


def _watchdog(log):
    """Keeps the mix running: reopens when a stream dies or freezes (Windows reconfiguring a device,
    e.g. VRChat opening the virtual mic) or when the mic to use changes (settings, new default).
    Retries back off up to 30 s and only state changes are logged, so a missing mic can't flood the log."""
    global mic_open
    mic, wait, last_msg = None, 1, None
    while True:
        time.sleep(wait)
        try:
            want = _resolve_mic()
        except Exception as e:
            want, msg = None, f"music share: no usable mic: {e}"
        else:
            dead = [n for n, s in zip(("mic", "virtual mic"), _streams)
                    if not s.active or time.time() - _beat[n] > 1.0]
            if want == mic and _streams and not dead:
                wait = 1
                continue
            msg = (f"music share: {', '.join(dead)} stream stopped; reopening" if want == mic and dead
                   else f"music share: using mic {want}")
        for s in _streams:
            s.close()
        _streams.clear()
        mic = None
        if want:
            try:
                sd._terminate()   # refresh PortAudio's device list: indices can change after a reconfigure
                sd._initialize()
                _open_streams(want)
                mic, wait = want, 1
            except Exception as e:
                msg = f"music share: can't open audio ({want}): {e!r}"
        if not mic:
            wait = min(wait * 2, 30)
        mic_open = bool(mic)
        if msg != last_msg:
            log(msg)
            last_msg = msg


_log = print


def start(log=print):
    """Start the mic + music mix in the background and keep it running."""
    global _log
    _log = log
    threading.Thread(target=_watchdog, args=(log,), daemon=True).start()
    threading.Thread(target=_music_thread, daemon=True).start()
