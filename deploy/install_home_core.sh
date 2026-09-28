#!/usr/bin/env bash
# Phase 0b, user half: run NORA as a service on the home-core laptop.
#
#   bash deploy/install_home_core.sh
#
# Installs the systemd user units (NORA, lock-at-login, daily backup) and
# creates the backup passphrase. Safe to re-run. The root half — lid switch,
# GDM auto-login, rclone — is deploy/home_core_root.sh.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
units="$HOME/.config/systemd/user"
pass="$HOME/.config/nora-backup/passphrase"

if [[ "$repo" != "$HOME/Projects/JARVIS" ]]; then
    echo "The units hard-code ~/Projects/JARVIS; this checkout is $repo." >&2
    exit 1
fi

mkdir -p "$units"
for u in nora.service nora-autolock.service nora-backup.service nora-backup.timer; do
    install -m 0644 "$repo/deploy/systemd/$u" "$units/$u"
done
systemctl --user daemon-reload

# Enabled, not started: starting it now would lock the screen in your face.
systemctl --user enable nora-autolock.service
systemctl --user enable --now nora-backup.timer
systemctl --user enable nora.service
if systemctl --user is-active --quiet nora.service; then
    echo "NORA service already running; restart it to pick up changes."
elif pgrep -f "python3? (\S*/)?main\.py" >/dev/null; then
    echo "NORA is already running by hand; stop it, then: systemctl --user start nora"
else
    systemctl --user start nora.service
fi

if [[ ! -f "$pass" ]]; then
    mkdir -p "$(dirname "$pass")"
    chmod 700 "$(dirname "$pass")"
    (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$pass")
    echo
    echo "New backup passphrase (also in $pass):"
    echo
    echo "    $(cat "$pass")"
    echo
    echo "Save it in your password manager NOW. Without it the backups cannot be"
    echo "decrypted, and the copy on this laptop dies with this laptop."
fi

echo
echo "Done. Check with: systemctl --user status nora nora-backup.timer"
