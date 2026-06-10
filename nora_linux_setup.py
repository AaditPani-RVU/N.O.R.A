"""nora-linux-setup — one-time privileged install for Linux flagships.

Wraps three sub-installers behind a single polkit-mediated sudo prompt:
  1. ydotool udev rule (F1 AT-SPI2 input synthesis)
  2. bpf_runner setcap  (F3 eBPF Why Engine)
  3. snap_runner setcap (F4 Time-Travel snapshots)

Run once after pip install:
  python nora_linux_setup.py

Or via the console-script entry (if installed):
  nora-linux-setup
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIBEXEC = Path("/usr/local/libexec")


def _banner(title: str) -> None:
    print(f"\n{'─' * 60}\n  {title}\n{'─' * 60}")


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False)
    if check and result.returncode != 0:
        print(f"  ERROR: command failed (exit {result.returncode})")
    return result


def install_ydotool_udev() -> bool:
    """Create udev rule so the user's input group can use ydotool."""
    _banner("F1: ydotool udev rule")
    rule_content = textwrap.dedent("""\
        # NORA: allow uinput access for ydotool (voice-driven input synthesis)
        KERNEL=="uinput", GROUP="input", MODE="0660", TAG+="uacl"
    """)
    rule_path = Path("/etc/udev/rules.d/60-nora-uinput.rules")
    try:
        result = subprocess.run(
            ["sudo", "tee", str(rule_path)],
            input=rule_content,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            _run(["sudo", "udevadm", "control", "--reload-rules"])
            _run(["sudo", "udevadm", "trigger"])
            # Add current user to input group
            user = os.environ.get("USER", os.environ.get("LOGNAME", ""))
            if user:
                _run(["sudo", "usermod", "-aG", "input", user])
            print(f"  ✓ ydotool udev rule installed at {rule_path}")
            print("  NOTE: log out and back in for group membership to take effect.")
            return True
        print(f"  ✗ tee failed: {result.stderr}")
        return False
    except Exception as e:
        print(f"  ✗ {e}")
        return False


def install_bpf_runner() -> bool:
    """Copy bpf_runner.py to /usr/local/libexec and set capabilities."""
    _banner("F3: eBPF Why Engine — bpf_runner")
    src = HERE / "nora" / "observability" / "bpf_runner.py"
    if not src.exists():
        print(f"  ✗ Source not found: {src}")
        return False

    dest = LIBEXEC / "nora-bpf-runner"
    try:
        _run(["sudo", "mkdir", "-p", str(LIBEXEC)])
        _run(["sudo", "cp", str(src), str(dest)])
        _run(["sudo", "chmod", "755", str(dest)])

        # Try kernel 5.8+ capability approach first
        result = _run(
            ["sudo", "setcap", "cap_bpf,cap_perfmon,cap_sys_resource+ep", str(dest)],
            check=False,
        )
        if result.returncode != 0:
            print("  setcap failed (kernel < 5.8?), falling back to setuid root")
            _run(["sudo", "chown", "root:root", str(dest)])
            _run(["sudo", "chmod", "4755", str(dest)])

        print(f"  ✓ bpf_runner installed at {dest}")
        return True
    except Exception as e:
        print(f"  ✗ {e}")
        return False


def install_snap_runner() -> bool:
    """Install a minimal privileged wrapper for btrfs/zfs subvolume swaps."""
    _banner("F4: Time-Travel — snap_runner")
    # snap_runner is a minimal shell wrapper; inline here for simplicity
    script_content = textwrap.dedent("""\
        #!/bin/bash
        # nora-snap-runner: allowlisted btrfs/zfs operations for NORA F4.
        # Installed with cap_sys_admin (or setuid root).
        set -euo pipefail
        CMD="${1:-}"
        case "$CMD" in
          btrfs-snapshot)
            # args: <src_subvol> <dst_dir>
            btrfs subvolume snapshot -r "$2" "$3"
            ;;
          btrfs-rollback)
            # args: <snap_dir> <live_subvol>
            btrfs subvolume snapshot "$2" "$3-nora-new"
            btrfs subvolume delete "$3" 2>/dev/null || true
            mv "$3-nora-new" "$3"
            ;;
          zfs-snapshot)
            zfs snapshot "$2"
            ;;
          zfs-rollback)
            zfs rollback -r "$2"
            ;;
          *)
            echo "Unknown command: $CMD" >&2
            exit 1
            ;;
        esac
    """)
    dest = LIBEXEC / "nora-snap-runner"
    try:
        result = subprocess.run(
            ["sudo", "tee", str(dest)],
            input=script_content,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  ✗ tee failed: {result.stderr}")
            return False
        _run(["sudo", "chmod", "755", str(dest)])
        result2 = _run(
            ["sudo", "setcap", "cap_sys_admin,cap_sys_ptrace,cap_checkpoint_restore+ep", str(dest)],
            check=False,
        )
        if result2.returncode != 0:
            _run(["sudo", "chown", "root:root", str(dest)])
            _run(["sudo", "chmod", "4755", str(dest)])
        print(f"  ✓ snap_runner installed at {dest}")
        return True
    except Exception as e:
        print(f"  ✗ {e}")
        return False


def check_dependencies() -> None:
    _banner("Dependency check")
    deps = {
        "bpftrace": "sudo apt install bpftrace linux-headers-$(uname -r)",
        "ydotool": "sudo apt install ydotool",
        "criu": "sudo apt install criu",
        "btrfs": "sudo apt install btrfs-progs",
        "pw-cli": "sudo apt install pipewire",
    }
    for bin_name, install_hint in deps.items():
        found = shutil.which(bin_name) is not None
        status = "✓" if found else "✗"
        hint = "" if found else f"  →  {install_hint}"
        print(f"  {status} {bin_name}{hint}")


def main() -> None:
    print("\nNORA Linux Setup — one-time privileged install")
    print("This script requires sudo for udev rules, setcap, and libexec installs.\n")

    check_dependencies()

    ok1 = install_ydotool_udev()
    ok2 = install_bpf_runner()
    ok3 = install_snap_runner()

    _banner("Summary")
    print(f"  F1 ydotool udev: {'✓' if ok1 else '✗'}")
    print(f"  F3 bpf_runner:   {'✓' if ok2 else '✗'}")
    print(f"  F4 snap_runner:  {'✓' if ok3 else '✗'}")

    if all([ok1, ok2, ok3]):
        print("\nAll components installed. Restart NORA to activate Linux features.")
    else:
        print("\nSome components failed — check output above. NORA will degrade gracefully.")

    sys.exit(0 if all([ok1, ok2, ok3]) else 1)


if __name__ == "__main__":
    main()
