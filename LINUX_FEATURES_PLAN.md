# NORA — Linux Flagship Features ("Ahead of Time")

## Context

NORA is a polished voice assistant (cognitive memory, ReAct planner, wakeword, MCP, screen intelligence) — but ~half its OS-touching command modules are Windows/Mac only (`win32clipboard`, `win32gui`, `pyautogui`+`pycaw`, AppleScript). Today, a Linux user installing NORA hits import errors, missing audio, and a hollow command surface.

Rather than port the Windows/Mac code, this plan adds **revolutionary Linux-native capabilities that aren't possible on Windows or macOS at all** — features that exploit Linux's unique kernel and userspace primitives (AT-SPI2 accessibility bus, D-Bus universal IPC, eBPF kernel observability, COW filesystems + CRIU, PipeWire graph mutation). The outcome is a NORA that, on Ubuntu/Fedora with a GPU, does things Siri/Cortana/Alexa/Google Assistant literally cannot — and stays unprivileged in the main process while doing it.

**Constraints honored:**
- New modules only; existing Windows commands untouched.
- Three tiny additive observer hooks in existing files (~10 LOC each) — flagged explicitly below.
- Target: modern Ubuntu 24.04 / Fedora 40, NVIDIA or AMD GPU.
- 4 flagships + 1 stretch, each demo-worthy in 30 seconds.

---

## The Four Flagships (+ Stretch)

### F1 — Semantic Screen Awareness via AT-SPI2

Replace fragile OCR with Linux's universal accessibility tree. Every GTK / Qt / Electron app publishes its widget hierarchy over the AT-SPI D-Bus. NORA reads the tree to know exactly where the "Submit" button is, what tab is focused, what text is in which field — **deterministically**. A small local VLM (Qwen2-VL-2B GGUF via `llama-cpp-python`) is invoked only when AT-SPI yields too few widgets (typical of un-flagged Electron). Mouse/keyboard synthesis under both Wayland and X11 via `ydotool` (so the same code works everywhere; `xdotool` is X11-only and would split the implementation).

**Library:** PyGObject `gi.repository.Atspi` + `ydotool` + `llama-cpp-python`.

**New files:**
- `nora/platform/linux/__init__.py`
- `nora/platform/linux/atspi_tree.py` — tree walker, fuzzy `find_element(description)` with `rapidfuzz` over `(role, name, parent_chain)`. GLib mainloop runs on a dedicated thread; results crossed into asyncio via `janus`.
- `nora/platform/linux/ydotool_input.py`
- `nora/platform/linux/vlm_fallback.py` — Qwen2-VL-2B, auto-downloaded on first use, fired only when `< 3` actionable widgets found.
- `nora/commands/atspi_actions.py`

**Registered actions (category `screen`):**
- `click_element(description)` — risk medium
- `read_focused_field()`, `list_buttons_in_window()`, `read_dialog()` — risk low
- `fill_field(label, text)` — risk medium
- `wait_for_element(description, timeout)` — event-driven via `Atspi.EventListener`

**Cognitive hooks (callback-only, no rewrites):**
- After every `click_element`/`fill_field`, call `cognitive_memory.record_episode(action, context={"app": ..., "role": ...})` — uses existing public API in `nora/cognitive_memory.py`.
- On VLM fallback, announce once-per-app-per-session that AT-SPI was incomplete, so NORA learns which apps are AT-SPI-blind.

**Landmines:**
- `at-spi-bus-launcher` must be running and `org.gnome.desktop.interface toolkit-accessibility` must be true; the setup script flips it.
- Electron apps need `--force-renderer-accessibility`; document, expect gaps, rely on VLM fallback.
- Wayland exposes only the focused window's tree to external readers — document this.

**Install (one line):** `sudo apt install python3-gi gir1.2-atspi-2.0 at-spi2-core ydotool && pip install rapidfuzz janus llama-cpp-python`.

**Privilege:** `ydotool` needs `/dev/uinput`; ship a udev rule (`/etc/udev/rules.d/60-nora-uinput.rules`) so the user's input group covers it. Main NORA stays unprivileged.

