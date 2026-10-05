#!/usr/bin/env bash
# Set up these dotfiles on a machine (Ubuntu/Debian). Safe to re-run.
#   1. link ~/.config -> <repo>/.config (an existing ~/.config is moved aside, never deleted)
#   2. install the packages the i3 config uses
#   3. install the icon font used by the bar/menus (Symbols Nerd Font)
#   4. enable the xremap user service if xremap is installed
#   5. install the Handy (speech to text) settings from handy/settings_store.json
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- 1. ~/.config symlink --------------------------------------------------------
if [ "$(readlink -f "$HOME/.config")" != "$repo/.config" ]; then
    if [ -e "$HOME/.config" ] || [ -L "$HOME/.config" ]; then
        backup="$HOME/.config.bak-$(date +%Y%m%d-%H%M%S)"
        read -r -p "Move existing ~/.config to $backup and link the repo? [y/N] " answer
        [[ "$answer" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
        mv "$HOME/.config" "$backup"
    fi
    ln -s "$repo/.config" "$HOME/.config"
    echo "Linked ~/.config -> $repo/.config"
fi

# ---- 2. packages ----------------------------------------------------------------
pkgs=(
    i3 i3status i3lock xss-lock suckless-tools  # window manager, lock, dmenu (fallback menus)
    rofi                                   # launcher + menus (translucent theme in .config/rofi)
    feh picom flameshot parcellite         # wallpaper, compositor (menu blur), screenshots, clipboard
    dunst libnotify-bin                    # notifications (volume OSD)
    network-manager-gnome                  # nm-applet, nm-connection-editor (Wi-Fi)
    pulseaudio-utils pavucontrol           # pactl (PipeWire or PulseAudio), mixer
    libglib2.0-bin                         # gdbus (media keys via MPRIS)
    xdotool                                # Handy sends its paste key (ctrl+shift+v) through it
    python3 fontconfig fonts-dejavu-core curl unzip
)
if command -v apt-get >/dev/null; then
    missing=()
    for p in "${pkgs[@]}"; do
        dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
    done
    if ((${#missing[@]})); then
        echo "Installing: ${missing[*]}"
        sudo apt-get install -y "${missing[@]}"
    fi
else
    echo "Not a Debian/Ubuntu system; install these (or equivalents) manually: ${pkgs[*]}"
fi

# ---- 3. icon font ----------------------------------------------------------------
if [ -z "$(fc-list 'Symbols Nerd Font Mono' family)" ]; then
    fonts="$HOME/.local/share/fonts/NerdFontsSymbolsOnly"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    curl -fsSL -o "$tmp/symbols.zip" \
        https://github.com/ryanoasis/nerd-fonts/releases/latest/download/NerdFontsSymbolsOnly.zip
    mkdir -p "$fonts"
    unzip -oq "$tmp/symbols.zip" '*.ttf' -d "$fonts"
    fc-cache -f "$fonts"
    echo "Installed Symbols Nerd Font to $fonts"
fi

# ---- 4. xremap -------------------------------------------------------------------
if [ -x "$HOME/.cargo/bin/xremap" ]; then
    systemctl --user daemon-reload
    systemctl --user enable --now xremap.service
else
    echo "xremap not installed; see README.org (xremap things) to install it, then re-run."
fi

# ---- 5. Handy settings -----------------------------------------------------------
# Handy keeps its settings in memory and writes them back, so only copy while it's not running.
handy_src="$repo/handy/settings_store.json"
handy_dst="$HOME/.local/share/com.pais.handy/settings_store.json"
if ! cmp -s "$handy_src" "$handy_dst"; then
    if pgrep -x handy >/dev/null; then
        echo "Handy is running; quit it (tray icon > Quit) and re-run to install its settings."
    else
        answer=y
        if [ -e "$handy_dst" ]; then
            read -r -p "Replace Handy settings with the repo's (old file kept as .bak-<date>)? [y/N] " answer
            [[ "$answer" =~ ^[Yy]$ ]] && cp "$handy_dst" "$handy_dst.bak-$(date +%Y%m%d-%H%M%S)"
        fi
        if [[ "$answer" =~ ^[Yy]$ ]]; then
            mkdir -p "$(dirname "$handy_dst")"
            cp "$handy_src" "$handy_dst"
            echo "Installed Handy settings to $handy_dst"
        fi
    fi
fi

echo "Done. Reload i3 with \$mod+Shift+r."
