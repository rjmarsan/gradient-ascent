from __future__ import annotations

import os
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATA_DIR = Path("~/code/gradient-ascent-workspace")
WORKSPACE_DIR_ENV = "COACH_WORKSPACE_DIR"
DATA_DIR_ENV = "COACH_DATA_DIR"


def _load_dotenv() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv
    except Exception:
        return
    workspace_dir = os.getenv(WORKSPACE_DIR_ENV) or os.getenv(DATA_DIR_ENV)
    if workspace_dir:
        env_path = Path(workspace_dir).expanduser() / ".env"
        if env_path.is_file():
            load_dotenv(env_path)
            return
    load_dotenv(find_dotenv(usecwd=True))


def default_data_dir() -> Path:
    return DEFAULT_DATA_DIR.expanduser()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_gradient_ascent_checkout(directory: Path) -> bool:
    manifest = directory / ".codex-plugin" / "plugin.json"
    project = directory / "pyproject.toml"
    package_marker = directory / "gradient_ascent" / "config.py"
    if any(not marker.is_file() or marker.is_symlink() for marker in (manifest, project, package_marker)):
        return False
    try:
        plugin_name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
        project_name = tomllib.loads(project.read_text(encoding="utf-8"))["project"]["name"]
    except (AttributeError, KeyError, OSError, TypeError, UnicodeError, ValueError):
        return False
    return plugin_name == "gradient-ascent" and project_name == "gradient-ascent"


def ensure_private_data_dir(data_dir: Path, *, action: str = "use coach data") -> Path:
    expanded = data_dir.expanduser()
    resolved = expanded.resolve()
    root = repo_root().resolve()
    inside_checkout = resolved == root or _is_relative_to(resolved, root)
    if not inside_checkout:
        inside_checkout = any(_is_gradient_ascent_checkout(parent) for parent in (resolved, *resolved.parents))
    if inside_checkout:
        raise SystemExit(
            f"Refusing to {action} with a workspace/data directory inside the "
            f"Gradient Ascent checkout: {resolved}. Use a separate coaching workspace "
            f"such as ~/code/gradient-ascent-workspace, or set "
            f"{WORKSPACE_DIR_ENV}/{DATA_DIR_ENV} to another private workspace."
        )
    return expanded


def ensure_private_output_path(path: Path, *, action: str = "write coach output") -> Path:
    return ensure_private_data_dir(path, action=action)


@dataclass(frozen=True)
class Config:
    data_dir: Path


def load_config() -> Config:
    _load_dotenv()
    data_dir = Path(
        os.getenv(WORKSPACE_DIR_ENV)
        or os.getenv(DATA_DIR_ENV)
        or default_data_dir()
    ).expanduser()
    return Config(data_dir=data_dir)
