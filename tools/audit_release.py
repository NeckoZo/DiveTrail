"""Inventory a possible public source release without copying or publishing it."""

import argparse
from collections import Counter
from collections.abc import Iterator
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess


ROOT_FILES = frozenset(
    {
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        "app.py",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "launch.bat",
        "LICENSE",
        "PRIVACY.md",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
        "README.md",
        "requirements-dev.txt",
        "requirements.txt",
    }
)
SOURCE_SUFFIXES = {
    ".github/workflows": {".yaml", ".yml"},
    "cloudbase": {".md", ".sql"},
    "docs": {".md"},
    "matchlog": {".py"},
    "tests": {".cjs", ".py"},
    "tools": {".cs", ".py"},
    "ui": {".css", ".html", ".js", ".json"},
}
# A deliberate hold for existing profile compatibility code. Keep the
# personal value itself out of the audit implementation and its reports.
ACCOUNT_COMPATIBILITY_FILES = frozenset(
    {
        "matchlog/recognition.py",
        "tests/test_pipeline.py",
        "tests/test_retention_backfill.py",
        "tests/test_rp_profile_snapshot.py",
        "ui/app.js",
    }
)
PRIVATE_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".playwright",
        ".playwright-cli",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "data",
        "output",
    }
)
PRIVATE_SUFFIXES = frozenset(
    {
        ".db",
        ".dpapi",
        ".key",
        ".log",
        ".matchlog",
        ".p12",
        ".pem",
        ".pyc",
        ".sqlite",
        ".sqlite3",
        ".sqlite3-shm",
        ".sqlite3-wal",
    }
)
PRIVATE_FILENAMES = frozenset(
    {".netrc", ".npmrc", ".pypirc", "credentials.json", "secrets.json"}
)
FINDING_PATTERNS = {
    "local_home_path": re.compile(
        r"(?i)(?:[a-z]:[/\\]+Users[/\\]+|/(?:home|Users)/)[^\s<>\x22\x27]+"
    ),
    "steam_id": re.compile(r"(?<!\d)7656119\d{10}(?!\d)"),
    "credential_like": re.compile(
        r"(?i)\b(?:gh[pousr]_[a-z0-9]{30,}|github_pat_[a-z0-9_]{30,}"
        r"|(?:AKIA|ASIA)[A-Z0-9]{16}|sk-(?:proj-)?[a-z0-9_-]{28,})\b"
        r"|-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"
    ),
    "secret_assignment": re.compile(
        r"""(?i)\b(?:api[-_]?key|access[-_]?token|auth[-_]?token|password|client[-_]?secret)"""
        r"""\b["']?\s*[:=]\s*["'][^"'\s]{16,}["']"""
    ),
}
MAX_SOURCE_SIZE = 2 * 1024 * 1024


def private_path(relative: Path) -> bool:
    return (
        any(part.casefold() in PRIVATE_DIRS for part in relative.parts)
        or relative.name.casefold().startswith((".env", "qa-"))
        or relative.name.casefold() in PRIVATE_FILENAMES
        or relative.suffix.casefold() in PRIVATE_SUFFIXES
    )


def redacted_path(relative: Path) -> str:
    value = relative.as_posix()
    for pattern in FINDING_PATTERNS.values():
        value = pattern.sub("[redacted]", value)
    return value


def redirected(path: Path) -> bool:
    metadata = path.lstat()
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def inventory_paths(root: Path) -> Iterator[tuple[Path, bool]]:
    """Prune runtime directories before opening any files, including symlinks."""

    def fail(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        folder = Path(directory)
        for name in sorted(dirs[:]):
            path = folder / name
            relative = path.relative_to(root)
            if private_path(relative):
                dirs.remove(name)
            elif redirected(path):
                dirs.remove(name)
                yield relative, True
        for name in sorted(files):
            path = folder / name
            relative = path.relative_to(root)
            if not private_path(relative):
                yield relative, redirected(path)


def category(relative: Path) -> str:
    if relative.is_relative_to("ui/assets"):
        return "bundled_asset"
    if relative.is_relative_to("tests/fixtures"):
        return "test_fixture"
    if relative.as_posix() in ACCOUNT_COMPATIBILITY_FILES:
        return "account_compatibility_review"
    if relative.as_posix() in ROOT_FILES:
        return "candidate_source"
    if any(
        relative.is_relative_to(folder) and relative.suffix.lower() in suffixes
        for folder, suffixes in SOURCE_SUFFIXES.items()
    ):
        return "candidate_source"
    return "unknown_file"


def text_findings(text: str) -> list[dict]:
    findings = []
    for line, value in enumerate(text.splitlines(), start=1):
        for name, pattern in FINDING_PATTERNS.items():
            if pattern.search(value):
                findings.append({"line": line, "category": name})
    return findings


def scan(root: Path) -> dict:
    candidates = []
    held = []
    for relative, linked in sorted(inventory_paths(root)):
        path = root / relative
        name = redacted_path(relative)
        if linked:
            held.append({"path": name, "reason": "redirected_path"})
            continue
        kind = category(relative)
        size = path.stat().st_size
        if kind != "candidate_source":
            held.append({"path": name, "reason": kind, "bytes": size})
            continue
        if size > MAX_SOURCE_SIZE:
            held.append({"path": name, "reason": "oversized_source", "bytes": size})
            continue
        content = path.read_bytes()
        try:
            findings = text_findings(content.decode("utf-8-sig"))
            if b"\0" in content:
                raise ValueError
        except (UnicodeError, ValueError):
            held.append({"path": name, "reason": "non_text_source", "bytes": size})
            continue
        if findings or name != relative.as_posix():
            held.append(
                {"path": name, "reason": "privacy_review", "findings": findings}
            )
        else:
            candidates.append(
                {
                    "path": name,
                    "bytes": size,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
    return {
        "ready_for_publication": False,
        "manual_review": [
            "code_license",
            "third_party_redistribution",
            "public_history_and_samples",
        ],
        "candidate_source": candidates,
        "withheld": held,
    }


def history_private_paths(root: Path) -> dict:
    """Check names in local refs only. This does not review historical content."""
    completed = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root.as_posix()}",
            "-C",
            str(root),
            "log",
            "--all",
            "--format=",
            "--name-only",
            "-z",
            "--no-renames",
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    names = {
        Path(value.decode("utf-8", errors="replace").lstrip("\n"))
        for value in completed.stdout.split(b"\0")
        if value.strip()
    }
    suspicious = [p for p in names if private_path(p)]
    return {
        "paths_examined": len(names),
        # Private filenames may themselves identify accounts. Report only counts.
        "private_paths_found": len(suspicious),
        "requires_content_review": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        action="store_true",
        help="Also check path names in local Git history",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full redacted candidate inventory",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        report = scan(root)
    except OSError:
        parser.exit(2, "无法完整检查工作目录，请确认项目文件可读。\n")
    if args.history:
        try:
            report["git_history_paths"] = history_private_paths(root)
        except (OSError, subprocess.CalledProcessError):
            parser.exit(2, "无法读取此工作目录的本地 Git 历史。\n")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        held = dict(Counter(row["reason"] for row in report["withheld"]))
        print(
            f"候选源码 {len(report['candidate_source'])} 个，待复核 {sum(held.values())} 个。"
        )
        for reason, count in sorted(held.items()):
            print(f"  {reason}: {count}")
        if args.history:
            result = report["git_history_paths"]
            print(
                f"历史路径 {result['paths_examined']} 个，匹配私有文件规则 {result['private_paths_found']} 个。"
            )
        print("公开发布状态：待确认。用 --json 查看待复核路径和候选文件哈希。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
