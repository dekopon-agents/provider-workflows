"""Create size receipts and post a report from a completed measurement run."""

import argparse
import io
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile


MARKER = "<!-- dekopon-size-report -->"
BOT = "github-actions[bot]"
DEPENDABOT = "dependabot[bot]"
REPORT_ARTIFACT = "dekopon-size-report"
BASE_ARTIFACT = "dekopon-size-receipt"
MAX_JSON = 256 * 1024
SHA = re.compile(r"[0-9a-f]{40}")
CRATE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")
BUCKETS = ("Production", "Test", "Test support")
PROVIDER_METRICS = {"component_bytes": "Component bytes", "name_bytes": "Name section bytes"}

MODULE = b"\x00asm\x01\x00\x00\x00"
COMPONENT = b"\x00asm\x0d\x00\x01\x00"


def read_u32(data, offset, end):
    value = 0
    for shift in range(0, 35, 7):
        if offset == end:
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if byte < 0x80 and value <= 0xffffffff:
            return value, offset
    raise ValueError("invalid WebAssembly section length")


def component_size(path):
    data = path.read_bytes()
    if not data.startswith(COMPONENT):
        raise ValueError("expected a WebAssembly component")
    name_bytes = 0
    containers = [(0, len(data), COMPONENT)]
    while containers:
        start, end, encoding = containers.pop()
        if data[start:start + 8] != encoding or start + 8 > end:
            raise ValueError("invalid embedded WebAssembly header")
        offset = start + 8
        while offset < end:
            section = data[offset]
            size, payload = read_u32(data, offset + 1, end)
            offset = payload + size
            if offset > end:
                raise ValueError("WebAssembly section exceeds its container")
            if encoding == COMPONENT and section in (1, 4):
                containers.append((payload, offset, MODULE if section == 1 else COMPONENT))
            if encoding == MODULE and section == 0:
                length, name = read_u32(data, payload, offset)
                if name + length > offset:
                    raise ValueError("WebAssembly custom section exceeds its container")
                if data[name:name + length] == b"name":
                    name_bytes += offset - name - length
    return {"component_bytes": len(data), "name_bytes": name_bytes}


def measure(kind, event, build, event_name, repo, component, output):
    if event_name == "push":
        if kind == "core":
            from size_counter import Lines, size_row, snapshots

            sha, files = snapshots(repo, build)
            counts = sum(files.values(), Lines())
            print(size_row(sha, counts))
            return
        output.write_text(json.dumps({"version": 1, "sha": build, "metrics": component_size(component)}))
        return

    pr = event["pull_request"]
    head = pr["head"]["sha"]
    base = pr["base"]["sha"]
    if kind == "core":
        from size_counter import changed_crates

        metrics = changed_crates(repo, base, build)
    else:
        metrics = component_size(component)
    report = {
        "version": 1,
        "kind": kind,
        "number": pr["number"],
        "head": head,
        "base": base,
        "build": build,
        "metrics": metrics,
    }
    output.write_text(json.dumps(report, sort_keys=True))


