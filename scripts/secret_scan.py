"""Scan staged Git blobs for common credentials without printing matched values."""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Iterable

PATTERNS = (
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("provider/service token", re.compile(r"(?i)\b(?:sk-(?:or-v1-)?[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|ATATT[A-Za-z0-9_+/=-]{20,}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


def staged_paths() -> Iterable[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        check=True, capture_output=True,
    )
    return (path.decode("utf-8", errors="replace") for path in result.stdout.split(b"\0") if path)


def main() -> int:
    try:
        findings = []
        for path in staged_paths():
            blob = subprocess.run(["git", "show", f":{path}"], check=True, capture_output=True).stdout
            content = blob.decode("utf-8", errors="ignore")
            for line_number, line in enumerate(content.splitlines(), start=1):
                for label, pattern in PATTERNS:
                    if pattern.search(line):
                        findings.append((path, line_number, label))
        if findings:
            for path, line_number, label in findings:
                print(f"{path}:{line_number}: possible {label} (value suppressed)")
            print("Commit blocked. Revoke/rotate any exposed value, remove it from the staged file, then stage the safe version.")
            return 1
        print("Secret scan passed for staged files.")
        return 0
    except (OSError, subprocess.CalledProcessError):
        print("Secret scan could not read the Git index; commit blocked.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
