#!/usr/bin/env python3
"""Audio control for i3: media keys, device picker, and status for the bar.

Usage: audio.py up|down|mute|mic-mute|pick|play-pause|next|previous|stop

Volume goes through `pactl`, so it works on both PipeWire (pipewire-pulse) and
plain PulseAudio. Volume is capped at MAX_VOLUME: anything above 100% is
digital gain that only adds clipping. Media keys talk MPRIS over `gdbus`, so
no playerctl is needed.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import ui  # noqa: E402

STEP = 5         # percent per key press / scroll notch
MAX_VOLUME = 100  # percent

# Short names for the bar, matched case-insensitively against the device's
# node name; first match wins. Unmatched devices use their description.
ALIASES = {
    "C-Media": "USB Audio",
    "bluez": "Bluetooth",
    "hdmi": "HDMI",
    "EMEET": "Webcam",
}

_C_ENV = {**os.environ, "LC_ALL": "C"}  # stable `pactl list` field names


def _pactl(*args):
    return ui.run("pactl", *args, env=_C_ENV).strip()


def _nodes(kind):
    """Parse `pactl list sinks|sources` into dicts (name, description, volume, muted, monitor)."""
    nodes = []
    for chunk in _pactl("list", kind).split("\n\n"):
        name = re.search(r"^\s*Name: (.+)$", chunk, re.M)
        if not name:
            continue
        desc = re.search(r"^\s*Description: (.+)$", chunk, re.M)
        mute = re.search(r"^\s*Mute: (\w+)$", chunk, re.M)
        vol_line = re.search(r"^\s*Volume: (.+)$", chunk, re.M)
        percents = [int(p) for p in re.findall(r"(\d+)%", vol_line.group(1))] if vol_line else []
        nodes.append({
            "name": name.group(1),
            "description": desc.group(1) if desc else name.group(1),
            "muted": bool(mute and mute.group(1) == "yes"),
            "volume": round(sum(percents) / len(percents)) if percents else 0,
            "monitor": 'device.class = "monitor"' in chunk or name.group(1).endswith(".monitor"),
        })
    return nodes


def _default_name(kind):
    return _pactl("get-default-sink" if kind == "sinks" else "get-default-source")


def label(node):
    lname = node["name"].lower()
    for key, alias in ALIASES.items():
        if key.lower() in lname:
            return alias
    return node["description"]


def status(kind):
    """Default sink/source as a dict (plus 'label'), or None if there is none."""
    default = _default_name(kind)
    for node in _nodes(kind):
        if node["name"] == default:
            return {**node, "label": label(node)}
    return None


def volume_icon(node):
    if node["muted"]:
        return ui.icon("vol_mute")
    if node["volume"] < 34:
        return ui.icon("vol_low")
    return ui.icon("vol_mid") if node["volume"] < 67 else ui.icon("vol_high")


def change(action, notify=True):
    """Apply a volume/mute action to the default devices."""
    if action in ("up", "down"):
        sink = status("sinks")
        if sink is None:
            return
        delta = STEP if action == "up" else -STEP
        # Rounding to the step grid also pulls an over-boosted sink (e.g. 290%)
        # straight back to the cap on the first press.
        target = max(0, min(MAX_VOLUME, (sink["volume"] + delta) // STEP * STEP))
        _pactl("set-sink-volume", "@DEFAULT_SINK@", f"{target}%")
        if action == "up" and sink["muted"]:
            _pactl("set-sink-mute", "@DEFAULT_SINK@", "0")
    elif action == "mute":
        _pactl("set-sink-mute", "@DEFAULT_SINK@", "toggle")
    elif action == "mic-mute":
        _pactl("set-source-mute", "@DEFAULT_SOURCE@", "toggle")
    else:
        raise ValueError(action)
    if notify:
        _notify(action)


def _notify(action):
    if action == "mic-mute":
        src = status("sources")
        if src:
            muted = src["muted"]
            ui.notify(f"{ui.icon('mic_mute' if muted else 'mic')}  Mic {'muted' if muted else 'on'}",
                      src["label"], tag="mic")
        return
    sink = status("sinks")
    if sink:
        text = "Muted" if sink["muted"] else f"{sink['volume']}%"
        ui.notify(f"{volume_icon(sink)}  {text}", sink["label"], tag="volume",
                  value=0 if sink["muted"] else sink["volume"])


def _set_default(kind, name):
    """Make `name` the default device and move every running stream onto it."""
    if kind == "sinks":
        _pactl("set-default-sink", name)
        streams, move = "sink-inputs", "move-sink-input"
    else:
        _pactl("set-default-source", name)
        streams, move = "source-outputs", "move-source-output"
    for line in _pactl("list", "short", streams).splitlines():
        stream_id = line.split("\t", 1)[0]
        if stream_id.isdigit():
            _pactl(move, stream_id, name)


def pick():
    """Menu of outputs and inputs; the chosen one becomes the default."""
    entries = {}
    for kind, icon_name, title in (("sinks", "speaker", "Output"), ("sources", "mic", "Input")):
        default = _default_name(kind)
        for node in _nodes(kind):
            if node["monitor"]:
                continue
            mark = ui.icon("selected") if node["name"] == default else " "
            line = f"{mark} {ui.icon(icon_name)}  {title}: {node['description']}"
            entries[line] = (kind, node)
    mixer = f"  {ui.icon('settings')}  Open mixer (pavucontrol)"
    lines = list(entries) + ([mixer] if ui.have("pavucontrol") else [])
    if not entries:
        ui.notify("No audio devices found", "Is pipewire-pulse or pulseaudio running?")
        return
    choice = ui.menu("Audio", lines)
    if choice == mixer:
        ui.spawn("pavucontrol")
    elif choice in entries:
        kind, node = entries[choice]
        _set_default(kind, node["name"])
        ui.notify(f"{ui.icon('speaker' if kind == 'sinks' else 'mic')}  {node['description']}",
                  f"Default {'output' if kind == 'sinks' else 'input'}", tag="audio-device")


# ---- media keys (MPRIS over D-Bus) ---------------------------------------------

_MPRIS = "org.mpris.MediaPlayer2."


def _gdbus(dest, path, method, *args):
    return ui.run("gdbus", "call", "--session", "--dest", dest, "--object-path", path,
                  "--method", method, *args, timeout=2)


def _players():
    out = _gdbus("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames")
    return re.findall(r"'(" + re.escape(_MPRIS) + r"[^']+)'", out)


def _pick_player():
    """Prefer a player that is currently playing, then one that is paused, then any."""
    ranked = {"Playing": 0, "Paused": 1}
    best, best_rank = None, 3
    for player in _players():
        out = _gdbus(player, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties.Get",
                     _MPRIS + "Player", "PlaybackStatus")
        m = re.search(r"'(\w+)'", out)
        rank = ranked.get(m.group(1), 2) if m else 2
        if rank < best_rank:
            best, best_rank = player, rank
    return best


def media(action):
    method = {"play-pause": "PlayPause", "next": "Next", "previous": "Previous", "stop": "Stop"}[action]
    player = _pick_player()
    if player:
        _gdbus(player, "/org/mpris/MediaPlayer2", f"{_MPRIS}Player.{method}")


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    action = argv[1]
    if action in ("up", "down", "mute", "mic-mute"):
        change(action)
    elif action == "pick":
        pick()
    elif action in ("play-pause", "next", "previous", "stop"):
        media(action)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
