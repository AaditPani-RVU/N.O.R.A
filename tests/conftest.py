"""Suite-wide isolation: no test writes NORA's live state in the repo root.

The core store, the audit log, the session index and the model-router log all
point into one temporary directory for the whole run. `nora.store` reads
`NORA_STORE_PATH` on every connect, so setting it here is enough; and
`jobs.reset_for_tests` and friends refuse to wipe the default path as a
second line of defence.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="nora-store-test-")
os.environ["NORA_STORE_PATH"] = os.path.join(_tmp, "nora_core.db")

# The audit log appends to a JSONL file in the repo root; redirect it too.
from nora import audit_log as _audit_log  # noqa: E402
_audit_log._LOG_PATH = Path(_tmp) / "nora_audit_log.jsonl"

# Every turn indexes its utterances (dialogue → session_index), and every model
# call is logged; neither should land in the live files.
from nora import model_router as _model_router, session_index as _session_index  # noqa: E402
_session_index._use_path_for_tests(Path(_tmp) / "nora_sessions.db")
_model_router._LOG_PATH = Path(_tmp) / "nora_model_router_log.jsonl"
