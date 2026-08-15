#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from gradient_ascent.config import ensure_private_data_dir, load_config  # noqa: E402
from gradient_ascent.training_center import build_training_center  # noqa: E402


def _repo_root() -> Path:
    return REPO_ROOT


def main() -> None:
    data_dir = ensure_private_data_dir(load_config().data_dir, action="build training center")
    result = build_training_center(data_dir)
    print(f"Wrote {result['html']} and {result['data_js']}")


if __name__ == "__main__":
    main()
