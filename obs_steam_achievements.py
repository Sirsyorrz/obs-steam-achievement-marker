"""
obs_steam_achievements.py — OBS Python script
Watches Steam achievement unlocks for the running game and adds chapter markers.
Add via OBS → Tools → Scripts.
"""

import json
import os
import platform
import queue
import threading
import time
from pathlib import Path

import obspython as obs

# ── shared state (written by thread, read by main thread timer) ───────────────

_thread: threading.Thread | None = None
_stop_event = threading.Event()
_marker_queue: queue.Queue[str] = queue.Queue()
_status_queue: queue.Queue[str] = queue.Queue()
_settings = None
_status = "Not started"

POLL_INTERVAL = 2


# ── Steam helpers ─────────────────────────────────────────────────────────────

def find_steam_userdata() -> Path | None:
    system = platform.system()
    if system == "Linux":
        candidates = [
            Path.home() / ".local/share/Steam/userdata",
            Path.home() / ".steam/steam/userdata",
        ]
    elif system == "Windows":
        candidates = [
            Path("C:/Program Files (x86)/Steam/userdata"),
            Path("D:/Program Files (x86)/Steam/userdata"),
        ]
    elif system == "Darwin":
        candidates = [Path.home() / "Library/Application Support/Steam/userdata"]
    else:
        candidates = []
    return next((p for p in candidates if p.exists()), None)


def get_steam_ids(userdata: Path) -> list[str]:
    return [d.name for d in userdata.iterdir() if d.is_dir() and d.name.isdigit() and d.name != "0"]


def get_running_appid() -> str | None:
    system = platform.system()
    if system == "Linux":
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                data = (pid_dir / "environ").read_bytes().decode("utf-8", errors="replace")
                for entry in data.split("\x00"):
                    if entry.startswith("SteamAppId="):
                        appid = entry.split("=", 1)[1].strip()
                        if appid and appid != "0":
                            return appid
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
    elif system == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess")
            appid, _ = winreg.QueryValueEx(key, "ActiveGameId")
            winreg.CloseKey(key)
            return str(appid) if appid and appid != 0 else None
        except Exception:
            pass
    return None


def load_achievements(cache_file: Path) -> dict[str, dict]:
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    data = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, list) and len(item) == 2 and item[0] == "achievements":
                data = item[1]
                break
    elif isinstance(raw, dict):
        data = raw.get("achievements", raw)

    all_entries = []
    if isinstance(data, dict):
        inner = data.get("data", {})
        if isinstance(inner, dict):
            all_entries = inner.get("vecHighlight", []) + inner.get("vecUnachieved", [])
        elif isinstance(inner, list):
            all_entries = inner

    result = {}
    for ach in all_entries:
        if isinstance(ach, dict) and "strID" in ach:
            result[ach["strID"]] = {
                "name": ach.get("strName", ach["strID"]),
                "unlocked": bool(ach.get("bAchieved", False)),
            }
    return result


# ── watcher thread (no obs.* calls here) ─────────────────────────────────────

def find_cache_file(userdata: Path, steam_ids: list[str], appid: str) -> Path | None:
    for sid in steam_ids:
        p = userdata / sid / "config" / "librarycache" / f"{appid}.json"
        if p.exists():
            return p
    return None


def _watcher_thread(userdata: Path, steam_ids: list[str]):
    current_appid = None
    known_state: dict[str, dict] = {}

    _status_queue.put("Watching — no game detected")

    while not _stop_event.is_set():
        try:
            appid = get_running_appid()

            if appid != current_appid:
                current_appid = appid
                if appid:
                    cache_file = find_cache_file(userdata, steam_ids, appid)
                    if cache_file:
                        known_state = load_achievements(cache_file)
                        _status_queue.put(f"Watching — AppID {appid} ({len(known_state)} achievements loaded)")
                    else:
                        known_state = {}
                        _status_queue.put(f"Watching — AppID {appid} (no achievement cache yet)")
                else:
                    known_state = {}
                    _status_queue.put("Watching — no game detected")

            if current_appid:
                cache_file = find_cache_file(userdata, steam_ids, current_appid)
                if cache_file:
                    current_state = load_achievements(cache_file)
                    for ach_id, info in current_state.items():
                        if info["unlocked"] and not known_state.get(ach_id, {}).get("unlocked", False):
                            _marker_queue.put(info["name"])
                    known_state = current_state

        except Exception as e:
            _status_queue.put(f"Error: {e}")

        _stop_event.wait(POLL_INTERVAL)


