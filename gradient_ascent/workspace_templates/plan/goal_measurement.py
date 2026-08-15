from __future__ import annotations

from pathlib import Path

from gradient_ascent.progress import goals_progress_html


def build_progress_html(data_dir: Path) -> str:
    """Render a useful conservative default until a coach adds custom evaluation logic."""
    return goals_progress_html(data_dir)
