"""Fail closed on common secrets/private artifacts before publishing this tree.

Scans bytes, reports filenames/rule names only; never prints matched values.
This is an additional guard, not a proof of absence of every possible secret.
"""
from pathlib import Path
import json
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "api-token": re.compile(rb"(?:sk-|xai-|gh[pousr]_)[A-Za-z0-9_-]{16,}"),
    "github-fine-grained-token": re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "windows-user-path": re.compile(rb"[A-Za-z]:[\\/]+Users[\\/]+", re.I),
    "unix-home-path": re.compile(rb"/(?:home|Users)/[A-Za-z0-9._-]+/"),
    "personal-email": re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}
FORBIDDEN_PARTS = {".venv", "venv", "inbox", "output", "subtitles", ".vscode", ".idea"}
FORBIDDEN_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".srt", ".ass", ".log", ".lnk", ".png", ".jpg", ".jpeg", ".key", ".pem", ".p12"}


def paths_to_scan():
    result = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=ROOT,
                            capture_output=True)
    if result.returncode == 0 and result.stdout:
        return [ROOT / p.decode("utf-8") for p in result.stdout.split(b"\0") if p]
    return [p for p in ROOT.rglob("*") if p.is_file()
            and ".git" not in p.relative_to(ROOT).parts
            and "__pycache__" not in p.relative_to(ROOT).parts]


def scan():
    failures = []
    files = paths_to_scan()
    for path in files:
        relative = path.relative_to(ROOT)
        parts = set(relative.parts)
        if (parts & FORBIDDEN_PARTS or path.suffix.lower() in FORBIDDEN_EXTENSIONS
                or path.name == "history.md" or path.name.endswith(".local.json")
                or (path.name.startswith(".env") and path.name != ".env.example")
                or "credentials" in path.name.lower()):
            failures.append((relative.as_posix(), "private-artifact"))
        content = path.read_bytes()
        for name, rule in RULES.items():
            if rule.search(content):
                failures.append((relative.as_posix(), name))
        if path.suffix == ".json":
            data = json.loads(content)
            if isinstance(data, dict):
                for key, value in data.items():
                    if re.search(r"(?i)(?:api[_-]?key|password|secret|token)$", key) and value:
                        failures.append((relative.as_posix(), "nonempty-credential"))
    for relative, rule in failures:
        print(f"FAIL {relative}: {rule}")
    print(f"Scanned {len(files)} files; findings: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(scan())
