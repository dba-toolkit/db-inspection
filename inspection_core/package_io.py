"""Safe package and JSON I/O shared by database package adapters."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path
from typing import Any


class PackageIOError(RuntimeError):
    """Raised when a collected package cannot be read safely."""


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise PackageIOError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_safe_member(name: str) -> bool:
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts


def safe_extract_tar(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as package:
        members = package.getmembers()
        if len(members) > 5000:
            raise PackageIOError(f"Too many archive entries: {archive}")
        total = 0
        for member in members:
            if not is_safe_member(member.name):
                raise PackageIOError(f"Unsafe archive path: {member.name}")
            if member.issym() or member.islnk():
                raise PackageIOError(f"Archive links are not allowed: {member.name}")
            total += max(member.size, 0)
            if total > 2 * 1024 * 1024 * 1024:
                raise PackageIOError(f"Archive expands beyond 2 GiB: {archive}")
        package.extractall(destination, filter="data")
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) == 1 and (roots[0] / "snapshot.json").exists():
        return roots[0]
    if (destination / "snapshot.json").exists():
        return destination
    matches = list(destination.rglob("snapshot.json"))
    if len(matches) != 1:
        raise PackageIOError(f"Cannot determine package root in {archive}")
    return matches[0].parent
