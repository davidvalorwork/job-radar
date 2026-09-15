"""Cross-platform quality commands. Run with uv run python scripts/quality.py all."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def commands(phase: str) -> list[list[str]]:
    checks = [
        [PYTHON, "-m", "ruff", "check", "."],
        [PYTHON, "-m", "ruff", "format", "--check", "."],
        [PYTHON, "-m", "mypy"],
        [PYTHON, "-m", "yamllint", "--strict", "."],
        [PYTHON, "-m", "pre_commit", "validate-config"],
        # Optional shellcheck/pyflakes disabled for identical Windows/Linux checks.
        ["actionlint", "-shellcheck=", "-pyflakes="],
        *[
            ["node", "--check", str(path.relative_to(ROOT))]
            for path in sorted((ROOT / "chrome-extension").glob("*.mjs"))
        ],
    ]
    tests = [
        [
            PYTHON,
            "-m",
            "pytest",
            "--cov",
            "--cov-report=term-missing",
            "--cov-report=xml:reports/coverage.xml",
            "--junitxml=reports/junit.xml",
        ],
        ["node", "--test", "tests/chrome_worker.test.mjs"],
    ]
    phases = {
        "check": checks,
        "format": [
            [PYTHON, "-m", "ruff", "check", "--fix", "."],
            [PYTHON, "-m", "ruff", "format", "."],
        ],
        "test": tests,
        "build": [["uv", "build"]],
        "all": checks + tests + [["uv", "build"]],
    }
    return phases[phase]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["check", "format", "test", "build", "all"])
    args = parser.parse_args(argv)
    for command in commands(args.phase):
        print("Running: " + " ".join(command), flush=True)
        try:
            result = subprocess.run(command, cwd=ROOT, check=False, timeout=600)
        except OSError, subprocess.TimeoutExpired:
            print("Quality tool unavailable or timed out. Run uv sync --locked.", file=sys.stderr)
            return 1
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
