#!/usr/bin/env python3
"""App launcher: rofi (apps with icons, see ~/.config/rofi) if installed, else dmenu_run."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import ui  # noqa: E402

if ui.have("rofi"):
    os.execvp("rofi", ["rofi", "-show", "drun"])
os.execvp("dmenu_run", ["dmenu_run", *ui.dmenu_args("run")])
