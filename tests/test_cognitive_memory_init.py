"""Cognitive memory initialises once, however many threads ask at startup.

warm_up() and the pipeline's own warm-up reach _get_collections within the
same second. Before the lock, both built a chromadb.PersistentClient at once;
chromadb's shared client cache came out broken ("'RustBindingsAPI' object has
no attribute 'bindings'") and memory stayed degraded until the next restart.
Seen on the home core after a service restart.
"""
from __future__ import annotations

import sys
import threading
import time
import types
import unittest
from unittest import mock

from nora import cognitive_memory


class _SlowClient:
    built = 0

    def __init__(self, path: str) -> None:
        type(self).built += 1
        time.sleep(0.2)  # wide window for a second thread to walk in

    def get_or_create_collection(self, name, metadata=None):
        col = mock.Mock()
        col.count.return_value = 0
        return col


class ConcurrentInitTest(unittest.TestCase):
    def setUp(self) -> None:
        _SlowClient.built = 0
        fake = types.ModuleType("chromadb")
        fake.PersistentClient = _SlowClient
        patches = [
            mock.patch.dict(sys.modules, {"chromadb": fake}),
            mock.patch.object(cognitive_memory, "_chroma_client", None),
            mock.patch.object(cognitive_memory, "_episodes_col", None),
            mock.patch.object(cognitive_memory, "_knowledge_col", None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_two_startup_threads_build_one_client(self):
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(cognitive_memory._get_collections()))
            for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)

        self.assertEqual(_SlowClient.built, 1)
        self.assertEqual(len(results), 2)
        self.assertIs(results[0][0], results[1][0])
        self.assertIsNotNone(results[0][0])


if __name__ == "__main__":
    unittest.main()
