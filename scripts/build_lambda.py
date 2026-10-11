"""Build the Lambda bundle in .lambda-build/ from any OS.

`sam build` on Windows resolves dependencies for Windows and fails on Windows-only packages.
uv resolves and downloads wheels for the Lambda platform (Linux x86_64, Python 3.13) instead.

Usage: python scripts/build_lambda.py     (requires `pip install uv`)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".lambda-build"
APP_DIRS = ("backend", "frontend", "data")
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".pytest_cache")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    subprocess.run([
        sys.executable, "-m", "uv", "pip", "install", "--quiet",
        "--python-platform", "x86_64-manylinux2014", "--python-version", "3.13",
        "--only-binary", ":all:", "--target", str(OUT), "-r", str(ROOT / "requirements.txt"),
    ], check=True)
    for name in APP_DIRS:
        shutil.copytree(ROOT / name, OUT / name, ignore=SKIP)
    # boto3 and botocore ship with the Lambda runtime.
    for extra in ("boto3", "botocore", "bin"):
        shutil.rmtree(OUT / extra, ignore_errors=True)
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file()) / 1e6
    print(f"{OUT} ready: {size:.1f} MB unzipped (Lambda limit is 250 MB)")


if __name__ == "__main__":
    main()