class GitHub:
    def __init__(self, api_url, repository, token):
        self.base = f"{api_url}/repos/{repository}"
        self.token = token

    def request(self, path, *, method="GET", data=None, archive=False):
        request = Request(
            self.base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        # GitHub redirects archive downloads to a signed URL. Keep auth on the original request.
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        with urlopen(request, timeout=30) as response:
            if archive:
                return response.read(MAX_JSON + 1)
            return json.load(response)

    def pages(self, path, key=None, **params):
        page = 1
        while True:
            query = urlencode(dict(params, per_page=100, page=page))
            result = self.request(f"{path}?{query}")
            items = result if key is None else result[key]
            yield from items
            if len(items) < 100:
                return
            page += 1


def counts(values, size):
    return (
        isinstance(values, list)
        and len(values) == size
        and all(type(value) is int and 0 <= value <= 2**53 - 1 for value in values)
    )


def provider_metrics(metrics):
    return (
        isinstance(metrics, dict)
        and metrics.keys() == PROVIDER_METRICS.keys()
        and counts(list(metrics.values()), len(PROVIDER_METRICS))
        and metrics["name_bytes"] <= metrics["component_bytes"]
    )


def validate_report(report, kind):
    if (
        report["version"] != 1
        or report["kind"] != kind
        or not counts([report["number"]], 1)
        or report["number"] == 0
        or any(not SHA.fullmatch(report[key]) for key in ("head", "base", "build"))
    ):
        raise ValueError("invalid size report metadata")
    metrics = report["metrics"]
    if kind == "provider":
        if not provider_metrics(metrics):
            raise ValueError("invalid provider size metrics")
    elif not isinstance(metrics, dict) or any(
        not CRATE.fullmatch(crate)
        or not isinstance(row, dict)
        or row.keys() != {"base", "head"}
        or not counts(row["base"], len(BUCKETS))
        or not counts(row["head"], len(BUCKETS))
        for crate, row in metrics.items()
    ):
        raise ValueError("invalid crate size metrics")


def measurement_artifacts(api, run_id, prefix):
    # The upload is the last measurement step. Partial reruns can leave it in an older attempt.
    pattern = re.compile(rf"{prefix}-([1-9][0-9]*)")
    candidates = []
    for artifact in api.pages(f"/actions/runs/{run_id}/artifacts", "artifacts"):
        attempt = pattern.fullmatch(artifact["name"])
        if attempt and not artifact["expired"]:
            candidates.append((int(attempt[1]), artifact))
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    return [artifact for _, artifact in candidates]


def pr_report_name(api, run_id):
    artifacts = measurement_artifacts(api, run_id, REPORT_ARTIFACT)
    return artifacts[0]["name"] if artifacts else ""


def base_metrics(api, workflow_id, sha):
    path = f"/actions/workflows/{workflow_id}/runs"
    runs = api.pages(path, "workflow_runs", branch="main", event="push", head_sha=sha)
    for run in runs:
        if run["head_sha"] != sha or run["head_branch"] != "main" or run["event"] != "push":
            continue
        for artifact in measurement_artifacts(api, run["id"], BASE_ARTIFACT):
            try:
                archive = api.request(f"/actions/artifacts/{artifact['id']}/zip", archive=True)
            except HTTPError as error:
                if error.code in (404, 410):
                    continue
                raise
            if len(archive) > MAX_JSON:
                raise ValueError("oversized base receipt archive")
            with ZipFile(io.BytesIO(archive)) as compressed:
                info = compressed.getinfo("receipt.json")
                if info.file_size > MAX_JSON:
                    raise ValueError("oversized base receipt")
                receipt = json.loads(compressed.read(info))
            if receipt["version"] != 1 or receipt["sha"] != sha or not provider_metrics(receipt["metrics"]):
                raise ValueError("invalid base receipt")
            return receipt["metrics"]
    return None


def display_delta(base, head):
    if base is None:
        return "base unavailable"
    if base == head:
        return "no change"
    return f"{head - base:+,}"


def display_base(base):
    return "base unavailable" if base is None else f"{base:,}"


def comment_body(report, baseline):
    body = [
        MARKER,
        "### Size report",
        "",
        f"PR head {report['head']} · base {report['base']}",
        "",
    ]
    metrics = report["metrics"]
    if report["kind"] == "provider":
        if report["build"] != report["head"]:
            body.extend([f"Provider CI measured merge commit {report['build']}.", ""])
        body.extend(["| Metric | Base | Head | Delta |", "| --- | ---: | ---: | ---: |"])
        for key, label in PROVIDER_METRICS.items():
            before = baseline[key] if baseline is not None else None
            after = metrics[key]
            body.append(f"| {label} | {display_base(before)} | {after:,} | {display_delta(before, after)} |")
    else:
        if report["build"] != report["head"]:
            body.extend([f"Core CI measured merge commit {report['build']}.", ""])
        body.extend([
            "Tracked Rust lines, excluding examples. Base counts account for exact moves between counted paths.",
            "",
        ])
        if metrics:
            body.extend(["| Crate | Bucket | Base | Head | Delta |", "| --- | --- | ---: | ---: | ---: |"])
            for crate, row in sorted(metrics.items()):
                for label, before, after in zip(BUCKETS, row["base"], row["head"]):
                    body.append(f"| {crate} | {label} | {before:,} | {after:,} | {display_delta(before, after)} |")
        else:
            body.append("no change")
    return "\n".join(body) + "\n"


def current_pull(pr, run, repo_id, head):
    return (
        pr["state"] == "open"
        and pr["user"]["login"] != DEPENDABOT
        and (pr["head"]["repo"] or {}).get("id") == repo_id
        and pr["head"]["sha"] == head
        and pr["head"]["ref"] == run["head_branch"]
    )


def post_report(api, event, path, kind):
    run = event["workflow_run"]
    repo_id = event["repository"]["id"]
    if (
        run["event"] != "pull_request"
        or (run["head_repository"] or {}).get("id") != repo_id
        or run["actor"]["login"] == DEPENDABOT
    ):
        return
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON:
        raise ValueError("invalid size report file")
    report = json.loads(path.read_bytes())
    validate_report(report, kind)
    head = report["head"]
    if head != run["head_sha"]:
        raise ValueError("size report head differs from its workflow run")
    number = report["number"]
    associated = run.get("pull_requests") or []
    if associated and not any(pr["number"] == number for pr in associated):
        raise ValueError("size report PR differs from its workflow run")

    pull_path = f"/pulls/{number}"
    pull = api.request(pull_path)
    if not current_pull(pull, run, repo_id, head):
        return
    if not associated and pull["base"]["sha"] != report["base"]:
        return

    baseline = base_metrics(api, run["workflow_id"], report["base"]) if kind == "provider" else None
    body = comment_body(report, baseline)
    path = f"/issues/{number}/comments"
    existing = next(
        (
            comment for comment in api.pages(path)
            if comment["user"]["login"] == BOT and comment["body"].startswith(MARKER + "\n")
        ),
        None,
    )
    # Check after artifact/API reads too, in case a new head arrived while we were rendering.
    if not current_pull(api.request(pull_path), run, repo_id, head):
        return
    if existing is None:
        api.request(path, method="POST", data={"body": body})
    else:
        api.request(f"/issues/comments/{existing['id']}", method="PATCH", data={"body": body})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    measure_args = commands.add_parser("measure")
    post_args = commands.add_parser("post")
    commands.add_parser("select-report")
    for command in (measure_args, post_args):
        command.add_argument("kind", choices=("core", "provider"))
        command.add_argument("artifact", type=Path)
    measure_args.add_argument("--repo", type=Path, default=Path("."))
    measure_args.add_argument("--component", type=Path)
    args = parser.parse_args()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if args.command == "measure":
        measure(
            args.kind, event, os.environ["GITHUB_SHA"], os.environ["GITHUB_EVENT_NAME"],
            args.repo, args.component, args.artifact,
        )
    else:
        api = GitHub(os.environ["GITHUB_API_URL"], os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
        if args.command == "select-report":
            print(pr_report_name(api, event["workflow_run"]["id"]))
        else:
            post_report(api, event, args.artifact, args.kind)


if __name__ == "__main__":
    main()
