"""Count tracked Rust lines at a revision, independently of the working tree."""

import argparse
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
import re
import subprocess
from typing import NamedTuple


SUPPORT = {"dekopon-test-support", "dekopon-provider-sdk-testkit"}
OPEN = {b"(": b")", b"[": b"]", b"{": b"}"}
CLOSE = set(OPEN.values())
LEX = re.compile(
    rb'(?P<space>\s+|//[^\n]*)|(?P<comment>/\*)'
    rb'|(?P<raw>(?:br|cr|r)(?P<hashes>#{0,255})")'
    rb'|"(?:\\[\s\S]|[^"\\])*"'
    rb"|'(?:\\(?:u\{[\da-fA-F_]+\}|x[\da-fA-F]{2}|.)|[^'\\\n])'"
    rb'|[\w\x80-\xff]+|->|.'
)
COMMENT = re.compile(rb"/\*|\*/")
NEWLINE = re.compile(rb"\n")
ITEM = {
    b"const", b"enum", b"extern", b"fn", b"impl", b"macro", b"macro_rules",
    b"mod", b"static", b"struct", b"trait", b"type", b"union", b"use",
}


class Lines(NamedTuple):
    production: int = 0
    test: int = 0
    support: int = 0

    @property
    def total(self):
        return sum(self)

    def __add__(self, other):
        return Lines(
            self.production + other.production,
            self.test + other.test,
            self.support + other.support,
        )


class Token(NamedTuple):
    value: bytes
    start: int
    end: int


def rust_tokens(source):
    cursor = 0
    while cursor < len(source):
        match = LEX.match(source, cursor)
        cursor = match.end()
        if match.group("space"):
            continue
        if match.group("comment"):
            depth = 1
            for delimiter in COMMENT.finditer(source, cursor):
                depth += 1 if delimiter[0] == b"/*" else -1
                if depth == 0:
                    cursor = delimiter.end()
                    break
            else:
                raise ValueError("unterminated Rust block comment")
            continue
        if match.group("raw"):
            end = b'"' + match.group("hashes")
            close = source.find(end, cursor)
            if close < 0:
                raise ValueError("unterminated Rust raw string")
            cursor = close + len(end)
        yield Token(match[0], match.start(), cursor)


def delimiters(tokens):
    stack = []
    pairs = {}
    enclosing = {}
    for index, token in enumerate(tokens):
        value = token.value
        if value in OPEN:
            stack.append(index)
        elif value in CLOSE and stack and OPEN[tokens[stack[-1]].value] == value:
            start = stack.pop()
            pairs[start] = index
            pairs[index] = start
        elif value == b"#" and stack:
            enclosing[index] = stack[-1]
    return pairs, enclosing


def attribute_open(tokens, index):
    bracket = index + 1
    if bracket < len(tokens) and tokens[bracket].value == b"!":
        bracket += 1
    if bracket < len(tokens) and tokens[bracket].value == b"[":
        return bracket
    return None


def is_const_function(tokens, index):
    # After const, Rust allows async, item safety, and extern with an optional ABI.
    following = (token.value for token in tokens[index + 1:index + 6])
    qualifier = next(following, b"")
    if qualifier == b"async":
        qualifier = next(following, b"")
    if qualifier in {b"safe", b"unsafe"}:
        qualifier = next(following, b"")
    if qualifier == b"extern":
        qualifier = next(following, b"")
        if qualifier.startswith((b'"', b'r"', b'r#')):
            qualifier = next(following, b"")
    return qualifier == b"fn"


def item_end(tokens, pairs, start):
    # Braces in generics or a semicolon-terminated item are part of the header/value.
    kind = None
    semicolon = False
    angles = 0
    index = start
    while index < len(tokens):
        value = tokens[index].value
        if value in ITEM and kind is None:
            kind = b"fn" if value == b"const" and is_const_function(tokens, index) else value
            semicolon = kind in {b"static", b"type", b"use", b"const"}
        elif value == b"<" and not semicolon:
            angles += 1
        elif value == b">" and angles:
            angles -= 1
        elif value in OPEN:
            closing = pairs.get(index)
            if closing is None:
                return index - 1
            if value == b"{" and not semicolon and angles == 0:
                return closing
            index = closing
        elif value == b";":
            return index
        elif value in CLOSE or (value == b"," and kind is None):
            return index - 1
        index += 1
    return index - 1


