"""Shared helpers for the i3 scripts: palette, icons, menus, notifications.

Stdlib only. Every external tool is optional; callers check `have()` first.
"""
import fcntl
import html
import os
import shutil
import subprocess
import sys
import threading

# Monochrome palette shared by the bar and the menus (keep in sync with
# `bar { colors }` in ../config). Color only for warnings.
BG = "#0f0f0f"
FG = "#e6e6e6"
DIM = "#707070"
ACCENT = "#a8a8a8"  # icons
YELLOW = "#d6c38d"
RED = "#d48a8a"

FONT = "DejaVu Sans Mono"
FONT_SIZE = 10
ICON_FONT = "Symbols Nerd Font Mono"

TERMINAL = "i3-sensible-terminal"

# Nerd Font glyph, plain-text fallback used when the icon font is missing.
_ICONS = {
    "vol_low": ("\U000F057F", "vol"),
    "vol_mid": ("\U000F0580", "vol"),
    "vol_high": ("\U000F057E", "vol"),
    "vol_mute": ("\U000F075F", "mute"),
    "mic": ("\U000F036C", "mic"),
    "mic_mute": ("\U000F036D", "mic off"),
    "speaker": ("\U000F04C3", ">"),
    "wifi_0": ("\U000F092F", "wifi"),
    "wifi_1": ("\U000F091F", "wifi"),
    "wifi_2": ("\U000F0922", "wifi"),
    "wifi_3": ("\U000F0925", "wifi"),
    "wifi_4": ("\U000F0928", "wifi"),
    "wifi_off": ("\U000F092D", "wifi off"),
    "lock": ("\U000F033E", "*"),
    "ethernet": ("\U000F0200", "eth"),
    "bt_on": ("\U000F00AF", "bt"),
    "bt_connected": ("\U000F00B1", "bt"),
    "bt_off": ("\U000F00B2", "bt off"),
    "cpu": ("\U000F035B", "cpu"),
    "mem": ("\U000F061A", "mem"),
    "settings": ("\U000F0493", "~"),
    "refresh": ("\U000F0450", "~"),
    "selected": ("\U000F012C", "(current)"),
}


def have(cmd):
    return shutil.which(cmd) is not None


def _icon_font_installed():
    if not have("fc-list"):
        return False
    out = subprocess.run(["fc-list", ICON_FONT, "family"], capture_output=True, text=True).stdout
    return bool(out.strip())


NERD_FONT = _icon_font_installed()


def icon(name):
    glyph, text = _ICONS[name]
    return glyph if NERD_FONT else text


