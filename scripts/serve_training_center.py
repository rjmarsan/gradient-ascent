#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from gradient_ascent.config import ensure_private_data_dir, load_config  # noqa: E402
from gradient_ascent.training_center_server import serve_training_center  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve the training center with API writes to the configured daily_notes.json",
    )
    parser.add_argument("--port", type=int, default=8787, help="Port to bind (defaults to 8787)")
    parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Serve existing derived files without rebuilding first",
    )
    args = parser.parse_args()
    data_dir = ensure_private_data_dir(load_config().data_dir, action="serve training center")
    serve_training_center(
        data_dir,
        port=args.port,
        rebuild=not args.no_rebuild,
    )


if __name__ == "__main__":
    main()
