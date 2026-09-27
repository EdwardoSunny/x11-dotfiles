"""Shared helpers for the i3 scripts: palette, icons, menus, notifications.

Stdlib only. Every external tool is optional; callers check `have()` first.
"""
import fcntl
import os
import shutil
import subprocess
import sys

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
            cmd, capture_output=True, text=True, timeout=timeout, env=env
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


def menu(prompt, lines):
    """Show a vertical menu (rofi if installed, else dmenu); return the chosen line or None."""
    if have("rofi"):
        cmd = ["rofi", "-dmenu", "-i", "-no-custom", "-format", "s", "-p", prompt]
    elif have("dmenu"):
        cmd = ["dmenu", "-l", str(min(len(lines), 15)), *dmenu_args(prompt)]
    else:
        notify("No menu program", "Install rofi or dmenu.")
        return None
    choice = _run_menu(cmd, "\n".join(lines))
    return choice if choice in lines else None


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
