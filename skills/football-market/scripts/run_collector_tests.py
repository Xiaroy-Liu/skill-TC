#!/usr/bin/env python3
"""Run collector tests in the verified Scrapling environment when required."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_WORKER = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"


def supports_okooo_html_tests(python: Path) -> bool:
    try:
        completed = subprocess.run(
            [str(python), "-c", "import scrapling; from football_sources.eightbo.runner import collect_8bo"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--start-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    # Keep a virtualenv symlink intact; resolving it drops the environment's
    # site-packages and makes an installed Scrapling look unavailable.
    current = Path(sys.executable).absolute()
    if not supports_okooo_html_tests(current):
        configured = os.getenv("FOOTBALL_EIGHTBO_PYTHON")
        fallback = Path(configured).expanduser() if configured else DEFAULT_WORKER
        if not supports_okooo_html_tests(fallback):
            raise RuntimeError("scrapling_runtime_unavailable")
        os.execv(str(fallback), [str(fallback), str(Path(__file__).resolve()), *sys.argv[1:]])
    start_dir = args.start_dir.resolve()
    return subprocess.call(
        [str(current), "-m", "unittest", "discover", "-s", str(start_dir), "-p", args.pattern],
        cwd=start_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
