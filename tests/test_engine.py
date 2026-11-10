# SPDX-License-Identifier: MIT
"""引擎测试：本地服务器模拟断流、签名过期、限流、无 Range 等场景。"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ghdl.cli import parse_size
from ghdl.engine import DownloadJob
from ghdl.model import plan_chunks, sanitize_filename

from .server import STORE, TestServer


def blob(size: int, seed: int = 1) -> bytes:
    return random.Random(seed).randbytes(size)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PlanTests(unittest.TestCase):
    def test_plan_covers_whole_file(self) -> None:
        chunks = plan_chunks(10_000_000, 8, 1024 * 1024)
        self.assertEqual(chunks[0].start, 0)
        self.assertEqual(chunks[-1].end, 10_000_000)
        for a, b in zip(chunks, chunks[1:]):
            self.assertEqual(a.end, b.start)
        self.assertEqual(sum(c.length for c in chunks), 10_000_000)

    def test_plan_default_sizes(self) -> None:
        chunks = plan_chunks(1024 * 1024 * 1024, 8)
        self.assertGreaterEqual(len(chunks), 24)
        self.assertLessEqual(len(chunks), 40)
        self.assertLessEqual(max(c.length for c in chunks), 64 * 1024 * 1024)

    def test_plan_tiny_file(self) -> None:
        chunks = plan_chunks(10, 4)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].length, 10)

    def test_sanitize_filename(self) -> None:
        self.assertEqual(sanitize_filename('a/b\\c?.zip'), "c_.zip")
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")
        self.assertTrue(sanitize_filename("x" * 300).endswith("x"))

    def test_parse_size(self) -> None:
        self.assertEqual(parse_size("8M"), 8 * 1024 * 1024)
        self.assertEqual(parse_size("1.5GiB"), int(1.5 * 1024 ** 3))
        self.assertEqual(parse_size("1024"), 1024)


class DownloadTests(unittest.TestCase):
    server: TestServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = TestServer().__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.__exit__(None, None, None)

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _job(self, url: str, out: Path, **kw) -> DownloadJob:
        params = dict(
            connections=4,
            chunk_size=256 * 1024,
            timeout=5,
            retry_base=0.01,
            retry_max=0.05,
            api_digest=False,
        )
        params.update(kw)
        return DownloadJob(url, out, **params)

    # ------------------------------------------------------------------
    def test_normal_segmented_download(self) -> None:
        name = "normal.bin"
        data = blob(2 * 1024 * 1024, 11)
        STORE.put(name, data)
        out = self.dir / "normal.bin"
        job = self._job(f"{self.server.base}/file/{name}", out)
        result = job.run()
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(out.read_bytes(), data)
        self.assertGreaterEqual(STORE.ranges[name], 4)
        self.assertFalse(Path(str(out) + ".ghdlpart.json").exists())
        self.assertFalse(Path(str(out) + ".ghdlpart").exists())

    def test_flaky_connection_recovers(self) -> None:
        name = "flaky.bin"
        data = blob(1024 * 1024, 12)
        STORE.put(name, data)
        STORE.drop_after[name] = 40000
        out = self.dir / "flaky.bin"
        job = self._job(f"{self.server.base}/flaky/{name}", out, chunk_size=128 * 1024, connections=3)
        result = job.run()
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(out.read_bytes(), data)
        self.assertGreater(STORE.drops[name], 0)
        self.assertGreater(result.retries, 0)

    def test_signed_url_expiry_refresh(self) -> None:
        name = "expire.bin"
        data = blob(4 * 1024 * 1024, 13)
        STORE.put(name, data)
        STORE.token_ttl = 0.2
        STORE.delay[name] = 0.02
        out = self.dir / "expire.bin"
        job = self._job(f"{self.server.base}/expire/{name}", out, chunk_size=256 * 1024, connections=2)
        result = job.run()
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(out.read_bytes(), data)
        self.assertGreater(STORE.expired[name], 0)

    def test_no_range_falls_back_to_stream(self) -> None:
        name = "norange.bin"
        data = blob(512 * 1024, 14)
        STORE.put(name, data)
        out = self.dir / "norange.bin"
        job = self._job(f"{self.server.base}/norange/{name}", out)
        result = job.run()
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(out.read_bytes(), data)
        self.assertFalse(Path(str(out) + ".ghdlpart.json").exists())

    def test_chunked_without_length(self) -> None:
        name = "chunked.bin"
        data = blob(400 * 1024, 15)
        STORE.put(name, data)
        out = self.dir / "chunked.bin"
        job = self._job(f"{self.server.base}/chunked/{name}", out)
        result = job.run()
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(out.read_bytes(), data)

    def test_parts_mode_assembles(self) -> None:
        name = "parts.bin"
        data = blob(2 * 1024 * 1024, 16)
        STORE.put(name, data)
        out = self.dir / "parts.bin"
        job = self._job(f"{self.server.base}/file/{name}", out, part_mode=True,
                        chunk_size=128 * 1024, connections=4)
        result = job.run()
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(out.read_bytes(), data)
        self.assertFalse((self.dir / "parts.bin.ghdlparts").exists())

    def test_sha256_mismatch_keeps_data(self) -> None:
        name = "badhash.bin"
        data = blob(512 * 1024, 17)
        STORE.put(name, data)
        out = self.dir / "badhash.bin"
        job = self._job(f"{self.server.base}/file/{name}", out, sha256="0" * 64)
        result = job.run()
        self.assertEqual(result.status, "error")
        self.assertIn("SHA-256", result.error or "")
        self.assertFalse(out.exists())
        self.assertTrue(Path(str(out) + ".ghdlpart").exists())

    def test_pause_then_resume(self) -> None:
        name = "resume.bin"
        data = blob(4 * 1024 * 1024, 18)
        STORE.put(name, data)
        STORE.delay[name] = 0.01
        out = self.dir / "resume.bin"
        url = f"{self.server.base}/file/{name}"

        job = self._job(url, out, connections=2, chunk_size=512 * 1024)
        box: dict = {}
        thread = threading.Thread(target=lambda: box.setdefault("r", job.run()), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snap = job.snapshot()
            if snap.downloaded > len(data) * 0.2:
                break
            time.sleep(0.01)
        job.request_stop()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "暂停超时")
        self.assertEqual(box["r"].status, "paused")
        self.assertFalse(out.exists())
        self.assertTrue(Path(str(out) + ".ghdlpart.json").exists())
        state = json.loads(Path(str(out) + ".ghdlpart.json").read_text(encoding="utf-8"))
        done_before = sum(c[2] for c in state["chunks"])
        self.assertGreater(done_before, 0)

        job2 = self._job(url, out, connections=2, chunk_size=512 * 1024)
        result2 = job2.run()
        self.assertEqual(result2.status, "done", result2.error)
        self.assertEqual(out.read_bytes(), data)
        self.assertLess(result2.downloaded, len(data))
        self.assertGreater(result2.downloaded, 0)

    def test_existing_file_skipped(self) -> None:
        name = "exists.bin"
        data = blob(256 * 1024, 19)
        STORE.put(name, data)
        out = self.dir / "exists.bin"
        out.write_bytes(data)
        job = self._job(f"{self.server.base}/file/{name}", out, sha256=sha256(data))
        result = job.run()
        self.assertEqual(result.status, "skipped")
        self.assertEqual(out.read_bytes(), data)

    def test_server_outage_then_recovery(self) -> None:
        name = "gate.bin"
        data = blob(256 * 1024, 20)
        STORE.put(name, data)
        STORE.gate[name] = 3
        out = self.dir / "gate.bin"
        job = self._job(f"{self.server.base}/gate/{name}", out, retry_base=0.02, retry_max=0.05)
        result = job.run()
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(out.read_bytes(), data)
        self.assertGreater(result.retries, 0)

    def test_force_redownloads_existing(self) -> None:
        name = "force.bin"
        data = blob(256 * 1024, 21)
        STORE.put(name, data)
        out = self.dir / "force.bin"
        url = f"{self.server.base}/file/{name}"
        self.assertEqual(self._job(url, out).run().status, "done")
        again = self._job(url, out)
        self.assertEqual(again.run().status, "skipped")
        forced = self._job(url, out, force=True)
        self.assertEqual(forced.run().status, "done")
        self.assertEqual(out.read_bytes(), data)

    def test_parts_mode_pause_resume(self) -> None:
        name = "parts_resume.bin"
        data = blob(4 * 1024 * 1024, 22)
        STORE.put(name, data)
        STORE.delay[name] = 0.01
        out = self.dir / "parts_resume.bin"
        url = f"{self.server.base}/file/{name}"

        job = self._job(url, out, connections=2, chunk_size=512 * 1024, part_mode=True)
        box: dict = {}
        thread = threading.Thread(target=lambda: box.setdefault("r", job.run()), daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if job.snapshot().downloaded > len(data) * 0.2:
                break
            time.sleep(0.01)
        job.request_stop()
        thread.join(15)
        self.assertEqual(box["r"].status, "paused")
        self.assertTrue((self.dir / "parts_resume.bin.ghdlparts").exists())

        job2 = self._job(url, out, connections=2, chunk_size=512 * 1024, part_mode=True)
        result2 = job2.run()
        self.assertEqual(result2.status, "done", result2.error)
        self.assertEqual(out.read_bytes(), data)
        self.assertFalse((self.dir / "parts_resume.bin.ghdlparts").exists())


if __name__ == "__main__":
    unittest.main()
