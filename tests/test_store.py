"""The core store: one SQLite file replacing the per-module JSON files.

What has to hold:
  * the schema migrates once, and re-opening a migrated file is a no-op;
  * the legacy `nora_jobs.json` / `nora_schedules.json` / `nora_tasks.json`
    are imported exactly once and renamed out of the way — and a file that
    cannot be read is left alone for a human, not swallowed;
  * jobs, schedules and tasks survive a restart with the same meaning they
    had in JSON (interrupted jobs fail, recurring schedules roll forward);
  * two *processes* writing at once lose nothing — the reason the store exists.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nora import jobs, scheduler, store, task_ledger


class _FreshStore(unittest.TestCase):
    """Each test gets its own database file in its own directory."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="nora-store-"))
        self.db = self.dir / "nora_core.db"
        patcher = mock.patch.dict(os.environ, {"NORA_STORE_PATH": str(self.db)})
        patcher.start()
        self.addCleanup(patcher.stop)


class MigrationTest(_FreshStore):
    def test_creates_schema_at_latest_version(self) -> None:
        conn = store.connect()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, len(store._MIGRATIONS))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"jobs", "schedules", "tasks"} <= tables)

    def test_wal_mode(self) -> None:
        mode = store.connect().execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_reopening_does_not_remigrate(self) -> None:
        store.connect()
        store._initialised.discard(str(self.db))
        with mock.patch.object(store.logger, "info") as info:
            # A fresh thread-local connection forces the init path again.
            store._local.conns.pop(str(self.db))
            store.connect()
        self.assertFalse(any("migrated" in str(c) for c in info.call_args_list))

    def test_reset_refuses_the_live_store(self) -> None:
        with mock.patch.dict(os.environ, {"NORA_STORE_PATH": ""}):
            with self.assertRaises(RuntimeError):
                store.reset_for_tests()


class LegacyImportTest(_FreshStore):
    def _write(self, name: str, payload) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_imports_all_three_and_renames(self) -> None:
        now = time.time()
        jobs_file = self._write("nora_jobs.json", {"jobs": [{
            "id": "abc123", "title": "your question about rust", "kind": "answer",
            "status": "done", "result": "It's fine.", "error": "",
            "created_at": now - 60, "started_at": now - 59, "finished_at": now - 50,
            "delivered": True, "deliver": True, "stale": False,
        }]})
        sched_file = self._write("nora_schedules.json", {"schedules": [{
            "id": "s1", "spec": "every day at 7", "what": "give me the brief",
            "next_run": now + 3600, "recurring": True, "interval_sec": 0.0,
            "daily_at": [7, 0], "weekday": None, "enabled": True,
            "created_at": now, "last_run": 0.0, "run_count": 0,
        }]})
        tasks_file = self._write("nora_tasks.json", {"t1": {
            "id": "t1", "title": "submit assignment", "status": "open", "notes": "",
            "log": [{"ts": now, "entry": "started"}], "associated_files": ["a.pdf"],
            "commands": [], "created_at": now, "updated_at": now, "closed_at": None,
        }})

        store.connect()

        for f in (jobs_file, sched_file, tasks_file):
            self.assertFalse(f.exists(), f"{f.name} not renamed")
            self.assertTrue(f.with_name(f.name + ".migrated").exists())

        job = jobs.get("abc123")
        self.assertEqual(job.result, "It's fine.")
        self.assertTrue(job.delivered)

        task = task_ledger.get_task("t1")
        self.assertEqual(task["log"][0]["entry"], "started")
        self.assertEqual(task["associated_files"], ["a.pdf"])

        row = store.query("SELECT daily_at, recurring FROM schedules WHERE id='s1'")[0]
        self.assertEqual(json.loads(row["daily_at"]), [7, 0])
        self.assertEqual(row["recurring"], 1)

    def test_import_runs_once(self) -> None:
        self._write("nora_tasks.json", {"t1": {
            "id": "t1", "title": "x", "status": "open", "created_at": 1, "updated_at": 1}})
        store.connect()
        # Same name reappears (e.g. an old backup restored beside the db):
        # INSERT OR IGNORE keeps the row count at one.
        self._write("nora_tasks.json", {"t1": {
            "id": "t1", "title": "x", "status": "open", "created_at": 1, "updated_at": 1}})
        store._initialised.discard(str(self.db))
        store._local.conns.pop(str(self.db))
        store.connect()
        self.assertEqual(len(task_ledger.get_recent_tasks()), 1)

    def test_unreadable_file_is_left_in_place(self) -> None:
        bad = self.dir / "nora_jobs.json"
        bad.write_text("{ not json", encoding="utf-8")
        store.connect()
        self.assertTrue(bad.exists())
        self.assertFalse(bad.with_name("nora_jobs.json.migrated").exists())