# ── main thread timer — drains queues and calls OBS API ──────────────────────

def _tick():
    global _status

    # Drain status updates (keep only latest)
    latest = None
    while not _status_queue.empty():
        try:
            latest = _status_queue.get_nowait()
        except queue.Empty:
            break
    if latest is not None:
        _status = latest
        obs.script_log(obs.LOG_INFO, _status)
        if _settings:
            obs.obs_data_set_string(_settings, "status_display", _status)

    # Drain and fire markers
    while not _marker_queue.empty():
        try:
            name = _marker_queue.get_nowait()
            _add_marker(name)
        except queue.Empty:
            break


def _add_marker(name: str):
    prefix = obs.obs_data_get_string(_settings, "marker_prefix") if _settings else "🏆 "
    label = prefix + name
    ok = obs.obs_frontend_recording_add_chapter(label)
    obs.script_log(obs.LOG_INFO, ("Marker added: " if ok else "Marker failed (not recording?): ") + label)


# ── watcher lifecycle ─────────────────────────────────────────────────────────

def start_watcher():
    global _thread, _status

    userdata_override = obs.obs_data_get_string(_settings, "userdata_path") if _settings else ""
    userdata = Path(userdata_override) if userdata_override else find_steam_userdata()
    if not userdata or not userdata.exists():
        _status = "❌ Steam userdata not found — set path manually"
        obs.script_log(obs.LOG_WARNING, _status)
        return

    steam_ids = get_steam_ids(userdata)
    if not steam_ids:
        _status = "❌ No Steam accounts found in userdata"
        obs.script_log(obs.LOG_WARNING, _status)
        return

    stop_watcher()
    _stop_event.clear()
    _thread = threading.Thread(target=_watcher_thread, args=(userdata, steam_ids), daemon=True)
    _thread.start()
    obs.script_log(obs.LOG_INFO, f"Watcher started (userdata={userdata}, accounts={steam_ids})")


def stop_watcher():
    global _thread
    if _thread and _thread.is_alive():
        _stop_event.set()
        _thread.join(timeout=5)
    _thread = None


# ── OBS script API ────────────────────────────────────────────────────────────

def script_description():
    return (
        "<h3>Steam Achievement Markers</h3>"
        "Automatically adds chapter markers when Steam achievements are unlocked.<br><br>"
        "Requires OBS 30.2+."
    )


def script_defaults(settings):
    p = find_steam_userdata()
    obs.obs_data_set_default_string(settings, "userdata_path", str(p) if p else "")
    obs.obs_data_set_default_string(settings, "marker_prefix", "🏆 ")
    obs.obs_data_set_default_string(settings, "status_display", "Not started")


def script_properties():
    props = obs.obs_properties_create()

    obs.obs_properties_add_text(props, "status_display", "Watcher status", obs.OBS_TEXT_DEFAULT)

    obs.obs_properties_add_text(props, "marker_prefix", "Marker prefix", obs.OBS_TEXT_DEFAULT)

    obs.obs_properties_add_path(props, "userdata_path", "Steam userdata path",
        obs.OBS_PATH_DIRECTORY, None, None)

    obs.obs_properties_add_button(props, "btn_restart", "Restart watcher", lambda p, s: (
        start_watcher(), False)[-1])

    obs.obs_properties_add_button(props, "btn_test", "Test marker", lambda p, s: (
        _add_marker("Test Achievement"), False)[-1])

    return props


def script_update(settings):
    global _settings
    _settings = settings


def script_load(settings):
    global _settings
    _settings = settings
    obs.timer_add(_tick, 500)
    start_watcher()


def script_unload():
    obs.timer_remove(_tick)
    stop_watcher()
