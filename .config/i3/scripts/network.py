#!/usr/bin/env python3
"""Network status for the bar and a Wi-Fi picker.

Usage: network.py pick

Uses NetworkManager (`nmcli`). Password prompts for new networks come from
NetworkManager's secret agent (nm-applet, started in the i3 config).
Without nmcli, status falls back to sysfs and the picker is unavailable.
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import ui  # noqa: E402


def _terse(line):
    """Split one `nmcli -t` line on unescaped colons."""
    return [f.replace("\\:", ":").replace("\\\\", "\\") for f in re.split(r"(?<!\\):", line)]


def wifi_icon(signal):
    if signal is None:
        return ui.icon("wifi_4")
    return ui.icon(f"wifi_{min(4, max(0, signal // 20))}")


def _proc_signal(dev):
    """Link quality from /proc/net/wireless as 0-100, or None."""
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                if line.strip().startswith(dev + ":"):
                    return min(100, round(float(line.split()[2].rstrip(".")) * 100 / 70))
    except (OSError, ValueError, IndexError):
        pass
    return None


def status():
    """{'kind': 'ethernet'|'wifi', 'name': str, 'signal': int|None} or None when offline."""
    if ui.have("nmcli"):
        wifi = None
        for line in ui.run("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device").splitlines():
            fields = _terse(line)
            if len(fields) != 4 or fields[2] != "connected":
                continue
            dev, kind, _, conn = fields
            if kind == "ethernet":
                return {"kind": "ethernet", "name": conn, "signal": None}
            if kind == "wifi" and wifi is None:
                wifi = {"kind": "wifi", "name": conn, "signal": _proc_signal(dev)}
        return wifi
    # Fallback without NetworkManager: any interface that is up.
    for path in sorted(glob.glob("/sys/class/net/*")):
        dev = os.path.basename(path)
        if dev == "lo" or not os.path.exists(os.path.join(path, "device")):
            continue
        try:
            with open(os.path.join(path, "operstate")) as f:
                if f.read().strip() != "up":
                    continue
        except OSError:
            continue
        if os.path.isdir(os.path.join(path, "wireless")):
            ssid = ui.run("iwgetid", "-r").strip() if ui.have("iwgetid") else ""
            return {"kind": "wifi", "name": ssid or dev, "signal": _proc_signal(dev)}
        return {"kind": "ethernet", "name": dev, "signal": None}
    return None


def _wifi_device():
    for line in ui.run("nmcli", "-t", "-f", "DEVICE,TYPE", "device").splitlines():
        fields = _terse(line)
        if len(fields) == 2 and fields[1] == "wifi":
            return fields[0]
    return None


def _networks():
    """Visible networks, strongest first, deduplicated by SSID: [(ssid, signal, secure, in_use)]."""
    out = ui.run("nmcli", "-t", "-f", "IN-USE,SIGNAL,SECURITY,SSID", "device", "wifi", "list", timeout=20)
    best = {}
    for line in out.splitlines():
        fields = _terse(line)
        if len(fields) != 4 or not fields[3]:
            continue
        in_use, signal, security, ssid = fields
        signal = int(signal) if signal.isdigit() else 0
        secure, in_use = security not in ("", "--"), in_use == "*"
        prev = best.get(ssid)
        if prev:  # same SSID on several access points / bands
            signal, secure, in_use = max(signal, prev[1]), secure or prev[2], in_use or prev[3]
        best[ssid] = (ssid, signal, secure, in_use)
    return sorted(best.values(), key=lambda n: (not n[3], -n[1]))


def _connect(ssid):
    ui.notify(f"{ui.icon('wifi_2')}  Connecting…", ssid, tag="wifi", timeout_ms=5000)
    # Long timeout: NetworkManager may be waiting on a password dialog.
    res = ui.run("nmcli", "--wait", "90", "device", "wifi", "connect", ssid, timeout=100)
    ok = "successfully activated" in res
    ui.notify(f"{ui.icon('wifi_4' if ok else 'wifi_off')}  {'Connected' if ok else 'Failed to connect'}",
              ssid, tag="wifi", timeout_ms=3000)


def pick():
    if not ui.have("nmcli"):
        ui.notify("nmcli not found", "Install network-manager to pick Wi-Fi networks.")
        return
    enabled = ui.run("nmcli", "radio", "wifi").strip() == "enabled"
    entries, actions = {}, {}
    connected = False
    if enabled:
        for ssid, signal, secure, in_use in _networks():
            connected |= in_use
            mark = ui.icon("selected") if in_use else " "
            line = f"{mark} {wifi_icon(signal)}  {ssid}" + (f"  {ui.icon('lock')}" if secure else "")
            entries[line] = ssid
        actions[f"  {ui.icon('refresh')}  Rescan"] = "rescan"
        if connected:
            actions[f"  {ui.icon('wifi_off')}  Disconnect"] = "disconnect"
        actions[f"  {ui.icon('wifi_off')}  Turn Wi-Fi off"] = "off"
    else:
        actions[f"  {ui.icon('wifi_4')}  Turn Wi-Fi on"] = "on"
    if ui.have("nm-connection-editor"):
        actions[f"  {ui.icon('settings')}  Edit connections"] = "edit"

    choice = ui.menu("Wi-Fi", list(entries) + list(actions))
    if choice in entries:
        if not choice.startswith(ui.icon("selected")):
            _connect(entries[choice])
        return
    action = actions.get(choice)
    if action == "rescan":
        ui.run("nmcli", "device", "wifi", "rescan", timeout=10)
        pick()
    elif action == "disconnect":
        dev = _wifi_device()
        if dev:
            ui.run("nmcli", "device", "disconnect", dev)
    elif action in ("on", "off"):
        ui.run("nmcli", "radio", "wifi", action)
    elif action == "edit":
        ui.spawn("nm-connection-editor")


def main(argv):
    if argv[1:] != ["pick"]:
        sys.exit(__doc__)
    pick()


if __name__ == "__main__":
    main(sys.argv)
