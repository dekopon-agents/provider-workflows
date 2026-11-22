import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from ci.size_report import (
    BASE_ARTIFACT, BOT, COMPONENT, DEPENDABOT, MARKER, MODULE, component_size, post_report,
)


HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40
OTHER = "d" * 40
METRICS = {"component_bytes": 4_000, "name_bytes": 900}


class FakeGitHub:
    """The same API seam serves the event's PR, artifacts and comments."""

    def __init__(self):
        self.pull = {
            "base": {"sha": BASE},
            "head": {"repo": {"id": 1}, "sha": HEAD, "ref": "feature"},
            "state": "open",
            "user": {"login": "author"},
        }
        self.comments = [
            {"id": 1, "user": {"login": "author"}, "body": MARKER + "\nIgnore this."},
            {"id": 2, "user": {"login": BOT}, "body": "Another bot comment"},
        ]
        self.writes = []
        self.runs = [
            {"id": 3, "run_attempt": 1, "head_sha": OTHER, "head_branch": "main", "event": "push"},
            {"id": 5, "run_attempt": 2, "head_sha": BASE, "head_branch": "main", "event": "push"},
        ]
        self.artifacts = [{"id": 9, "name": f"{BASE_ARTIFACT}-2", "expired": False}]

    def pages(self, path, key=None, **params):
        if path == "/actions/workflows/4/runs":
            assert params == {"branch": "main", "event": "push", "head_sha": BASE, "status": "success"}
            yield from self.runs
        elif path == "/actions/runs/5/artifacts":
            assert params == {"name": f"{BASE_ARTIFACT}-2"}
            yield from self.artifacts
        elif path == "/issues/7/comments":
            yield from self.comments
        else:
            raise AssertionError(path)

    def request(self, path, *, method="GET", data=None, archive=False):
        if method == "GET" and path == "/pulls/7":
            return self.pull
        if archive and path == "/actions/artifacts/9/zip":
            binary = io.BytesIO()
            with ZipFile(binary, "w") as compressed:
                receipt = {"version": 1, "sha": BASE, "metrics": METRICS}
                compressed.writestr("receipt.json", json.dumps(receipt))
            return binary.getvalue()
        if method == "POST" and path == "/issues/7/comments":
            self.comments.append({"id": 3, "user": {"login": BOT}, **data})
        elif method == "PATCH" and path == "/issues/comments/3":
            self.comments[2].update(data)
        else:
            raise AssertionError((method, path))
        self.writes.append(method)


class SizeReportTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.api = FakeGitHub()
        self.event = {
            "repository": {"id": 1},
            "workflow_run": {
                "event": "pull_request",
                "conclusion": "success",
                "head_repository": {"id": 1},
                "actor": {"login": "author"},
                "head_sha": HEAD,
                "head_branch": "feature",
                "workflow_id": 4,
                "pull_requests": [{"number": 7}],
            },
        }

    def post(self, metrics=METRICS, *, kind="provider"):
        path = self.directory / "receipt.json"
        report = {
            "version": 1, "kind": kind, "number": 7, "head": HEAD, "base": BASE,
            "build": MERGE if kind == "provider" else HEAD, "metrics": metrics,
        }
        path.write_text(json.dumps(report))
        post_report(self.api, self.event, path, kind)

    def test_missing_and_expired_base(self):
        for artifacts in ([], [{**self.api.artifacts[0], "expired": True}]):
            with self.subTest(artifacts=artifacts):
                self.api.artifacts = artifacts
                self.post()
                body = self.api.comments[2]["body"]
                self.assertIn("| Component bytes | base unavailable | 4,000 | base unavailable |", body)
                self.assertIn("| Name section bytes | base unavailable | 900 | base unavailable |", body)

    def test_sticky_comment_updates_and_zero_means_no_change(self):
        self.post()
        first = self.api.comments[2]["body"]
        self.assertTrue(first.startswith(MARKER + "\n"))
        self.assertIn("| Component bytes | 4,000 | 4,000 | no change |", first)
        self.assertIn(HEAD, first)
        self.assertIn(MERGE, first)
        self.post({**METRICS, "name_bytes": 800})
        self.assertEqual(self.api.writes, ["POST", "PATCH"])
        self.assertEqual(len(self.api.comments), 3)
        self.assertIn("| Name section bytes | 900 | 800 | -100 |", self.api.comments[2]["body"])

    def test_fork_dependabot_and_stale_head_skip(self):
        run = self.event["workflow_run"]
        run["head_repository"]["id"] = 2
        self.post()
        run["head_repository"]["id"] = 1
        run["actor"]["login"] = DEPENDABOT
        self.post()
        run["actor"]["login"] = "author"
        self.api.pull["user"]["login"] = DEPENDABOT
        self.post()
        self.api.pull["user"]["login"] = "author"
        self.api.pull["head"]["sha"] = OTHER
        self.post()
        self.assertEqual(self.api.writes, [])

    def test_core_comment_carries_each_crates_three_buckets(self):
        self.post({"dekopon-core": {"base": [4, 10, 0], "head": [5, 8, 0]}}, kind="core")
        body = self.api.comments[2]["body"]
        self.assertIn("| dekopon-core | Production | 4 | 5 | +1 |", body)
        self.assertIn("| dekopon-core | Test | 10 | 8 | -2 |", body)
        self.assertIn("| dekopon-core | Test support | 0 | 0 | no change |", body)

    def test_component_counts_embedded_name_payloads(self):
        def section(kind, payload):
            return bytes((kind, len(payload))) + payload

        module = MODULE + section(0, b"\x04name\x00\x01") + section(0, b"\x05other123")
        child = COMPONENT + section(1, module)
        data = COMPONENT + section(1, module) + section(4, child) + section(0, b"\x0ecomponent-name45")
        wasm = self.directory / "provider.wasm"
        wasm.write_bytes(data)
        self.assertEqual(component_size(wasm), {"component_bytes": len(data), "name_bytes": 4})
