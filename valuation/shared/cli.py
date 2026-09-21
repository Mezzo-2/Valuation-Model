from __future__ import annotations

import argparse
from pathlib import Path


def run_dir_arg() -> Path:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--segment", default="")
    args = parser.parse_args()
    return Path(args.run_dir), args.segment