def run(*cmd, timeout=3, env=None):
    """Run a command and return stdout ("" on any failure)."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env,
            stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def spawn(*cmd):
    """Start a detached process (GUI apps, pickers) without waiting on it."""
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def spawn_first(*candidates):
    """Spawn the first candidate command whose binary is installed."""
    for cmd in candidates:
        if have(cmd[0]):
            spawn(*cmd)
            return True
    return False


def open_in_terminal(*cmd):
    spawn(TERMINAL, "-e", *cmd)


def dmenu_args(prompt):
    """dmenu flags matching the palette (fallback when rofi isn't installed)."""
    return ["-i", "-p", prompt, "-fn", f"{FONT}:size={FONT_SIZE + 1}",
            "-nb", BG, "-nf", FG, "-sb", FG, "-sf", BG]


def _run_menu(cmd, text):
    """Run a menu program; return its selection, or None if cancelled/failed."""
    try:
        res = subprocess.run(cmd, input=text, capture_output=True, text=True)
    except OSError:
        return None
    return res.stdout.rstrip("\n") if res.returncode == 0 else None


def menu(prompt, lines, active=(), more=None, keys=()):
    """Pick one item with rofi (or dmenu). Returns (line, key): the chosen line or
    None, and the name of the hotkey action if one was used instead.

    lines   rows shown immediately
    active  indices of "current" rows (highlighted in rofi, marked in dmenu)
    more    optional callable returning extra rows (e.g. a slow Wi-Fi scan); rofi
            opens at once and appends them live, dmenu waits for them first
    keys    [(binding, name, label)] extra actions: rofi hotkeys listed under the
            input (e.g. "Alt+d"), dmenu extra rows
    """
    lines = list(lines)
    if have("rofi"):
        return _rofi_menu(prompt, lines, active, more, keys)
    if have("dmenu"):
        return _dmenu_menu(prompt, lines, active, more, keys)
    notify("No menu program", "Install rofi or dmenu.")
    return None, None


def _rofi_menu(prompt, lines, active, more, keys):
    # No -no-custom or -selected-row: with either, rofi waits for the end of input
    # before drawing. Typed text that matches no row is rejected below instead.
    cmd = ["rofi", "-dmenu", "-i", "-format", "s", "-p", prompt,
           "-async-pre-read", "0"]  # show rows as they arrive
    if active:
        cmd += ["-a", ",".join(map(str, active))]
    for n, (binding, _, _) in enumerate(keys, 1):
        cmd += [f"-kb-custom-{n}", binding]
    if keys:
        cmd += ["-mesg", html.escape("  ·  ".join(f"{b.lower()} {label}" for b, _, label in keys))]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    except OSError:
        return None, None
    shown = list(lines)

    def feed():
        try:
            proc.stdin.write("".join(line + "\n" for line in lines))
            proc.stdin.flush()
            if more:
                extra = more()
                shown.extend(extra)  # before writing, so a pick of a new row is recognized
                proc.stdin.write("".join(line + "\n" for line in extra))
            proc.stdin.close()
        except (OSError, ValueError):  # rofi already closed (user picked early)
            pass

    threading.Thread(target=feed, daemon=True).start()
    choice = proc.stdout.read().rstrip("\n")
    code = proc.wait()
    if 10 <= code < 10 + len(keys):  # kb-custom-N exits with 9 + N
        return None, keys[code - 10][1]
    return (choice if code == 0 and choice in shown else None), None


def _dmenu_menu(prompt, lines, active, more, keys):
    mark = f"  {icon('selected')}"
    rows = {line + (mark if i in active else ""): line for i, line in enumerate(lines)}
    for line in more() if more else []:
        rows[line] = line
    actions = {label: name for _, name, label in keys}
    options = list(rows) + list(actions)
    choice = _run_menu(["dmenu", "-l", str(min(len(options), 15)), *dmenu_args(prompt)],
                       "\n".join(options))
    if choice in actions:
        return None, actions[choice]
    return rows.get(choice), None


def ask(prompt, secret=False):
    """Prompt for one line of text (masked if `secret`); None if cancelled or empty."""
    if have("rofi"):
        cmd = ["rofi", "-dmenu", "-l", "0", "-p", prompt] + (["-password"] if secret else [])
    elif secret and have("zenity"):
        cmd = ["zenity", "--password", f"--title={prompt}"]
    elif have("dmenu"):
        cmd = ["dmenu", *dmenu_args(prompt)]
        if secret:  # stock dmenu can't mask input; draw the typed text in the background color
            cmd += ["-nf", BG, "-sf", BG]
    else:
        notify("No menu program", "Install rofi or dmenu.")
        return None
    return _run_menu(cmd, "") or None


def single_instance(name):
    """Exit if another copy of this picker is already open (avoids stacked menus)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    lock = open(os.path.join(runtime, f"i3-{name}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)
    return lock  # keep a reference: the lock lives as long as the file object


def notify(summary, body="", tag=None, value=None, timeout_ms=1500):
    """Desktop notification; `tag` replaces the previous one with the same tag (dunst OSD style)."""
    if not have("notify-send"):
        return
    cmd = ["notify-send", "-a", "i3", "-u", "low", "-t", str(timeout_ms)]
    if tag:
        cmd += ["-h", f"string:x-dunst-stack-tag:{tag}", "-h", f"string:synchronous:{tag}"]
    if value is not None:
        cmd += ["-h", f"int:value:{value}"]
    spawn(*cmd, summary, body)
