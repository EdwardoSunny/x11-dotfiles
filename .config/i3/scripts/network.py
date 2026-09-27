#!/usr/bin/env python3
"""Network status for the bar and a Wi-Fi picker.

Usage: network.py pick

Uses NetworkManager (`nmcli`). Saved networks are activated by profile (so
renamed profiles work); new secured networks prompt for the password in the
menu. Without nmcli, status falls back to sysfs and the picker is unavailable.
"""
import glob
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import ui  # noqa: E402

_C_ENV = {**os.environ, "LC_ALL": "C"}  # English, parseable nmcli messages
CONNECT_WAIT = 45  # seconds NetworkManager may take to associate + get DHCP


def nmcli(*args, timeout=15):
    """Run nmcli; return (ok, text) where text is stdout, or the error message on failure."""
    try:
        res = subprocess.run(["nmcli", *args], capture_output=True, text=True,
                             timeout=timeout, env=_C_ENV)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as exc:
        return False, str(exc)
    if res.returncode == 0:
        return True, res.stdout
    return False, (res.stderr or res.stdout).strip().removeprefix("Error: ")


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
        ok, out = nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", timeout=3)
        wifi = None
        for line in out.splitlines() if ok else []:
            fields = _terse(line)
            if len(fields) != 4 or fields[2] != "connected":
                continue
            dev, kind, _, conn = fields
            if kind == "ethernet":
                return {"kind": "ethernet", "name": conn, "signal": None}
            if kind == "wifi" and wifi is None:
                wifi = {"kind": "wifi", "name": conn, "signal": _proc_signal(dev)}
        return wifi
    # Fallback without NetworkManager: any physical interface that is up.
    for path in sorted(glob.glob("/sys/class/net/*")):
        dev = os.path.basename(path)
        if not os.path.exists(os.path.join(path, "device")):
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


# ---- picker ----------------------------------------------------------------------

def _wifi_device():
    ok, out = nmcli("-t", "-f", "DEVICE,TYPE", "device")
    for line in out.splitlines() if ok else []:
        fields = _terse(line)
        if len(fields) == 2 and fields[1] == "wifi":
            return fields[0]
    return None


def _networks(rescan):
    """Visible networks, deduplicated by SSID, current first then strongest:
    [{'ssid', 'signal', 'security', 'in_use'}]. rescan: 'no' (cached, instant) or 'yes'."""
    ok, out = nmcli("-t", "-f", "IN-USE,SIGNAL,SECURITY,SSID", "device", "wifi", "list",
                    "--rescan", rescan, timeout=30)
    best = {}
    for line in out.splitlines() if ok else []:
        fields = _terse(line)
        if len(fields) != 4 or not fields[3]:  # skip hidden (empty SSID)
            continue
        in_use, signal, security, ssid = fields
        net = {"ssid": ssid, "signal": int(signal) if signal.isdigit() else 0,
               "security": "" if security == "--" else security, "in_use": in_use == "*"}
        prev = best.get(ssid)
        if prev:  # same SSID on several access points / bands
            net["signal"] = max(net["signal"], prev["signal"])
            net["security"] = net["security"] or prev["security"]
            net["in_use"] = net["in_use"] or prev["in_use"]
        best[ssid] = net
    return sorted(best.values(), key=lambda n: (not n["in_use"], -n["signal"]))


def _saved_profiles():
    """{ssid: uuid} of saved Wi-Fi profiles (profile names may differ from the SSID)."""
    ok, out = nmcli("-t", "-f", "UUID,TYPE", "connection", "show")
    profiles = {}
    for line in out.splitlines() if ok else []:
        fields = _terse(line)
        if len(fields) == 2 and fields[1] == "802-11-wireless":
            ok, ssid = nmcli("-g", "802-11-wireless.ssid", "connection", "show", "uuid", fields[0])
            if ok and ssid.strip():
                profiles.setdefault(ssid.strip(), fields[0])
    return profiles


def _is_enterprise(security):
    return "802.1X" in security


def _result(ok, ssid, msg):
    if ok:
        ui.notify(f"{ui.icon('wifi_4')}  Connected", ssid, tag="wifi", timeout_ms=3000)
    else:
        ui.notify(f"{ui.icon('wifi_off')}  Couldn't connect to {ssid}", msg, tag="wifi", timeout_ms=8000)


