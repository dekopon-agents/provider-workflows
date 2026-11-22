import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from ci.size_counter import Lines, SUPPORT, changed_crates, git, snapshots


HISTORY = os.environ.get("SIZE_HISTORY_REPO")
COUNTER = Path(__file__).parents[1] / "size_counter.py"
INLINE_TEST = b'''// #[cfg(test)] is a comment
fn run() {}
#[cfg(test)]
mod tests {
    #[test]
    fn inner() {
        let _ = r#" } "#;
    }
}
fn after() {}'''


class SizeCounterTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)
        git(self.repo, "init", "--quiet")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "user.name", "Size fixture")
        self.write("crates/core/src/lib.rs", INLINE_TEST)
        self.write("crates/core/tests/check.rs", b"fn main() {\n}\n")
        self.write("crates/core/examples/check.rs", b"fn main() {}\n")
        self.write("crates/dekopon-test-support/tests/fixture.rs", b"#[cfg(test)]\nfn fixture() {}\n")
        self.commit("fixture")

    def write(self, name, body):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def commit(self, message):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "--quiet", "-m", message)

    def test_inline_support_examples_and_conservation(self):
        _, files = snapshots(self.repo, "HEAD")
        self.assertEqual(
            files,
            {
                "crates/core/src/lib.rs": Lines(3, 7, 0),
                "crates/core/tests/check.rs": Lines(0, 2, 0),
                "crates/dekopon-test-support/tests/fixture.rs": Lines(0, 0, 2),
            },
        )
        self.assertEqual(sum(files.values(), Lines()).total, 14)

    def test_exact_rename_across_crate_and_bucket_counts_zero(self):
        destination = self.repo / "crates/other/tests/moved.rs"
        destination.parent.mkdir(parents=True)
        git(self.repo, "mv", "crates/core/src/lib.rs", str(destination))
        self.commit("move")
        self.assertEqual(changed_crates(self.repo, "HEAD^", "HEAD"), {})

        self.write("crates/other/tests/moved.rs", INLINE_TEST + b"\nfn added() {}\n")
        self.commit("add one line")
        self.assertEqual(
            changed_crates(self.repo, "HEAD^", "HEAD"),
            {"other": {"base": Lines(0, 10, 0), "head": Lines(0, 11, 0)}},
        )


@unittest.skipUnless(HISTORY, "set SIZE_HISTORY_REPO to a full read-only Dekopon checkout")
class SizeHistoryTest(unittest.TestCase):
    def test_exact_path_method_and_inline_reclassification(self):
        sha = "ea5c4730"
        output = subprocess.check_output(
            [sys.executable, "-B", str(COUNTER), "--repo", HISTORY, "--path-method", sha], text=True
        ).strip()
        print(f"\n{output}")
        self.assertEqual(output, "ea5c4730 production=106488 test=49666 total=156154")
        _, path = snapshots(HISTORY, sha, path_method=True)
        _, default = snapshots(HISTORY, sha)
        inline = sum(counts.test for name, counts in default.items() if path[name].production)
        counts = sum(default.values(), Lines())
        self.assertGreaterEqual(inline, 30_000)
        self.assertEqual(counts.total, sum(path[name].total for name in default))
        print(f"{sha} default={tuple(counts)} total={counts.total} inline={inline}")

    def test_bridged_baselines(self):
        for sha, expected in (("26c05897", (96_789, 99_892)), ("4eb92d29", (151_742, 173_893))):
            with self.subTest(sha=sha):
                _, path = snapshots(HISTORY, sha, path_method=True)
                _, default = snapshots(HISTORY, sha)
                _, bridge = snapshots(HISTORY, sha, include_examples=True, split_support=True)
                literal = sum(path.values(), Lines())
                actual = sum(default.values(), Lines())
                support = sum((bridge[name] for name in default if name.split("/")[1] in SUPPORT), Lines())
                examples = sum((bridge[name] for name in bridge.keys() - default.keys()), Lines())
                adjusted = (actual.production + support.production + examples.production,
                            actual.test + support.test + examples.test)
                print(
                    f"\n{sha} default={tuple(actual)} total={actual.total} "
                    f"path={(literal.production, literal.test)} "
                    f"support={(support.production, support.test)} "
                    f"examples={(examples.production, examples.test)} bridged={adjusted}"
                )
                self.assertEqual(actual.total, sum(path[name].total for name in default))
                self.assertEqual(support.total, actual.support)
                if sha == "4eb92d29":
                    self.assertEqual(support.production, 3_371)
                for measured, baseline in zip(adjusted, expected):
                    self.assertLessEqual(abs(measured - baseline), baseline * 0.01)
