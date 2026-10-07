"""nora doctor: is everything NORA depends on still working?

    python -m nora.doctor               run every check and print the findings
    python -m nora.doctor google-login  sign in to Google again (opens a browser)

`nora.health` answers "can this subsystem start?" in under a second, from
imports and files. The doctor answers the slower question the audit kept
running into: does it still *work*? A removed model, an expired Google token
or a command module that failed to import all look fine to `health` and fail
only when the user asks for something, with nothing to say why.

Every check costs nothing against a free tier: model lists come from each
provider's `/models` endpoint, and tokens are refreshed, not used.

Running inside NORA, `start()` runs the checks a few minutes after startup and
then daily. A check that starts failing is told to the user once, with its
fix, on the phone if one is connected and otherwise through the laptop's
speaker; it is not repeated until it has recovered and failed again.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nora.config import get_config

logger = logging.getLogger("nora.doctor")

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "nora_doctor_state.json"

FIRST_RUN_DELAY_SEC = 180
INTERVAL_SEC = 24 * 3600
PHONE_STALE_SEC = 2 * 86400


@dataclass
class Finding:
    check: str            # stable key: "model:groq/compound-mini", "google", ...
    ok: bool
    detail: str
    fix: str = ""


# ── checks ───────────────────────────────────────────────────────────────────

def _model_lists() -> dict[tuple[str, str], set[str] | str]:
    """{(base_url, key_env): model ids, or an error string}, one call per provider."""
    import requests

    roles = get_config().get("llm_router", {}).get("roles", {}) or {}
    wanted = {(c.get("base_url", ""), c.get("api_key_env", ""))
              for cands in roles.values() for c in cands or []
              if c.get("provider") != "ollama" and c.get("base_url")}
    out: dict[tuple[str, str], set[str] | str] = {}
    for base, key_env in wanted:
        key = os.environ.get(key_env, "")
        if not key:
            out[(base, key_env)] = f"{key_env} is not set"
            continue
        try:
            r = requests.get(base.rstrip("/") + "/models",
                             headers={"Authorization": f"Bearer {key}"}, timeout=15)
            if r.status_code in (401, 403):
                out[(base, key_env)] = f"{key_env} was refused (HTTP {r.status_code})"
            elif r.status_code != 200:
                out[(base, key_env)] = f"HTTP {r.status_code} listing models"
            else:
                out[(base, key_env)] = {m.get("id", "") for m in r.json().get("data", [])}
        except Exception as e:
            out[(base, key_env)] = f"unreachable: {type(e).__name__}"
    return out


def check_models() -> list[Finding]:
    """Every model the router may call is still offered by its provider."""
    roles = get_config().get("llm_router", {}).get("roles", {}) or {}
    lists = _model_lists()
    findings: list[Finding] = []
    for role, cands in roles.items():
        for c in cands or []:
            if c.get("provider") == "ollama" or not c.get("base_url"):
                continue
            listed = lists.get((c["base_url"], c.get("api_key_env", "")))
            model, name = c["model"], c.get("name", c["model"])
            key = f"model:{role}:{name}"
            if isinstance(listed, str):
                findings.append(Finding(key, False, f"{role} model {model}: {listed}",
                                        f"check {c.get('api_key_env')} in .env"
                                        if "set" in listed or "refused" in listed else ""))
            elif model not in listed:
                findings.append(Finding(
                    key, False, f"{model} ({c.get('provider')}) is no longer offered; "
                                f"the {role} role still tries it first" if cands.index(c) == 0
                    else f"{model} ({c.get('provider')}) is no longer offered ({role} role)",
                    f"remove {name} from llm_router.roles.{role} in config.yaml"))
            else:
                findings.append(Finding(key, True, f"{role}: {model} available"))
    return findings


def check_google() -> list[Finding]:
    """The Google token still refreshes (calendar and Gmail)."""
    token = ROOT / "google_token.json"
    if not token.exists():
        return [Finding("google", False, "Google isn't signed in, so calendar and Gmail are off",
                        "python -m nora.doctor google-login")]
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return [Finding("google", False, "the Google client libraries aren't installed",
                        "pip install google-api-python-client google-auth-oauthlib")]
    try:
        creds = Credentials.from_authorized_user_file(str(token))
        if not creds.refresh_token:
            return [Finding("google", False, "the Google token has no refresh token",
                            "python -m nora.doctor google-login")]
        creds.refresh(Request())
    except RefreshError as e:
        reason = "expired or was revoked" if "invalid_grant" in str(e) else str(e)[:80]
        return [Finding("google", False, f"Google sign-in {reason}, so calendar and Gmail don't work",
                        "python -m nora.doctor google-login")]
    except Exception as e:
        return [Finding("google", False, f"Google token check failed: {type(e).__name__}: {e}"[:160])]
    return [Finding("google", True, "Google token refreshes")]


def check_spotify() -> list[Finding]:
    from nora import spotify_user
    if not spotify_user.is_logged_in():
        return [Finding("spotify", False, "Spotify isn't signed in, so your playlists can't be played",
                        "python -m nora.spotify_user login")]
    try:
        spotify_user._access_token()
    except Exception as e:
        return [Finding("spotify", False, f"Spotify sign-in no longer works: {str(e)[:100]}",
                        "python -m nora.spotify_user login")]
    return [Finding("spotify", True, "Spotify token refreshes")]


def check_hub() -> list[Finding]:
    cfg = get_config().get("hub", {}) or {}
    if not cfg.get("enabled"):
        return []
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 8770))
    # A plain HTTP request, answered with a 404: a bare connect-and-close
    # makes the websockets server log a handshake traceback every time.
    try:
        with socket.create_connection((host, port), timeout=3) as s:
            s.sendall(b"GET /doctor HTTP/1.1\r\nHost: " + host.encode() + b"\r\nConnection: close\r\n\r\n")
            answered = s.recv(16).startswith(b"HTTP/1.")
    except OSError as e:
        return [Finding("hub", False, f"the device hub isn't listening on {host}:{port} ({e.strerror or e})",
                        "systemctl --user restart nora")]
    if not answered:
        return [Finding("hub", False, f"something on {host}:{port} isn't answering like the hub",
                        "systemctl --user restart nora")]
    return [Finding("hub", True, f"hub answering on {host}:{port}")]


def check_phone() -> list[Finding]:
    """Each approved device has been seen recently."""
    from nora.hub import registry
    findings = []
    for d in registry.listing():
        if not d.approved_at or d.revoked_at:
            continue
        age = time.time() - (d.last_seen or d.approved_at)
        ok = age < PHONE_STALE_SEC
        fix = ("" if ok else
               f"open the NORA app on the {d.name} and check it shows connected, or if it is "
               f"no longer used, python -m nora.hub revoke {d.id}")
        findings.append(Finding(f"device:{d.id}", ok, f"{d.name} last connected {_ago(age)}", fix))
    return findings


def check_commands() -> list[Finding]:
    from nora import command_engine
    if not command_engine.get_available_actions():
        command_engine.discover_commands()
    failures = command_engine.load_failures()
    if not failures:
        return [Finding("commands", True, f"{len(command_engine.get_available_actions())} commands loaded")]
    return [Finding(f"commands:{mod}", False, f"{mod} failed to load, so its commands are missing ({err[:80]})",
                    "see the import error above; the module must import without a display")
            for mod, err in failures.items()]


CHECKS: list[tuple[str, Callable[[], list[Finding]]]] = [
    ("models", check_models),
    ("google", check_google),
    ("spotify", check_spotify),
    ("hub", check_hub),
    ("devices", check_phone),
    ("commands", check_commands),
]


def run_checks() -> list[Finding]:
    findings: list[Finding] = []
    for name, fn in CHECKS:
        try:
            findings.extend(fn())
        except Exception as e:
            findings.append(Finding(name, False, f"the {name} check itself failed: {type(e).__name__}: {e}"[:200]))
    return findings


def _ago(sec: float) -> str:
    if sec < 3600:
        return f"{int(sec // 60)} minutes ago"
    if sec < 2 * 86400:
        return f"{int(sec // 3600)} hours ago"
    return f"{int(sec // 86400)} days ago"


# ── telling the user, once ───────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def news(findings: list[Finding], state: dict) -> tuple[list[Finding], dict]:
    """The failures not yet told, and the state to keep. A check that passes
    is forgotten, so failing again later is news again."""
    now = time.time()
    failing = {f.check: f for f in findings if not f.ok}
    fresh = [f for k, f in failing.items() if k not in state]
    kept = {k: state.get(k, {"since": now}) for k in failing}
    return fresh, kept


def tell(fresh: list[Finding], speak: Callable[..., None] | None) -> str:
    if not fresh:
        return ""
    lines = [f"{f.detail}." + (f" To fix it: {f.fix}." if f.fix else "") for f in fresh[:3]]
    more = f" And {len(fresh) - 3} more; run python -m nora.doctor." if len(fresh) > 3 else ""
    text = ("Something stopped working. " if len(fresh) == 1 else "A few things stopped working. ") \
        + " ".join(lines) + more
    from nora import delivery
    if not delivery.broadcast(text, kind="notice") and speak is not None:
        speak(text, mood="info")
    logger.warning("doctor: %s", text)
    return text


def check_and_tell(speak: Callable[..., None] | None = None) -> list[Finding]:
    findings = run_checks()
    fresh, state = news(findings, _load_state())
    tell(fresh, speak)
    try:
        STATE_PATH.write_text(json.dumps(state, indent=1))
    except OSError as e:
        logger.debug("doctor state not kept: %s", e)
    bad = [f for f in findings if not f.ok]
    logger.info("doctor: %d checks, %d failing%s", len(findings), len(bad),
                "".join(f"; {f.check}" for f in bad))
    return findings


_thread: threading.Thread | None = None


def start(speak_callback: Callable[..., None] | None = None) -> None:
    """Run the checks a few minutes after startup, then daily."""
    global _thread
    if not (get_config().get("doctor", {}) or {}).get("enabled", True) or _thread is not None:
        return

    def loop() -> None:
        time.sleep(FIRST_RUN_DELAY_SEC)
        while True:
            try:
                check_and_tell(speak_callback)
            except Exception as e:
                logger.warning("doctor run failed: %s", e)
            time.sleep(INTERVAL_SEC)

    _thread = threading.Thread(target=loop, daemon=True, name="nora-doctor")
    _thread.start()


# ── command line ─────────────────────────────────────────────────────────────

def _load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _google_login() -> int:
    """Throw away the dead token and run the browser sign-in again."""
    token = ROOT / "google_token.json"
    if token.exists():
        token.rename(token.with_suffix(".json.old"))
    from nora.commands.google_services import _get_creds
    _get_creds()
    print("Signed in. Calendar and Gmail work again.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _load_env()
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    if argv[:1] == ["google-login"]:
        return _google_login()
    findings = run_checks()
    for f in sorted(findings, key=lambda f: f.ok):
        print(f"{'ok  ' if f.ok else 'FAIL'} {f.detail}" + (f"\n       fix: {f.fix}" if f.fix else ""))
    bad = sum(not f.ok for f in findings)
    print(f"\n{len(findings)} checks, {bad} failing")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