def inline_test_lines(source):
    if b"cfg" not in source or b"test" not in source:
        return 0
    tokens = list(rust_tokens(source))
    pairs, enclosing = delimiters(tokens)
    newlines = [match.start() for match in NEWLINE.finditer(source)]
    test_lines = set()
    attr_chain = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        bracket = attribute_open(tokens, index) if token.value == b"#" else None
        close = pairs.get(bracket)
        if close is None:
            attr_chain = None
            index += 1
            continue

        condition = tuple(part.value for part in tokens[bracket + 1:close])
        if condition in {(b"cfg", b"(", b"test", b")"), (b"cfg", b"(", b"test", b",", b")")}:
            if bracket == index + 2:
                if index not in enclosing and source:
                    return physical_lines(source)
                scope = enclosing[index]
                end = pairs.get(scope, close)
                first = token.start
            else:
                item = close + 1
                while item < len(tokens) and tokens[item].value == b"#":
                    next_bracket = attribute_open(tokens, item)
                    if next_bracket != item + 1 or next_bracket not in pairs:
                        break
                    item = pairs[next_bracket] + 1
                end = item_end(tokens, pairs, item)
                first = tokens[attr_chain if attr_chain is not None else index].start
            last = tokens[end].end - 1
            test_lines.update(range(bisect_left(newlines, first), bisect_left(newlines, last) + 1))

        if bracket == index + 1 and attr_chain is None:
            attr_chain = index
        index = close + 1
    return len(test_lines)


def physical_lines(source):
    return source.count(b"\n") + bool(source and not source.endswith(b"\n"))


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def resolve(repo, revision):
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").strip().decode()


def rust_blobs(repo, sha):
    for entry in git(repo, "ls-tree", "-rz", sha, "--", "crates/").split(b"\0"):
        if not entry:
            continue
        info, path = entry.split(b"\t", 1)
        mode, _, oid = info.split()
        parts = path.split(b"/")
        if mode in {b"100644", b"100755"} and len(parts) >= 3 and path.endswith(b".rs"):
            yield tuple(part.decode("utf-8", "surrogateescape") for part in parts), oid


def snapshots(repo, revision, *, path_method=False, include_examples=False, split_support=False):
    sha = resolve(repo, revision)
    files = {}
    command = ["git", "-C", str(repo), "cat-file", "--batch"]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        for parts, oid in rust_blobs(repo, sha):
            if not (include_examples or path_method) and "examples" in parts[1:-1]:
                continue
            process.stdin.write(oid + b"\n")
            process.stdin.flush()
            _, _, size = process.stdout.readline().split()
            source = process.stdout.read(int(size))
            if process.stdout.read(1) != b"\n":
                raise ValueError("incomplete git blob")
            total = physical_lines(source)
            if not path_method and not split_support and parts[1] in SUPPORT:
                counts = Lines(support=total)
            elif "tests" in parts[1:-1] or parts[-1] == "tests.rs":
                counts = Lines(test=total)
            elif path_method:
                counts = Lines(production=total)
            else:
                test = inline_test_lines(source)
                counts = Lines(production=total - test, test=test)
            files["/".join(parts)] = counts
        process.stdin.close()
        if process.wait():
            raise ValueError("git cat-file failed")
    return sha, files


def crate_totals(files):
    totals = defaultdict(Lines)
    for path, counts in files.items():
        totals[path.split("/")[1]] += counts
    return totals


def changed_crates(repo, base, head):
    base, old = snapshots(repo, base)
    head, new = snapshots(repo, head)
    records = git(repo, "diff", "--name-status", "-z", "--find-renames=100%", base, head).split(b"\0")
    changes = iter(records[:-1])
    changed = set()
    for kind in changes:
        path = next(changes).decode("utf-8", "surrogateescape")
        if kind == b"R100":
            destination = next(changes).decode("utf-8", "surrogateescape")
            if path in old and destination in new:
                del old[path]
                old[destination] = new[destination]
                continue
            if destination in new:
                changed.add(destination.split("/")[1])
        if path in old or path in new:
            changed.add(path.split("/")[1])
    before = crate_totals(old)
    after = crate_totals(new)
    return {crate: {"base": before[crate], "head": after[crate]} for crate in sorted(changed)}


def size_row(sha, counts):
    return f"| {sha[:8]} | {counts.production:,} | {counts.test:,} | {counts.support:,} | {counts.total:,} |"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--path-method", action="store_true", help="reproduce the historical two-bucket path count")
    parser.add_argument("revision")
    args = parser.parse_args()
    sha, files = snapshots(args.repo, args.revision, path_method=args.path_method)
    counts = sum(files.values(), Lines())
    if args.path_method:
        print(f"{sha[:8]} production={counts.production} test={counts.test} total={counts.total}")
    else:
        print(size_row(sha, counts))


if __name__ == "__main__":
    main()
