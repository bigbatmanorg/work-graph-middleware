#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "artifacts", "dist", "build", "__pycache__"}
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".txt",
    ".example",
    ".gitignore",
}
PATTERNS = [
    ("OpenRouter key", re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}")),
    ("OpenAI-style key", re.compile(r"sk-[A-Za-z0-9_-]{30,}")),
    (
        "hardcoded secret assignment",
        re.compile(r"(?i)(api[_-]?key|token|password)\s*[=:]\s*['\"][^'\"]{12,}['\"]"),
    ),
]


def main() -> int:
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.name == ".env.example" or path.suffix in TEXT_SUFFIXES or path.name == ".gitignore":
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for label, pattern in PATTERNS:
                if pattern.search(text):
                    findings.append(f"{path.relative_to(ROOT)}: {label}")
    if findings:
        print("Secret scan FAILED")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("Secret scan PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
