"""Generate the GitHub changelog from the in-app release notes."""

import json
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]


class Release(TypedDict):
    version: str
    date: str
    changes: list[str]


class Changelog(TypedDict):
    version: str
    releases: list[Release]


def render_changelog(data: Changelog) -> str:
    sections = ["# 更新日志"]
    for release in data["releases"]:
        sections.append(
            f"## {release['version']} · {release['date']}\n\n"
            + "\n".join(f"- {change}" for change in release["changes"])
        )
    return "\n\n".join(sections) + "\n"


if __name__ == "__main__":
    source = json.loads((ROOT / "ui/changelog.json").read_text(encoding="utf-8"))
    (ROOT / "CHANGELOG.md").write_text(render_changelog(source), encoding="utf-8")
