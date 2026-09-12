from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    status: str
    command: tuple[str, ...]
    returncode: int | None
    duration_seconds: float
    output_file: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "command": list(self.command),
            "returncode": self.returncode,
            "duration_seconds": self.duration_seconds,
            "output_file": self.output_file,
        }


def _unavailable_gate(name: str, command: list[str], output_root: Path, reason: str) -> GateResult:
    output_file = output_root / f"gate-{name}.log"
    output_file.write_text(reason + "\n", encoding="utf-8")
    return GateResult(name, "UNAVAILABLE", tuple(command), None, 0.0, str(output_file))


def _run_module_gate(name: str, module: str, command: list[str], output_root: Path) -> GateResult:
    if importlib.util.find_spec(module) is None:
        return _unavailable_gate(
            name,
            command,
            output_root,
            f"Python module not installed: {module}",
        )
    return _run_gate(name, command, output_root)


def _run_gate(name: str, command: list[str], output_root: Path) -> GateResult:
    output_file = output_root / f"gate-{name}.log"
    executable = shutil.which(command[0])
    if executable is None:
        output_file.write_text(f"executable not found: {command[0]}\n", encoding="utf-8")
        return GateResult(name, "UNAVAILABLE", tuple(command), None, 0.0, str(output_file))
    started = time.monotonic()
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        env=os.environ.copy(),
    )
    duration = time.monotonic() - started
    output_file.write_text(completed.stdout, encoding="utf-8")
    return GateResult(
        name,
        "PASS" if completed.returncode == 0 else "FAIL",
        tuple(command),
        completed.returncode,
        round(duration, 3),
        str(output_file),
    )


def deterministic_gates(output_root: Path) -> tuple[GateResult, ...]:
    return (
        _run_gate(
            "pytest",
            [
                sys.executable,
                "-m",
                "pytest",
                "-m",
                "not live",
                "--cov=work_graph_middleware",
                "--cov-branch",
                "--cov-report=term-missing",
                "--cov-fail-under=95",
            ],
            output_root,
        ),
        _run_gate("ruff", ["ruff", "check", "src", "tests", "scripts"], output_root),
        _run_gate(
            "format",
            ["ruff", "format", "--check", "src", "tests", "scripts"],
            output_root,
        ),
        _run_gate("mypy", ["mypy", "src/work_graph_middleware"], output_root),
        _run_gate("docs", [sys.executable, "scripts/docs_check.py"], output_root),
        _run_gate("secrets", [sys.executable, "scripts/secret_scan.py"], output_root),
        _run_module_gate(
            "build",
            "build",
            [sys.executable, "-m", "build"],
            output_root,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Certify WorkGraph middleware")
    parser.add_argument(
        "--live",
        action="store_true",
        help="run real OpenRouter DeepAgents scenarios",
    )
    parser.add_argument("--only", action="append", default=[], help="live scenario id to run")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", default=None)
    parser.add_argument(
        "--allow-unavailable-quality-tools",
        action="store_true",
        help="development-only: do not fail because Ruff/mypy/build are unavailable",
    )
    args = parser.parse_args(argv)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = Path(args.output or f"artifacts/certification/{stamp}").resolve()
    root.mkdir(parents=True, exist_ok=True)

    gates = deterministic_gates(root)
    quality_ok = all(
        item.status == "PASS"
        or (args.allow_unavailable_quality_tools and item.status == "UNAVAILABLE")
        for item in gates
    )
    live_payload: dict[str, Any] | None = None
    if args.live and quality_ok:
        from work_graph_middleware.testing.live import run_live_certification

        live_payload = run_live_certification(
            output_root=root / "live",
            only=set(args.only) or None,
            repetitions=max(1, args.repetitions),
        )

    passed = quality_ok and (live_payload is None or live_payload["status"] == "PASS")
    payload = {
        "schema_version": 1,
        "status": "PASS" if passed else "FAIL",
        "gates": [item.to_dict() for item in gates],
        "live": live_payload,
    }
    (root / "certification.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    lines = ["# WorkGraph Certification", "", f"**Status:** {payload['status']}", "", "## Gates"]
    lines.extend(f"- {item.status} — {item.name}" for item in gates)
    if args.live:
        lines.extend(
            [
                "",
                "## Live DeepAgents / OpenRouter",
                f"**Status:** {live_payload['status'] if live_payload else 'NOT RUN'}",
                "",
                "See `live/summary.md` and per-scenario `REPORT.md` files.",
            ]
        )
    (root / "CERTIFICATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "artifact_root": str(root)}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
