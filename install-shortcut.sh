#!/bin/sh
# Add Groundhog Tuner to the Linux application menu.
# --autostart also opens it when you log in.
set -e
root=$(cd "$(dirname "$0")" && pwd)
if [ "$(uname)" = "Darwin" ]; then
  echo "On macOS, double-click \"Groundhog Tuner.command\" in Finder instead."
  echo "To open it at login: System Settings > General > Login Items > add that file."
  exit 0
fi
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
autostart="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
# The app was called Groundhog Gamma Tuner until October 2026. Replace its
# entries, and keep opening at login if the old entry did.
rm -f "$apps/groundhog-gamma-tuner.desktop"
if [ -f "$autostart/groundhog-gamma-tuner.desktop" ]; then
  rm -f "$autostart/groundhog-gamma-tuner.desktop"
  set -- --autostart
fi
mkdir -p "$apps"
entry="$apps/groundhog-tuner.desktop"
cat > "$entry" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Groundhog Tuner
Comment=Tune Bitaxe miners running AxeOS
Exec="$root/run.sh"
Path=$root
Icon=$root/assets/app_icon.png
Terminal=false
Categories=Utility;
DESKTOP
chmod +x "$root/run.sh" "$entry"
echo "Added to the application menu: $entry"
if [ "$1" = "--autostart" ]; then
  mkdir -p "$autostart"
  cp "$entry" "$autostart/"
  echo "Opens at login: $autostart/groundhog-tuner.desktop"
fi
