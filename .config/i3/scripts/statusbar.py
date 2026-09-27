#!/usr/bin/env python3
"""i3bar status line: cpu, memory, bluetooth, network, mic, volume, clock.

Speaks the i3bar JSON protocol with click events. Stdlib only; every block
hides itself (or degrades) when its backing tool/device is missing.

Clicks:
  volume     left: pick device   middle: mute   right: mixer   scroll: volume
  mic        left: mute          right: mixer (inputs)
  network    left: Wi-Fi menu    right: connection editor
  bluetooth  left: manager       right: power on/off
  cpu / mem  left: process viewer
"""
import glob
import html
import json
import os
import subprocess
import sys
import threading
import time

SCRIPTS = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPTS)
import audio  # noqa: E402
import network  # noqa: E402
import ui  # noqa: E402

TICK = 2  # seconds between redraws when nothing happens


# ---- rendering -------------------------------------------------------------------

def seg(text, color=None, is_icon=False):
    attrs = []
    if color:
        attrs.append(f"foreground='{color}'")
    if is_icon and ui.NERD_FONT:
        attrs.append(f"font_family='{ui.ICON_FONT}'")
    text = html.escape(text)
    return f"<span {' '.join(attrs)}>{text}</span>" if attrs else text


def ico(name, color=ui.ACCENT):
    return seg(ui.icon(name), color, is_icon=True)


def block(name, *parts):
    return {
        "name": name,
        "full_text": " ".join(p for p in parts if p),
        "markup": "pango",
        "separator": False,
        "separator_block_width": 18,
    }


def shorten(text, limit=22):
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---- blocks ----------------------------------------------------------------------

_cpu_prev = None


def cpu_block():
    global _cpu_prev
    with open("/proc/stat") as f:
        fields = [int(x) for x in f.readline().split()[1:]]
    idle, total = fields[3] + fields[4], sum(fields)
    prev, _cpu_prev = _cpu_prev, (idle, total)
    if prev is None or total == prev[1]:
        return block("cpu", ico("cpu"), seg(" 0%"))
    pct = round(100 * (1 - (idle - prev[0]) / (total - prev[1])))
    color = ui.RED if pct >= 90 else ui.YELLOW if pct >= 70 else None
    return block("cpu", ico("cpu"), seg(f"{pct:2d}%", color))


def mem_block():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0])  # kB
    total, avail = info["MemTotal"], info.get("MemAvailable", info["MemFree"])
    ratio = 1 - avail / total
    color = ui.RED if ratio >= 0.9 else ui.YELLOW if ratio >= 0.8 else None
    return block("mem", ico("mem"), seg(f"{(total - avail) / 1048576:.1f}G", color))


def bluetooth_block():
    # No adapter -> no block, and never call bluetoothctl (it hangs without bluetoothd).
    if not glob.glob("/sys/class/bluetooth/hci*") or not ui.have("bluetoothctl"):
        return None
    if "Powered: yes" not in ui.run("bluetoothctl", "show", timeout=2):
        return block("bluetooth", ico("bt_off", ui.DIM))
    names = [line.split(" ", 2)[2] for line in ui.run("bluetoothctl", "devices", "Connected", timeout=2).splitlines()
             if line.startswith("Device ") and line.count(" ") >= 2]
    if not names:
        return block("bluetooth", ico("bt_on", ui.DIM))
    return block("bluetooth", ico("bt_connected"), seg(shorten(", ".join(names))))


def network_block():
    st = network.status()
    if st is None:
        return block("network", ico("wifi_off", ui.RED), seg("offline", ui.DIM))
    if st["kind"] == "ethernet":
        return block("network", ico("ethernet"), seg(shorten(st["name"])))
    return block("network", seg(network.wifi_icon(st["signal"]), ui.ACCENT, is_icon=True), seg(shorten(st["name"])))


def mic_block():
    src = audio.status("sources")
    if src is None:
        return None
    return block("mic", ico("mic_mute", ui.RED) if src["muted"] else ico("mic", ui.DIM))


def volume_block():
    sink = audio.status("sinks")
    if sink is None:
        return block("volume", ico("vol_mute", ui.RED), seg("no output", ui.DIM))
    label = seg(shorten(sink["label"], 18), ui.DIM)
    if sink["muted"]:
        return block("volume", seg(audio.volume_icon(sink), ui.DIM, is_icon=True), seg("muted", ui.DIM), label)
    return block("volume", seg(audio.volume_icon(sink), ui.ACCENT, is_icon=True), seg(f"{sink['volume']}%"), label)


