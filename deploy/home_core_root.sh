#!/usr/bin/env bash
# Phase 0b, root half: keep the home-core laptop awake and logged in.
#
#   sudo bash deploy/home_core_root.sh
#
# 1. Closing the lid no longer suspends (logind drop-in).
# 2. GDM logs the user in automatically after a reboot, so NORA's session
#    (mic, speaker, screen control) comes back unattended. nora-autolock.service
#    locks the screen the moment that session starts.
# 3. Installs rclone for the Google Drive backups.
#
# Safe to re-run. Undo: delete /etc/systemd/logind.conf.d/nora-home-core.conf,
# and set AutomaticLoginEnable=false in /etc/gdm3/daemon.conf.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo." >&2
    exit 1
fi
user="${SUDO_USER:?run via sudo from your own account}"

# 1. Lid switch
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/nora-home-core.conf <<'EOF'
# NORA home core: this laptop lives closed on a shelf. See deploy/home_core_root.sh.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
# logind re-reads its config on SIGHUP; restarting it would end the session.
systemctl kill -s HUP systemd-logind
echo "Lid switch: ignored"

# 2. GDM auto-login
conf=/etc/gdm3/daemon.conf
cp -n "$conf" "$conf.before-nora"
python3 - "$conf" "$user" <<'EOF'
import re, sys
path, user = sys.argv[1], sys.argv[2]
text = open(path).read()
text = re.sub(r"(?m)^\s*#?\s*AutomaticLogin(Enable)?\s*=.*\n", "", text)
text = re.sub(r"(?m)^\[daemon\]\s*$",
              f"[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin={user}", text, count=1)
open(path, "w").write(text)
EOF
echo "GDM auto-login: $user (original kept at $conf.before-nora)"

# 3. rclone
if ! command -v rclone >/dev/null; then
    apt-get install -y rclone
fi
echo "rclone: $(rclone version | head -1)"
