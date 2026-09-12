#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    ROOT / "README.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "docs" / "DEEPAGENTS.md",
    ROOT / "docs" / "CERTIFICATION.md",
    ROOT / "docs" / "OPERATIONS.md",
    ROOT / ".env.example",
]
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def main() -> int:
    errors: list[str] = []
    for path in REQUIRED:
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            errors.append(f"missing/empty required document: {path.relative_to(ROOT)}")
    for path in [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("# "):
            errors.append(f"{path.relative_to(ROOT)}: must start with H1")
        for target in LINK.findall(text):
            if target.startswith(("http://", "https://", "#")):
                continue
            destination = (path.parent / target.split("#", 1)[0]).resolve()
            if not destination.exists():
                errors.append(f"{path.relative_to(ROOT)}: broken link {target}")
    if errors:
        print("Docs check FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Docs check PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