def clock_block():
    return block("clock", seg(time.strftime("%a %d %b"), ui.DIM), seg(time.strftime("%H:%M")))


class Cached:
    """Recompute a block at most every `ttl` seconds unless invalidated."""

    def __init__(self, fn, ttl):
        self.fn, self.ttl, self.expires, self.value = fn, ttl, 0.0, None
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            now = time.monotonic()
            if now >= self.expires:
                try:
                    self.value = self.fn()
                except Exception as exc:  # one broken block must not kill the bar
                    print(f"statusbar: {self.fn.__name__}: {exc!r}", file=sys.stderr)
                    self.value = None
                self.expires = now + self.ttl
            return self.value

    def invalidate(self):
        self.expires = 0.0


BLOCKS = {
    "cpu": Cached(cpu_block, TICK),
    "mem": Cached(mem_block, TICK),
    "bluetooth": Cached(bluetooth_block, 10),
    "network": Cached(network_block, 5),
    "mic": Cached(mic_block, 30),        # also refreshed on pactl events
    "volume": Cached(volume_block, 30),  # also refreshed on pactl events
    "clock": Cached(clock_block, 1),
}


# ---- events ----------------------------------------------------------------------

def _process_viewer():
    for top in ("btop", "htop", "top"):
        if ui.have(top):
            ui.open_in_terminal(top)
            return


def _bluetooth_manager():
    if ui.have("bluetui"):
        ui.open_in_terminal("bluetui")
    elif not ui.spawn_first(["blueman-manager"]):
        ui.open_in_terminal("bluetoothctl")


def on_click(name, button):
    script = lambda s, *a: ui.spawn(sys.executable, os.path.join(SCRIPTS, s), *a)  # noqa: E731
    mixer = lambda tab: ui.spawn_first(["pavucontrol", f"--tab={tab}"])  # noqa: E731
    if name == "volume":
        if button == 1:
            script("audio.py", "pick")
        elif button == 2:
            audio.change("mute", notify=False)
        elif button == 3:
            mixer(3)
        elif button in (4, 5):
            audio.change("up" if button == 4 else "down", notify=False)
    elif name == "mic":
        if button == 1:
            audio.change("mic-mute", notify=False)
        elif button == 3:
            mixer(4)
    elif name == "network":
        if button == 1:
            script("network.py", "pick")
        elif button == 3:
            ui.spawn_first(["nm-connection-editor"])
    elif name == "bluetooth":
        if button == 1:
            _bluetooth_manager()
        elif button == 3:
            on = "Powered: yes" in ui.run("bluetoothctl", "show", timeout=2)
            ui.run("bluetoothctl", "power", "off" if on else "on", timeout=5)
    elif name in ("cpu", "mem") and button == 1:
        _process_viewer()
    if name in BLOCKS:
        BLOCKS[name].invalidate()


def read_clicks(wake):
    for line in sys.stdin:
        line = line.strip().lstrip(",")
        if not line or line == "[":
            continue
        try:
            event = json.loads(line)
            on_click(event.get("name"), event.get("button"))
        except Exception as exc:
            print(f"statusbar: click: {exc!r}", file=sys.stderr)
        wake.set()


def watch_audio(wake):
    """Redraw audio blocks the moment volume/mute/default device changes."""
    while True:
        if ui.have("pactl"):
            try:
                proc = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, env=audio._C_ENV)
                for line in proc.stdout:
                    if any(k in line for k in (" on sink #", " on source #", " on server", " on card #")):
                        BLOCKS["volume"].invalidate()
                        BLOCKS["mic"].invalidate()
                        wake.set()
                proc.wait()
            except OSError:
                pass
        time.sleep(5)  # sound server restarting or absent; retry


def main():
    wake = threading.Event()
    threading.Thread(target=read_clicks, args=(wake,), daemon=True).start()
    threading.Thread(target=watch_audio, args=(wake,), daemon=True).start()
    out = sys.stdout
    try:
        out.write(json.dumps({"version": 1, "click_events": True}) + "\n[\n")
        while True:
            blocks = [b for b in (get() for get in BLOCKS.values()) if b]
            out.write(json.dumps(blocks, ensure_ascii=False) + ",\n")
            out.flush()
            if wake.wait(TICK):
                time.sleep(0.03)  # coalesce bursts of events into one redraw
            wake.clear()
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
