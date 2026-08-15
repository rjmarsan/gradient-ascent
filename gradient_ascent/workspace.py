from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .config import ensure_private_data_dir
from .workspace_lock import workspace_lock


WORKSPACE_MARKERS = (
    Path("AGENTS.md"),
    Path("plan/onboarding.json"),
    Path("connections/config.json"),
)


def _cross_device_descendants(workspace: Path) -> list[Path]:
    root_device = workspace.stat(follow_symlinks=False).st_dev
    crossings: list[Path] = []
    for current, directories, _files in os.walk(workspace, topdown=True, followlinks=False):
        current_path = Path(current)
        traversable: list[str] = []
        for directory in directories:
            path = current_path / directory
            if path.is_symlink():
                continue
            try:
                device = path.stat(follow_symlinks=False).st_dev
            except OSError as exc:
                raise ValueError(f"Could not verify workspace filesystem boundary: {path}") from exc
            if device != root_device:
                crossings.append(path)
            else:
                traversable.append(directory)
        directories[:] = traversable
    return crossings


def _validated_workspace(workspace: Path) -> Path:
    expanded = workspace.expanduser()
    if expanded.is_symlink():
        raise ValueError("Refusing to purge a symbolic link; provide the real workspace path.")
    resolved = ensure_private_data_dir(expanded, action="purge coach workspace").resolve()
    if resolved in {Path("/").resolve(), Path.home().resolve()}:
        raise ValueError(f"Refusing to purge unsafe path: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Gradient Ascent workspace does not exist: {resolved}")
    if resolved.is_mount():
        raise ValueError(f"Refusing to purge a workspace that is a filesystem mount point: {resolved}")
    crossings = _cross_device_descendants(resolved)
    if crossings:
        raise ValueError(
            "Refusing to purge a workspace containing another filesystem mount: "
            f"{crossings[0]}"
        )
    missing = [str(marker) for marker in WORKSPACE_MARKERS if not (resolved / marker).is_file()]
    if missing:
        raise ValueError(
            f"Path is not a recognized Gradient Ascent workspace: {resolved}; "
            f"missing {', '.join(missing)}"
        )
    return resolved


def _workspace_purge_preview(resolved: Path) -> dict[str, Any]:
    files = 0
    bytes_total = 0
    for path in resolved.rglob("*"):
        if path.is_file() and not path.is_symlink():
            files += 1
            try:
                bytes_total += path.stat().st_size
            except OSError:
                pass
    return {
        "workspace": str(resolved),
        "existing_paths": sorted(path.name for path in resolved.iterdir()),
        "file_count": files,
        "byte_count": bytes_total,
        "will_delete_workspace": True,
        "includes_git_history": (resolved / ".git").exists(),
    }


def preview_workspace_purge(workspace: Path) -> dict[str, Any]:
    return _workspace_purge_preview(_validated_workspace(workspace))


def purge_workspace_data(workspace: Path, *, confirmation: str) -> dict[str, Any]:
    # Lock the requested path before its sole validation. workspace_lock records
    # the directory identity before waiting and rejects a replaced generation.
    with workspace_lock(workspace):
        resolved = _validated_workspace(workspace)
        preview = _workspace_purge_preview(resolved)
        expected = preview["workspace"]
        if confirmation != expected:
            raise ValueError(
                "Exact resolved workspace path confirmation is required; "
                f"expected {expected!r}."
            )
        shutil.rmtree(Path(expected))
        return {
            **preview,
            "deleted": True,
        }