**Demo:** "NORA, in Firefox click the address bar, type github.com/anthropic, press enter, then click the Issues tab." Four AT-SPI calls, zero pixels read, faster and more accurate than any OCR loop.

---

### F2 — D-Bus Universal Voice Remote

Auto-discover every service on the session and system D-Bus, introspect their XML, and synthesize voice commands without per-app plugins. Spotify, VLC, Firefox, NetworkManager, BlueZ, Hyprland, KDE Connect, libvirt, podman — all become voice-controllable. This is what makes "write a Spotify plugin / write a NetworkManager plugin / write a BlueZ plugin" obsolete on Linux.

**Library:** `dbus-next` (asyncio-native, full introspection; `pydbus` is GLib-bound and wrong fit for NORA's loop).

**New files:**
- `nora/platform/linux/dbus_catalog.py` — startup introspection cache, pickled to `~/.nora/dbus_catalog.pkl`, 24h TTL.
- `nora/platform/linux/dbus_invoker.py` — typed-arg coercion + safe call wrapper.
- `nora/commands/dbus_remote.py`

**Registered actions (category `system`):**
- `dbus_call(service, object, method, args)` — risk medium, umbrella for the long tail.
- A curated set of ~30 "blessed" wrappers auto-generated at startup from a verb whitelist + arity heuristic: `media_play_pause()`, `media_next()`, `wifi_connect(ssid)`, `bluetooth_pair(mac)`, `night_light_toggle()`, `firefox_open_url(url)`, …
- `discover_dbus(query)` — semantic search over the full catalog so the LLM never sees the 10k-method firehose.

**Prompt-budget strategy:** the LLM only sees blessed wrappers + the umbrella + a "say 'discover dbus X' to find more" hint. Top-10 user methods after 7 days auto-promote to blessed wrappers via `~/.nora/dbus_blessed.json`.

**Cognitive hooks:**
- Every umbrella call records to cognitive memory; `predict_next_action` (already in `nora/cognitive_memory.py`) surfaces frequently-used services naturally.

**Landmines:**
- Session vs. system bus confusion — catalog tags every entry.
- `a{sv}` variant signatures — coerce dicts via dbus-next's `Variant` helper; refuse nested structs with a clear error.
- Spotify's MPRIS object path is non-standard — keep a small MPRIS shim.

**Install:** `pip install dbus-next`. Zero system deps beyond the session bus that's already running on every desktop.

**Demo:** "NORA, pause Spotify, connect to wifi CoffeeShop, turn on night light, and tell Firefox to open my GitHub notifications." Four different D-Bus services, one sentence, no per-app code.

---

### F3 — eBPF Causal Observer ("Why Engine")

NORA grows kernel-grade observability. A library of small bpftrace one-shots — `cpu_hotspot.bt`, `io_hotspot.bt`, `net_talkers.bt`, `dev_opens.bt`, `child_procs.bt`, `path_watch.bt` — runs on demand. When the user asks "why is my fan loud?" / "what's writing to disk?" / "which process touched /dev/video0?", NORA fires the right trace, pipes JSON output to the LLM, and speaks a plain-English causal explanation. The existing `nora/anomaly_watchdog.py` upgrades from "CPU > 90%" to "CPU > 90% — and here's the call stack causing it."

**Library:** `bpftrace -f json` via `asyncio.create_subprocess_exec`. (`bcc` Python bindings are kernel-version fragile and heavier; bpftrace ships pre-compiled BTF-aware on Ubuntu 24.04 / Fedora 40.)

**Privilege model:** main NORA stays unprivileged. A tiny helper `nora/observability/bpf_runner.py` is installed as `/usr/local/libexec/nora-bpf-runner` with `setcap cap_bpf,cap_perfmon,cap_sys_resource+ep` (kernel 5.8+; fallback setuid root for 5.4–5.7). The runner accepts **only allowlisted script names + arg dicts** over a Unix socket at `/run/user/$UID/nora-bpf.sock` — no arbitrary bpftrace code paths from the main process. One-time install via a new `nora-install-bpf` console script (polkit-mediated sudo prompt).

**New files:**
- `nora/observability/__init__.py`
- `nora/observability/bpf_scripts/*.bt` — the script library.
- `nora/observability/bpf_client.py` — Unix-socket client.
- `nora/observability/bpf_runner.py` — privileged helper.
- `nora/commands/why_engine.py`

**Registered actions (new category `observe`):**
- `why_busy(duration_sec=5)` — risk low
- `what_writes_disk(duration_sec=5)` — risk low
- `who_opened(path, duration_sec=10)` — risk low
- `top_talkers(duration_sec=5)` — risk low
- `watch_path(path)` / `unwatch_path(path)` — persistent watcher; risk medium

**⚠ One existing-file edit:** add `register_drill_down(cb)` to `nora/anomaly_watchdog.py` (~10 LOC, pure addition — no behavior change when no subscriber). The why-engine subscribes; when CPU > 90 fires, the watchdog calls back and the why-engine runs `cpu_hotspot.bt`, LLM-summarizes, and feeds the result into the watchdog's existing `_speak_cb` (line 170). This is the *only* way to wire automatic drill-downs without rewriting the watchdog.

**Cognitive hooks:** `record_episode("ebpf_alert", context=...)` on every watcher fire, so proactive engine can pattern-match repeats ("you ask 'why busy' every morning at 9 — want me to run it automatically?").

**Landmines:**
- Pre-5.8 kernels lack CAP_BPF — fall back to setuid root with strict path allowlist; refuse install on < 5.4.
- Secure Boot + `lockdown=integrity` blocks kprobes — detect via `/sys/kernel/security/lockdown`, degrade to tracepoints.
- Cap each script at 5s and 10k events to prevent runaway output.

**Install:** `sudo apt install bpftrace linux-headers-$(uname -r) && nora-install-bpf`.

**Demo:** "NORA, why is my fan loud?" → 5-second cpu_hotspot trace → "Chrome's GPU process is at 340% CPU running a WebGL benchmark in tab 7. Want me to kill that tab?"

---

### F4 — Time-Travel Filesystem + CRIU Session Continuity

NORA auto-snapshots before risky actions (file edit, package install, config change) and lets the user roll back by time or label. Going further: CRIU dumps the entire process tree to disk so a coding session — editor, terminals, dev server, even some daemons — can be paused and resumed across reboots. This is genuinely impossible on Windows/macOS as packaged consumer features.

**Libraries:** `subprocess` against `btrfs` and `zfs` CLIs (the Python wrappers are stale; the CLI is the stable contract). `pycriu` for CRIU's RPC daemon. FS auto-detected via `findmnt -no FSTYPE /home`.

**Privilege model:** same pattern as F3. A `nora/platform/linux/snap_runner.py` helper installed as `/usr/local/libexec/nora-snap-runner` with `cap_sys_admin,cap_sys_ptrace,cap_checkpoint_restore+ep`. Allowlisted to operations within `/home/$USER` and `/etc`. Polkit rule `org.nora.snapshot`. One-time `nora-install-snap` console script.

**New files:**
- `nora/platform/linux/snapshot_btrfs.py` — first-class.
- `nora/platform/linux/snapshot_zfs.py` — alternative.
- `nora/platform/linux/snapshot_rsync.py` — ext4 fallback, targeted dirs (`~/.config`, `~/.local/share/nora`) — explicitly NOT whole-home.
- `nora/platform/linux/criu_session.py`
- `nora/commands/time_travel.py`

**Registered actions (new category `time`):**
- `snapshot_now(label)` — risk low
- `list_snapshots()` — risk low
- `rollback_to(label_or_time)` — risk high, requires_confirmation true
- `pause_session(name, pids=None)` — risk high, requires_confirmation true
- `resume_session(name)` — risk high, requires_confirmation true

**⚠ One existing-file edit:** add `register_pre_action_hook(cb)` to `nora/reversible.py` (~10 LOC). Today, `record_action()` at line 65 is the only entry — the new hook fires before it, letting the snapshot module take an auto-labeled COW snapshot tagged with the action's episode ID. Then "undo my last package install" maps to a deterministic snapshot rollback.

**Cognitive hooks:** snapshot IDs recorded with each episode in `cognitive_memory`, so semantic recall ("the version of my config from before the Vim refactor") translates to a rollback target.

**Landmines:**
- CRIU + GPU processes: nvidia-uvm checkpointing is experimental — refuse to checkpoint PIDs holding `/dev/nvidia*` unless `experimental_gpu_checkpoint: true` in `config.yaml`.
- Btrfs snapshots of `/home` require `/home` to be its own subvolume (Fedora default yes, Ubuntu default no) — detect at install, warn loudly.
- ZFS on Ubuntu requires `zfsutils-linux`; on Fedora requires DKMS + reboot — don't auto-install; flag and document.
- Hard cap 100 snapshots, oldest unlabeled pruned first.

**Install:** `sudo apt install btrfs-progs criu && pip install pycriu && nora-install-snap`.

**Demo:** "NORA, snapshot before I refactor." → 20 minutes of work → "this is broken, roll back." → btrfs subvolume swap, 23 minutes erased. Then: "Pause my coding session." → close laptop. Next morning: "Resume yesterday's coding session." → CRIU restores nvim + 4 terminals + dev server.

---

### F5 — Adaptive Ambient (PipeWire + Wayland) [stretch]

PipeWire graph mutation gives NORA something no proprietary OS allows: live re-routing of any application's audio. "Duck Spotify when I speak" inserts an 8-second sidechain on the wakeword event. "Create a virtual mic with denoise" loads RNNoise as a LADSPA filter on the default source. "Tap Discord audio into transcription" creates a virtual sink and exposes a new source. Layered on top: Wayland compositor IPC (Sway/Hyprland/KWin) reactively tiles windows for "coding"/"writing"/"meeting" contexts inferred from cognitive memory.

**Libraries:** `pw-cli` + `pw-dump` subprocess (JSON, stable contract; native bindings are alpha). Compositor IPC: `i3ipc-python` for Sway, `hyprctl` subprocess for Hyprland, `org.kde.KWin` D-Bus for KWin (reuses F2's invoker — bonus).

**New files:**
- `nora/platform/linux/pipewire_graph.py`
- `nora/platform/linux/compositor_ipc.py` — autodetects `XDG_CURRENT_DESKTOP`, dispatches.
- `nora/platform/linux/kwin_scripts/*.js` — for KWin focus_mode.
- `nora/commands/ambient_linux.py` (suffix `_linux` to avoid colliding with the existing `nora/ambient.py`).

**Registered actions (category `focus`):**
- `duck_app_when_speaking(app)` — risk low
- `denoise_mic()` — risk low
- `tap_app_audio(app)` — risk low
- `focus_mode(intent)` — risk medium

**⚠ One existing-file edit:** add `register_on_wake_callback(cb)` to `nora/wakeword.py` (~10 LOC). When wake fires, registered subscribers (the ducker) drop the named PipeWire sink-input volume to 20% for 8s. Without this hook, ducking-on-wake isn't possible without a fork.

**Cognitive hooks:** `predict_next_action` already selects a likely intent given time-of-day + recent apps; proactive engine offers "switch to focus mode?" when confidence > 0.7.

**Landmines:**
- PipeWire object IDs are not stable across restarts — always look up by `node.name`/`application.name`.
- RNNoise plugin packaging varies (Ubuntu vs Fedora COPR) — graceful degrade to a noise gate.
- Hyprland IPC is a moving target — pin to `hyprctl` subprocess, avoid the raw socket protocol.

**Install:** `sudo apt install pipewire pipewire-pulse wireplumber libndi-rnnoise0 && pip install i3ipc`.

**Demo:** Music playing in Spotify. "Hey NORA" → music ducks instantly. "Enter focus mode for writing." → Firefox + Obsidian tile 60/40 on Sway, Slack notifications muted, Spotify switches to a lo-fi playlist via F2's MPRIS wrapper.

---

---

### F6 — systemd / journald Voice Interface

Every Linux machine has systemd. NORA grows a full voice interface to it: start/stop/enable services, query structured logs, explain failures, and analyse boot time — all via natural language. "Why did my server crash last night?" → NORA queries journald with the right filters, pipes the structured output to the LLM, and speaks a plain-English root-cause summary. This is the most universally useful Linux-exclusive feature; nothing equivalent exists on Windows or macOS as an unprivileged API.

**Library:** `systemd.journal` Python bindings (`python3-systemd`, ships in Debian/Fedora) for journald; `dbus-next` reuses F2's invoker for `org.freedesktop.systemd1` service control. `systemd-analyze` via subprocess for boot forensics.

**New files:**
- `nora/platform/linux/systemd_journal.py` — structured journal reader with cursor-based pagination and field filtering.
- `nora/platform/linux/systemd_units.py` — unit lifecycle (start/stop/enable/disable/reload) via D-Bus, no sudo required for user units; polkit prompt for system units.
- `nora/commands/systemd_voice.py`

**Registered actions (new category `system`):**
- `service_status(name)` — risk low
- `service_start(name)` / `service_stop(name)` / `service_restart(name)` — risk medium, system units require confirmation
- `service_enable(name)` / `service_disable(name)` — risk medium
- `journal_query(unit, since, until, priority)` — risk low; LLM summarises output
- `explain_last_failure(unit)` — risk low; pulls coredump context if available via `coredumpctl`
- `boot_analysis()` — risk low; `systemd-analyze blame` parsed and ranked
- `list_failed_units()` — risk low

**Cognitive hooks:** `record_episode("service_failure", ...)` on every `explain_last_failure` call so proactive engine can surface "nginx failed again — want me to check the logs?" after recurring failures.

**Landmines:**
- User vs. system bus: user units need `--user`; system units need polkit. Tag every unit with its bus at discovery time.
- `journal_query` output can be enormous — hard cap at 500 lines before LLM summarisation; use `PRIORITY` field to pre-filter noise.
- `coredumpctl` needs `systemd-coredump` installed — graceful degrade if absent.

**Install:** `sudo apt install python3-systemd`. Zero new system deps beyond what's already running.

**Demo:** Background `nginx` fails silently. Voice: "Why did nginx fail?" → NORA queries journald for the last nginx failure, finds a port 80 bind error, speaks "nginx couldn't bind to port 80 — something else is already using it. Want me to find what?" Voice: "Check boot time" → "Your slowest boot unit is snapd at 8.4 seconds. The next three are…"

---

### F7 — cgroups v2 Resource Governor

Voice-controlled, real-time resource limiting for any process tree — no sudo, no kernel modules, no reboot. Uses `systemd-run --scope` to place a running process (or a new one) into a transient cgroup with CPU quota, memory high/max, and I/O weight constraints. "NORA, throttle Chrome to 4 cores and 1 GB RAM while I compile" takes effect in under 100ms. On a 12-core Ryzen running heavy builds alongside a browser this is genuinely useful every day.

**Library:** `dbus-next` (reuses F2's invoker) against `org.freedesktop.systemd1` `StartTransientUnit` method. Reading current resource usage from `/sys/fs/cgroup/<slice>/` directly via Python `open()` — no external tools needed.

**New files:**
- `nora/platform/linux/cgroup_manager.py` — creates/updates/removes transient scopes; reads current cgroup accounting (`cpu.stat`, `memory.current`, `io.stat`).
- `nora/commands/resource_governor.py`

**Registered actions (new category `resources`):**
- `limit_app(app, cpu_cores=None, memory_mb=None, io_weight=None)` — risk low
- `unlimit_app(app)` — risk low
- `resource_usage(app)` — risk low; reads live cgroup stats
- `top_resource_hogs(n=5)` — risk low; ranks current cgroup slices by CPU + memory
- `throttle_build(cpu_cores)` — shortcut that finds the most recent compiler process tree

**How it works (no sudo):** `systemd-run --user --scope --slice=nora.slice -p CPUQuota=400% -p MemoryMax=1G --pid <PID> sleep inf` moves the existing PID into a managed scope. WirePlumber already runs user slices this way. `CPUQuota=400%` = 4 cores on any CPU count. Memory limits use cgroups v2 `memory.high` (soft, no OOM kill) by default; `memory.max` only on explicit user request.

**Cognitive hooks:** `record_episode("resource_limit", context={"app": ..., "cpu": ..., "mem": ...})` so NORA learns your habitual limits and can pre-apply them ("you always throttle Chrome during builds — want me to do that automatically?").

**Landmines:**
- cgroups v2 unified hierarchy must be mounted at `/sys/fs/cgroup` (default on Debian 11+/Fedora 31+ with systemd 245+); detect and refuse gracefully on v1.
- `CPUQuota` > number-of-cores is valid (burst) — don't cap it; just warn if the user asks for more cores than exist.
- `memory.high` is a soft limit (reclaim pressure, no kill); `memory.max` is hard (OOM kill). Default to `high` and require explicit confirmation for `max`.
- App-to-PID resolution: use `/proc/<pid>/cmdline` + `cgroup` file to find the right process; if multiple instances exist, ask which one.

**Install:** Zero — `systemd-run` and cgroups v2 are present on every modern Debian/Fedora system.

**Demo:** `cargo build` pegs all 12 threads, fan spins up. Voice: "NORA, throttle the build to 6 cores." → `CPUQuota=600%` applied in ~80ms, fan slows. Voice: "How much memory is Chrome using?" → "Chrome's cgroup is at 1.2 GB RSS with a 2 GB soft cap." Voice: "What are the top resource hogs right now?" → ranked list by CPU + memory from live cgroup accounting.

---

### F8 — fanotify Security Watchdog

`fanotify` is a Linux kernel API that lets an unprivileged process intercept and audit every file access in a directory subtree — and optionally *deny* the access before it completes. NORA uses it to watch sensitive paths (`~/.ssh`, `~/.env`, `~/.gnupg`, `~/.config/nora/`) and alert immediately when any process reads or modifies them, with full PID/binary/parent-chain attribution. Goes further than F3's bpftrace (which is read-only observation): fanotify can block the access entirely while NORA asks you.

**Library:** `python-fanotify` (thin ctypes wrapper, no C build needed) or direct `ctypes` syscall to `fanotify_init` + `fanotify_mark`. A dedicated thread reads the event queue; results crossed into asyncio via `asyncio.Queue`.

**New files:**
- `nora/platform/linux/fanotify_watcher.py` — `FanotifyWatcher` class: `watch(path, events)`, `unwatch(path)`, async event stream. Runs in a daemon thread; no privilege required for `FAN_CLASS_NOTIF` (read-only audit). `FAN_CLASS_CONTENT` (deny-capable) requires `CAP_SYS_ADMIN` — uses the same privileged-helper pattern as F3.
- `nora/commands/security_watch.py`

**Registered actions (new category `security`):**
- `watch_sensitive(paths=None)` — risk low; defaults to `~/.ssh`, `~/.gnupg`, `~/.env*`; starts audit-only watcher
- `unwatch_sensitive()` — risk low
- `list_watched_paths()` — risk low
- `explain_last_access(path)` — risk low; LLM narrates the access chain (who → what → why likely)
- `block_path(path)` — risk high, requires privileged helper; any access attempt triggers a NORA confirmation prompt

**Cognitive hooks:** Every alert fires `record_episode("sensitive_file_access", context={"path": ..., "pid": ..., "binary": ...})`. After 3+ accesses by the same binary NORA proactively asks whether to whitelist or block it.

**Landmines:**
- `FAN_CLASS_NOTIF` works unprivileged for directories the user owns; system paths need the privileged helper.
- Recursive watching requires `FAN_MARK_FILESYSTEM` (kernel 4.20+) or per-directory marks — use the per-directory fallback for portability.
- High-frequency paths (e.g. `~/.config/pulse`) will flood the queue — allow-list by inode, not just path string.
- Deny mode (`FAN_CLASS_CONTENT`) holds the kernel syscall until NORA responds — must respond within 30s or the kernel auto-allows.

**Install:** `pip install python-fanotify` (or pure ctypes, zero apt deps).

**Demo:** Voice: "NORA, watch my SSH keys." → Thirty seconds later a script touches `~/.ssh/id_rsa`. NORA speaks: "Alert: `/usr/bin/python3` (PID 4821, parent: bash) just read your SSH private key. Want me to block it?" Voice: "Yes, block it." → fanotify deny mode activated; the next access is blocked at the kernel and NORA announces it.

---

### F9 — AMD pstate / Thermal Power Governor

Your Ryzen 5 5600H supports `amd_pstate_epp` — Linux's energy-performance preference hints that bypass the BIOS and write directly to MSR registers. This is a Linux-exclusive feature: Windows exposes a coarse "power plan" abstraction; Linux lets you set per-core EPP values, CPU frequency scaling governors, and (on supported boards) package TDP limits via `amd_energy` or `zenpower`. NORA turns this into a voice-controlled power profile that actually changes hardware behaviour, not just software scheduling.

**Library:** Direct `/sys/devices/system/cpu/cpu*/cpufreq/` writes via Python `open()` — no external tools. `subprocess` against `cpupower` for frequency range overrides. Thermal readings from `/sys/class/hwmon/hwmon*/temp*` and `/sys/class/powercap/` (RAPL) for real-time watt readings.

**New files:**
- `nora/platform/linux/power_governor.py` — EPP writer, governor switcher, RAPL reader, hwmon temperature poller.
- `nora/commands/power_voice.py`

**Registered actions (new category `power`):**
- `set_power_profile(profile)` — profiles: `performance`, `balanced`, `battery`, `quiet`; risk low
- `get_power_stats()` — risk low; current governor, EPP, package watts (RAPL), CPU temp, fan RPM
- `set_cpu_governor(governor)` — risk low; `performance | powersave | schedutil`
- `set_tdp(watts)` — risk medium; writes RAPL constraint via `/sys/class/powercap/`; requires write permission (polkit rule, one-time)
- `thermal_watch(threshold_c=85)` — risk low; alerts when any core exceeds threshold

**Profile definitions (sensible defaults for Ryzen 5600H):**

| Profile | Governor | EPP | RAPL |
|---|---|---|---|
| `performance` | performance | performance | uncapped |
| `balanced` | schedutil | balance_performance | 35W |
| `battery` | powersave | power | 15W |
| `quiet` | powersave | power | 10W (fan silent) |

**Cognitive hooks:** `predict_next_action` already tracks time-of-day + app context; proactive engine offers "switch to battery profile?" when lid angle sensor or AC disconnect event fires (via udev rule).

**Landmines:**
- `amd_pstate_epp` requires kernel 6.3+ and `amd_pstate=active` kernel cmdline; detect via `/sys/devices/system/cpu/amd_pstate/status` and fall back to `acpi-cpufreq` governor-only control.
- RAPL writes require `/sys/class/powercap/intel-rapl*/constraint_*/power_limit_uw` — on AMD this is `amd_energy` or `zenpower` DKMS module; detect and degrade gracefully to governor-only if absent.
- Fan control varies wildly by board (ASUS, Lenovo, HP each have different WMI interfaces or EC registers) — expose thermal *read* universally; fan *write* only if `nbfc-linux` or a known WMI path is detected.

**Install:** `sudo apt install linux-cpupower`; RAPL write permission via one polkit rule in `nora-linux-setup`.

**Demo:** Plugged in, building Rust project, fans loud. Voice: "NORA, switch to quiet mode." → EPP set to `power`, RAPL capped at 10W, governor set to `powersave`. Fans drop within 5 seconds. Voice: "What's my package power draw?" → "Currently 9.8 watts. CPU is at 42°C." Unplug charger. NORA proactively: "You unplugged — want me to switch to battery profile?"

---

## Cross-Cutting Plumbing

**New directory layout:**
```
nora/platform/linux/   # OS shims (atspi, dbus, snapshots, pipewire, compositor)
nora/observability/    # eBPF helper + scripts
nora/commands/         # 5 new modules (atspi_actions, dbus_remote, why_engine, time_travel, ambient_linux)
                       # + F6–F9: systemd_voice, resource_governor, security_watch, power_voice
```

**Three small additive hooks in existing files** (not rewrites — observer-pattern extension points, ≤10 LOC each, no behavior change without a subscriber):
- `nora/anomaly_watchdog.py` — `register_drill_down(cb)` for F3
- `nora/reversible.py` — `register_pre_action_hook(cb)` for F4
- `nora/wakeword.py` — `register_on_wake_callback(cb)` for F5

These are unavoidable because the relevant signals (anomaly-fired, action-recorded, wake-detected) only exist inside those modules. Each is a strict superset addition.

**One unified setup script** `nora-linux-setup` (new `pyproject.toml` console-script entry) wraps the three privileged installers (`nora-install-bpf`, `nora-install-snap`, the ydotool udev rule) behind one polkit-mediated flow, so the user runs one command to bootstrap.

**Critical files to read/modify:**
- `/home/aadit/Projects/JARVIS/nora/command_engine.py` — `@register` API reference (read only).
- `/home/aadit/Projects/JARVIS/nora/anomaly_watchdog.py` — add `register_drill_down`.
- `/home/aadit/Projects/JARVIS/nora/reversible.py` — add `register_pre_action_hook`.
- `/home/aadit/Projects/JARVIS/nora/wakeword.py` — add `register_on_wake_callback`.
- `/home/aadit/Projects/JARVIS/nora/cognitive_memory.py` — read-only; use `record_episode`, `predict_next_action` public APIs.

---

## Verification

End-to-end demo plan (also serves as acceptance test):

1. **F1.** Fresh Firefox window. Voice: "click the address bar, type example.com, press enter, then click the first heading." Expect: four deterministic AT-SPI hits, zero VLM calls, < 2s total.
2. **F2.** Voice: "discover dbus for Spotify" → blessed wrapper added. Voice: "pause Spotify, list Bluetooth devices, switch wifi to <ssid>." Expect: three D-Bus services, one sentence, < 3s.
3. **F3.** Run `stress-ng --cpu 4` in a background tab. Voice: "why is my fan loud?" Expect: bpftrace runs, LLM names `stress-ng` in the explanation, < 7s end-to-end.
4. **F4.** Voice: "snapshot now labeled refactor-start." Modify three files. Voice: "roll back to refactor-start." Expect: btrfs subvolume swap, files reverted, editor reload notice. Then voice: "pause this session." Reboot. Voice: "resume." Expect: CRIU restore brings back terminals.
5. **F5.** Spotify playing. Say "Hey NORA" → measure ducking latency (< 200ms). Voice: "focus mode for writing." Expect: Sway/Hyprland retile + notifications muted via D-Bus.
6. **F6.** Kill nginx with a bad config. Voice: "why did nginx fail?" Expect: journald query, LLM names the bind error, < 5s. Voice: "check boot time." Expect: ranked slowest units from `systemd-analyze blame`.
7. **F7.** Run `cargo build` (pegs all cores). Voice: "throttle the build to 6 cores." Expect: `CPUQuota=600%` applied via transient cgroup, CPU usage drops within one scheduler tick. Voice: "what are the top resource hogs?" Expect: ranked cgroup list.
8. **F8.** Voice: "watch my SSH keys." Touch `~/.ssh/id_rsa` from a separate terminal. Expect: NORA speaks an alert within 1 second naming the PID and binary, no polling delay.
9. **F9.** On battery. Voice: "switch to quiet mode." Expect: EPP set to `power`, RAPL capped, fan RPM drops within 5 seconds. Voice: "what's my package power draw?" Expect: live watt reading from RAPL.

**Regression tests** for the three hooked files:
- Run NORA with no F3/F4/F5 modules installed → existing watchdog/reversible/wakeword behavior unchanged.
- Verify `register_*` returns None and the call site behaves identically when no subscriber registered.

---

## Out of Scope (Explicit)

- Porting existing Windows command modules (`ask_claude.py`, `system_control.py`, `notifications.py`, `music.py`, `app_launcher.py`) to Linux — separate effort, the user said no rewrites.
- macOS support for any of these features — the entire premise is "things only Linux can do."
- Replacing Whisper/Groq/Claude in the existing pipeline — F1's VLM is a local fallback for screen targeting only, not a general-purpose LLM swap.