def connect(ssid, security, dev, hidden=False):
    """Connect to `ssid`: saved profile first, else a new profile (asking for a password if secured)."""
    uuid = _saved_profiles().get(ssid)
    if uuid:
        ui.notify(f"{ui.icon('wifi_2')}  Connecting…", ssid, tag="wifi", timeout_ms=CONNECT_WAIT * 1000)
        ok, msg = nmcli("--wait", str(CONNECT_WAIT), "connection", "up", "uuid", uuid,
                        "ifname", dev, timeout=CONNECT_WAIT + 15)
        if not ok and "Secrets were required" in msg and not _is_enterprise(security):
            # Saved password is wrong/missing: ask, store it, retry once.
            password = ui.ask(f"Password for {ssid}", secret=True)
            if password is None:
                return _result(False, ssid, msg)
            nmcli("connection", "modify", "uuid", uuid, "802-11-wireless-security.psk", password)
            ok, msg = nmcli("--wait", str(CONNECT_WAIT), "connection", "up", "uuid", uuid,
                            "ifname", dev, timeout=CONNECT_WAIT + 15)
        return _result(ok, ssid, msg)

    if _is_enterprise(security):
        ui.notify(f"{ui.icon('lock')}  {ssid} needs a username/certificate",
                  "Set it up in the connection editor.", timeout_ms=6000)
        ui.spawn_first(["nm-connection-editor"])
        return None

    args = ["--wait", str(CONNECT_WAIT), "device", "wifi", "connect", ssid, "ifname", dev]
    if security or hidden:
        password = ui.ask(f"Password for {ssid}" + (" (empty if open)" if hidden else ""), secret=True)
        if password is None and not hidden:
            return None
        if password:
            args += ["password", password]
    if hidden:
        args += ["hidden", "yes"]
    ui.notify(f"{ui.icon('wifi_2')}  Connecting…", ssid, tag="wifi", timeout_ms=CONNECT_WAIT * 1000)
    ok, msg = nmcli(*args, timeout=CONNECT_WAIT + 15)
    if not ok:
        # Don't keep a profile with a wrong password: it would be reused silently next time.
        new_uuid = _saved_profiles().get(ssid)
        if new_uuid:
            nmcli("connection", "delete", "uuid", new_uuid)
    return _result(ok, ssid, msg)


def pick():
    if not ui.have("nmcli"):
        ui.notify("nmcli not found", "Install network-manager to pick Wi-Fi networks.")
        return
    dev = _wifi_device()
    if dev is None:
        ui.notify(f"{ui.icon('wifi_off')}  No Wi-Fi adapter", "NetworkManager sees no Wi-Fi device.")
        return
    ok, radio = nmcli("radio", "wifi")
    enabled = ok and radio.strip() == "enabled"

    entries, actions = {}, {}
    if enabled:
        networks = _networks("no")  # cached scan: the menu opens instantly
        if not networks:
            ui.notify(f"{ui.icon('refresh')}  Scanning for networks…", tag="wifi", timeout_ms=5000)
            networks = _networks("yes")
        for net in networks:
            lock = f"  {ui.icon('lock')}" if net["security"] else ""
            mark = f"  {ui.icon('selected')}" if net["in_use"] else ""
            entries[f"{wifi_icon(net['signal'])}  {net['ssid']}{lock}{mark}"] = net
        actions[f"{ui.icon('refresh')}  Rescan"] = "rescan"
        actions[f"{ui.icon('wifi_2')}  Hidden network…"] = "hidden"
        if any(n["in_use"] for n in networks):
            actions[f"{ui.icon('wifi_off')}  Disconnect"] = "disconnect"
        actions[f"{ui.icon('wifi_off')}  Turn Wi-Fi off"] = "off"
    else:
        actions[f"{ui.icon('wifi_4')}  Turn Wi-Fi on"] = "on"
    if ui.have("nm-connection-editor"):
        actions[f"{ui.icon('settings')}  Edit connections"] = "edit"

    choice = ui.menu("Wi-Fi", list(entries) + list(actions))
    if choice in entries:
        net = entries[choice]
        if net["in_use"]:
            ui.notify(f"{ui.icon('wifi_4')}  Already connected", net["ssid"], tag="wifi")
        else:
            connect(net["ssid"], net["security"], dev)
        return
    action = actions.get(choice)
    if action == "rescan":
        ui.notify(f"{ui.icon('refresh')}  Scanning for networks…", tag="wifi", timeout_ms=5000)
        nmcli("device", "wifi", "rescan", "ifname", dev, timeout=15)
        # rescan returns before results land; listing with --rescan yes waits for them
        _networks("yes")
        pick()
    elif action == "hidden":
        ssid = ui.ask("Hidden network name (SSID)")
        if ssid:
            connect(ssid, "", dev, hidden=True)
    elif action == "disconnect":
        nmcli("device", "disconnect", dev)
    elif action in ("on", "off"):
        nmcli("radio", "wifi", action)
    elif action == "edit":
        ui.spawn("nm-connection-editor")


def main(argv):
    if argv[1:] != ["pick"]:
        sys.exit(__doc__)
    _lock = ui.single_instance("wifi-picker")  # noqa: F841
    pick()


if __name__ == "__main__":
    main(sys.argv)