class RestartSemanticsTest(_FreshStore):
    def test_interrupted_job_is_failed_and_never_spoken(self) -> None:
        jobs._insert(jobs.Job(id="run1", title="long thing", status=jobs.STATUS_RUNNING))
        jobs._load()
        job = jobs.get("run1")
        self.assertEqual(job.status, jobs.STATUS_FAILED)
        self.assertEqual(job.error, "Interrupted by shutdown.")
        self.assertTrue(job.stale)
        self.assertTrue(job.delivered)
        self.assertEqual(jobs.pending(), [])

    def test_history_is_trimmed_but_live_jobs_are_kept(self) -> None:
        live = jobs.Job(id="live", title="still going", created_at=0.0)
        jobs._insert(live)
        for i in range(jobs._HISTORY_LIMIT + 10):
            jobs._insert(jobs.Job(id=f"d{i}", title=f"done {i}",
                                  status=jobs.STATUS_DONE, created_at=1.0 + i))
        done = store.query("SELECT COUNT(*) FROM jobs WHERE status='done'")[0][0]
        self.assertLessEqual(done, jobs._HISTORY_LIMIT)
        self.assertIsNotNone(jobs.get("live"))

    def test_schedule_survives_restart(self) -> None:
        scheduler._schedules.clear()
        scheduler._loaded = True
        sched = scheduler.add("every day at 7", "give me the brief")

        scheduler._schedules.clear()
        scheduler._loaded = False
        listed = scheduler.listing()
        self.assertEqual([s.id for s in listed], [sched.id])
        self.assertEqual(listed[0].daily_at, [7, 0])
        self.assertTrue(listed[0].recurring)

    def test_missed_recurring_schedule_rolls_forward(self) -> None:
        scheduler._schedules.clear()
        scheduler._loaded = True
        sched = scheduler.add("every 30 minutes", "check the build")
        sched.next_run = time.time() - 7200
        scheduler._write(sched)

        scheduler._schedules.clear()
        scheduler._loaded = False
        [reloaded] = scheduler.listing()
        self.assertGreater(reloaded.next_run, time.time())

    def test_cancelled_schedule_is_not_resurrected_by_a_late_write(self) -> None:
        scheduler._schedules.clear()
        scheduler._loaded = True
        sched = scheduler.add("in 1 hour", "stretch")
        scheduler.remove(sched.id)
        scheduler._write(sched)
        self.assertEqual(store.query("SELECT COUNT(*) FROM schedules")[0][0], 0)

    def test_task_round_trip(self) -> None:
        tid = task_ledger.create_task("submit assignment", associated_files=["a.pdf"])
        self.assertTrue(task_ledger.append_log(tid, "drafted intro"))
        self.assertTrue(task_ledger.update_task(tid, status="in_progress", bogus=1))
        [task] = task_ledger.get_open_tasks()
        self.assertEqual(task["status"], "in_progress")
        self.assertEqual(task["log"][0]["entry"], "drafted intro")
        self.assertNotIn("bogus", task)
        self.assertTrue(task_ledger.close_task(tid))
        self.assertEqual(task_ledger.get_open_tasks(), [])
        self.assertEqual(task_ledger.task_summary(), {"closed": 1})
        self.assertFalse(task_ledger.close_task("nope"))
        self.assertEqual(task_ledger.find_tasks("ASSIGN")[0]["id"], tid)


def _writer(db: str, prefix: str, n: int) -> None:
    os.environ["NORA_STORE_PATH"] = db
    from nora import task_ledger as tl
    for i in range(n):
        tid = tl.create_task(f"{prefix}-{i}")
        tl.append_log(tid, "x")


class CrossProcessTest(_FreshStore):
    def test_two_processes_lose_nothing(self) -> None:
        store.connect()   # migrate once up front
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_writer, args=(str(self.db), p, 40)) for p in "ab"]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        titles = {t["title"] for t in task_ledger.get_recent_tasks(n=1000)}
        self.assertEqual(len(titles), 80)
        logs = store.query("SELECT COUNT(*) FROM tasks WHERE log != '[]'")[0][0]
        self.assertEqual(logs, 80)


if __name__ == "__main__":
    unittest.main()
