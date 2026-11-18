#!/usr/bin/env python3
"""Check docs/failure-taxonomy.md against the code: every Q-number is unique and in order, every
row has a known status, and every test the catalog names exists in pipeline/tests, db/tests or
ml/tests. Run by scripts/check.sh (and so by CI). Standard library only.

A row may name no test ("—") only if its status is `review only` or `curation`: a problem the
pipeline claims to handle must have a test that keeps it handled.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "docs" / "failure-taxonomy.md"
TEST_DIRS = ["pipeline/tests", "db/tests", "discovery/tests", "ml/tests"]
STATUSES = ("handled", "review only", "curation")


def defined_tests() -> set:
    names = set()
    for d in TEST_DIRS:
        for f in (ROOT / d).glob("test_*.py"):
            names.update(re.findall(r"^def (test_\w+)", f.read_text(encoding="utf-8"), flags=re.M))
    return names


def rows(text: str):
    for line in text.splitlines():
        m = re.match(r"\|\s*(Q\d+)\s*\|", line)
        if m:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            yield m.group(1), cells


def main() -> int:
    tests = defined_tests()
    problems, seen = [], []
    for q, cells in rows(CATALOG.read_text(encoding="utf-8")):
        if len(cells) != 7:
            problems.append(f"{q}: expected 7 columns, found {len(cells)}")
            continue
        status, named = cells[6], re.findall(r"`(test_\w+)`", cells[5])
        seen.append(q)
        if not any(status.startswith(s) or s in status for s in STATUSES):
            problems.append(f"{q}: unknown status {status!r} (use one of {', '.join(STATUSES)})")
        for t in named:
            if t not in tests:
                problems.append(f"{q}: test {t} does not exist in {', '.join(TEST_DIRS)}")
        if not named and status.startswith("handled"):
            problems.append(f"{q}: status 'handled' needs at least one regression test")
    numbers = [int(q[1:]) for q in seen]
    if len(set(numbers)) != len(numbers):
        problems.append("duplicate Q-numbers")
    if numbers != sorted(numbers):
        problems.append("Q-numbers out of order")
    for p in problems:
        print(p)
    if not problems:
        print(f"{len(seen)} data problems catalogued; every named test exists")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
