"""What the home-core backup picks up, and that live databases copy whole.

The backup is only worth having if a restore brings NORA back: the secrets
(.env, Google OAuth) and every state file, and nothing that is merely
regenerable. SQLite files go through the online backup API because NORA keeps
writing while the timer runs; the test holds a write transaction open to prove
the copy is a consistent database rather than a torn file.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "backup_state", Path(__file__).resolve().parent.parent / "deploy" / "backup_state.py")
backup_state = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backup_state)


def _touch(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class CollectTest(unittest.TestCase):
    def test_secrets_and_state_in_logs_and_code_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in (".env", "google_token.json",
                         "client_secret_123-abc.apps.googleusercontent.com.json",
                         "nora_memory.json", "nora_audit_log.jsonl", "nora_sessions.db",
                         "nora_cognitive_db/chroma.sqlite3",
                         "nora_cognitive_db/574fa31a/data_level0.bin",
                         # not state:
                         "nora.log", "config.yaml", "main.py", "nora/memory.py",
                         ".venv/lib/nora_fake.json"):
                _touch(root / name)
            got = {str(p) for p in backup_state.collect(root)}
        self.assertEqual(got, {
            ".env", "google_token.json",
            "client_secret_123-abc.apps.googleusercontent.com.json",
            "nora_memory.json", "nora_audit_log.jsonl", "nora_sessions.db",
            "nora_cognitive_db/chroma.sqlite3",
            "nora_cognitive_db/574fa31a/data_level0.bin",
        })


class SnapshotTest(unittest.TestCase):
    def test_live_database_is_copied_consistently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, staging = Path(tmp) / "repo", Path(tmp) / "stage"
            root.mkdir(), staging.mkdir()
            _touch(root / ".env", "NORA_API_TOKEN=secret\n")
            db = root / "nora_sessions.db"
            writer = sqlite3.connect(db)
            writer.execute("CREATE TABLE turns (text TEXT)")
            writer.execute("INSERT INTO turns VALUES ('committed')")
            writer.commit()
            # An uncommitted write in flight while the backup runs.
            writer.execute("INSERT INTO turns VALUES ('in flight')")
            try:
                backup_state.snapshot(root, staging)
            finally:
                writer.rollback()
                writer.close()

            copy = sqlite3.connect(staging / "nora_sessions.db")
            try:
                rows = copy.execute("SELECT text FROM turns").fetchall()
                self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            finally:
                copy.close()
            self.assertEqual(rows, [("committed",)])
            self.assertEqual((staging / ".env").read_text(), "NORA_API_TOKEN=secret\n")


if __name__ == "__main__":
    unittest.main()
