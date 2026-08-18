"""Opt-in, append-only installation of reviewed private coaching guidance."""

from __future__ import annotations

import os
from pathlib import Path

from . import recording_repair as _files
from .workspace_lock import workspace_identity, workspace_lock


MAX_GUIDANCE_BYTES = 1024 * 1024
MANAGED_START = b"<!-- gradient-ascent:coaching-history:start -->"
MANAGED_END = b"<!-- gradient-ascent:coaching-history:end -->"
_HISTORY_IGNORE = b"plan/.history/"


def _managed_section(body: bytes) -> bytes | None:
    starts, ends = body.count(MANAGED_START), body.count(MANAGED_END)
    if not starts and not ends:
        return None
    if starts != 1 or ends != 1 or body.index(MANAGED_START) >= body.index(MANAGED_END):
        raise ValueError("Coaching guidance markers are incomplete or duplicated.")
    return body[body.index(MANAGED_START) : body.index(MANAGED_END) + len(MANAGED_END)]


def _guidance_section() -> bytes:
    template = Path(__file__).parent / "workspace_templates" / "AGENTS.md"
    with template.open("rb") as handle:
        body = handle.read(MAX_GUIDANCE_BYTES + 1)
    if len(body) > MAX_GUIDANCE_BYTES:
        raise RuntimeError("The packaged coaching guidance is unavailable.")
    section = _managed_section(body)
    if section is None or len(section) > MAX_GUIDANCE_BYTES:
        raise RuntimeError("The packaged coaching guidance is unavailable.")
    return section


def _append(body: bytes, addition: bytes, *, blank_line: bool = False) -> bytes:
    separator = b"" if not body or body.endswith(b"\n") else b"\n"
    if blank_line and body and not (body + separator).endswith(b"\n\n"):
        separator += b"\n"
    return body + separator + addition + b"\n"


def _history_ignore_update(body: bytes) -> bytes | None:
    rules = [line for line in body.splitlines() if line.strip() and not line.startswith(b"#")]
    if rules and rules[-1] == _HISTORY_IGNORE:
        return None
    updated = _append(body, _HISTORY_IGNORE)
    if len(updated) > MAX_GUIDANCE_BYTES:
        raise ValueError("Workspace guidance exceeds its size limit.")
    return updated


def _ensure_history_ignore(root: int) -> bool:
    """Protect private history before persistence, using its already-pinned root."""
    before = _files._read(root, ".gitignore", MAX_GUIDANCE_BYTES)
    updated = _history_ignore_update(before or b"")
    if updated is None:
        return False
    if _files._read(root, ".gitignore", MAX_GUIDANCE_BYTES) != before:
        raise RuntimeError("Workspace ignore rules changed; retry.")
    _files._write(root, ".gitignore", updated, MAX_GUIDANCE_BYTES)
    os.fsync(root)
    return True


def install_coaching_history_guidance(
    data_dir: Path, *, expected_identity: tuple[int, int] | None = None
) -> dict[str, bool]:
    """Preserve user instructions; install only the reviewed managed section."""
    data_dir = Path(data_dir)
    if not _files._secure_files_supported():
        raise RuntimeError("This platform cannot safely install coaching guidance.")
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    section = _guidance_section()
    with workspace_lock(data_dir, expected_identity=identity), _files._directory(data_dir) as root:
        before = {
            name: _files._read(root, name, MAX_GUIDANCE_BYTES)
            for name in ("AGENTS.md", ".gitignore")
        }
        agents, ignore = before["AGENTS.md"] or b"", before[".gitignore"] or b""
        updates = {}
        if _managed_section(agents) is None:
            updates["AGENTS.md"] = _append(agents, section, blank_line=True)
        ignore_update = _history_ignore_update(ignore)
        if ignore_update is not None:
            updates[".gitignore"] = ignore_update
        if any(len(body) > MAX_GUIDANCE_BYTES for body in updates.values()):
            raise ValueError("Workspace guidance exceeds its size limit.")
        _files._assert_generation(data_dir, root, identity)
        for name, body in updates.items():
            if _files._read(root, name, MAX_GUIDANCE_BYTES) != before[name]:
                raise RuntimeError("Workspace guidance changed; retry.")
            _files._assert_generation(data_dir, root, identity)
            _files._write(root, name, body, MAX_GUIDANCE_BYTES)
            os.fsync(root)
            _files._assert_generation(data_dir, root, identity)
        return {"installed": "AGENTS.md" in updates, "ignore_updated": ".gitignore" in updates}
