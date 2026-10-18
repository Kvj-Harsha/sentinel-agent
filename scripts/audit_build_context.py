"""Check a Docker-filtered context; report paths/categories only, never matched values.

Run inside a BuildKit read-only context mount, not against the unfiltered host tree.
This is a focused pattern check, not a complete security or Git history audit.
"""

import fnmatch
from pathlib import Path
import sys

from secret_scan import PATTERNS


PRIVATE_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".pytest-tmp", ".ruff_cache", ".mypy_cache", ".secrets", ".reset-backups",
    ".codex", ".agents", ".idea", ".vscode", "node_modules",
}
TEXT_SUFFIXES = {".py", ".json", ".hocon", ".md", ".yml", ".yaml", ".txt", ".ps1", ".sh", ".toml", ".ini", ".cfg", ".example"}


def private_path(path):
    if any(part in PRIVATE_DIRS for part in path.parts):
        return True
    name = path.name.lower()
    if name != ".env.example" and (name == ".env" or name.startswith(".env.") or name == ".envrc"):
        return True
    if any(fnmatch.fnmatch(name, pattern) for pattern in ("*.db", "*.db-*", "*.sqlite*", "*.pyc", "*.pyo", "*.log", "*.pem", "*.key")):
        return True
    return any(path.parts[i:i+2] in (("eval", "_reports"), ("eval", "_thinking")) for i in range(len(path.parts)-1))


def audit(root):
    findings = []
    count = 0
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            findings.append((str(file.relative_to(root)), "symlink requires explicit review"))
            continue
        if not file.is_file():
            continue
        count += 1
        relative = file.relative_to(root)
        if private_path(relative):
            findings.append((str(relative), "private/local artifact included"))
            continue
        if file.suffix.lower() not in TEXT_SUFFIXES and file.name not in {"Dockerfile", ".env.example"}:
            continue
        try:
            content = file.read_text(encoding="utf-8")
        except UnicodeError:
            findings.append((str(relative), "text file not valid UTF-8; review required"))
            continue
        for label, pattern in PATTERNS:
            if pattern.search(content):
                findings.append((str(relative), f"possible {label}; value suppressed"))
    return count, findings


def main():
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_dir():
        print("Usage: audit_build_context.py <Docker-filtered-context-directory>")
        return 2
    try:
        count, findings = audit(Path(sys.argv[1]))
    except OSError:
        print("FAIL: context could not be read; values suppressed.")
        return 2
    for path, reason in findings:
        print(f"FAIL: {path}: {reason}")
    if findings:
        print(f"Context audit failed: {len(findings)} findings across {count} files.")
        return 1
    print(f"PASS: {count} context files; no forbidden artifacts or recognized token/private-key patterns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
