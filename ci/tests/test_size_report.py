import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from ci.size_report import (
    BASE_ARTIFACT, BOT, COMPONENT, DEPENDABOT, MARKER, MODULE, REPORT_ARTIFACT,
    component_size, post_report, pr_report_name,
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
            {"id": 3, "run_attempt": 1, "head_sha": OTHER, "head_branch": "main",
             "event": "push", "conclusion": "success"},
            {"id": 5, "run_attempt": 2, "head_sha": BASE, "head_branch": "main",
             "event": "push", "conclusion": "success"},
        ]
        self.artifacts = [{"id": 9, "name": f"{BASE_ARTIFACT}-2", "expired": False}]
        self.pr_artifacts = []

    def pages(self, path, key=None, **params):
        if path == "/actions/workflows/4/runs":
            status = params.pop("status", None)
            assert params == {"branch": "main", "event": "push", "head_sha": BASE}
            yield from (run for run in self.runs if status is None or run["conclusion"] == status)
        elif path in {"/actions/runs/5/artifacts", "/actions/runs/6/artifacts"}:
            name = params.get("name")
            artifacts = self.artifacts if path == "/actions/runs/5/artifacts" else self.pr_artifacts
            yield from (artifact for artifact in artifacts if name is None or artifact["name"] == name)
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
                "id": 6,
                "run_attempt": 1,
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
            "build": MERGE, "metrics": metrics,
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

    def test_failed_sibling_jobs_keep_pr_report_and_base_receipt(self):
        self.event["workflow_run"]["conclusion"] = "failure"
        self.api.runs[1]["conclusion"] = "failure"
        self.post()
        self.assertEqual(self.api.writes, ["POST"])
        self.assertIn("| Component bytes | 4,000 | 4,000 | no change |", self.api.comments[2]["body"])

    def test_partial_rerun_keeps_the_previous_base_receipt(self):
        self.api.runs[1]["run_attempt"] = 3
        self.post()
        self.assertIn("| Component bytes | 4,000 | 4,000 | no change |", self.api.comments[2]["body"])

    def test_partial_rerun_selects_latest_unexpired_pr_report(self):
        run = self.event["workflow_run"]
        run["run_attempt"] = 12
        self.api.pr_artifacts = [
            {"id": 10, "name": f"{REPORT_ARTIFACT}-9", "expired": False},
            {"id": 11, "name": f"{REPORT_ARTIFACT}-10", "expired": False},
            {"id": 12, "name": f"{REPORT_ARTIFACT}-11", "expired": True},
            {"id": 13, "name": f"{BASE_ARTIFACT}-12", "expired": False},
        ]
        self.assertEqual(pr_report_name(self.api, run["id"]), f"{REPORT_ARTIFACT}-10")

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
        self.assertIn(f"Core CI measured merge commit {MERGE}.", body)
        self.assertIn("| dekopon-core | Production | 4 | 5 | +1 |", body)
        self.assertIn("| dekopon-core | Test | 10 | 8 | -2 |", body)
        self.assertIn("| dekopon-core | Test support | 0 | 0 | no change |", body)

    def test_workflow_isolates_poster_from_downloaded_artifact(self):
        workflow = Path(__file__).resolve().parents[2] / ".github/workflows/size-post.yml"
        definition = workflow.read_text()
        script = '"$RUNNER_TEMP/size-poster/size_report.py"'
        artifact = '"$RUNNER_TEMP/size-report/receipt.json"'
        self.assertIn(f"--output {script}", definition)
        self.assertIn(f"python3 -I -B {script} select-report", definition)
        self.assertIn(f'python3 -I -B {script} post "$KIND" {artifact}', definition)
        self.assertIn("path: ${{ runner.temp }}/size-report\n", definition)

    def test_component_counts_embedded_name_payloads(self):
        def section(kind, payload):
            return bytes((kind, len(payload))) + payload

        module = MODULE + section(0, b"\x04name\x00\x01") + section(0, b"\x05other123")
        child = COMPONENT + section(1, module)
        data = COMPONENT + section(1, module) + section(4, child) + section(0, b"\x0ecomponent-name45")
        wasm = self.directory / "provider.wasm"
        wasm.write_bytes(data)
        self.assertEqual(component_size(wasm), {"component_bytes": len(data), "name_bytes": 4})
