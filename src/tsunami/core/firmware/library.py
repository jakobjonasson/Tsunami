"""The firmware folder: image files on this PC, and their hashes.

Images are hundreds of megabytes, so they live in machine data, never in the workspace.
A play names an image by file name and pins it with a hash; the file found here must
match every hash the play gives, or nothing is sent. That makes the play, which a person
reviews and an approval is pinned to, the authority on what gets installed: swapping the
file in the folder afterwards fails the run instead of installing something else.

See design.md, Firmware upgrades: The firmware folder.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

from tsunami.core.firmware.base import FirmwareError, ImageFile
from tsunami.core.models.play import FirmwareImage
from tsunami.core.models.settings import AppSettings
from tsunami.core.paths import AppPaths

ALGORITHMS = ("md5", "sha256", "sha512")
_CHUNK = 1024 * 1024
_cache: dict[tuple[str, int, int], dict[str, str]] = {}
_cache_lock = threading.Lock()


def firmware_dir(paths: AppPaths, settings: AppSettings | None = None) -> Path:
    """Where image files are looked for: the folder in Settings, else machine data."""
    chosen = settings.firmware_dir if settings is not None else None
    return Path(chosen) if chosen else paths.firmware_dir


def file_hashes(path: Path) -> dict[str, str]:
    """md5, sha256 and sha512 of a file, read once. Cached until the file changes."""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _cache_lock:
        if key in _cache:
            return dict(_cache[key])
    digests = {name: hashlib.new(name) for name in ALGORITHMS}
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            for digest in digests.values():
                digest.update(chunk)
    found = {name: digest.hexdigest() for name, digest in digests.items()}
    with _cache_lock:
        _cache[key] = found
    return dict(found)


def _inside(folder: Path, name: str) -> Path | None:
    """The file in the folder, or None. A name is a bare file name (the play model
    refuses anything else), and a link that leads out of the folder is not followed."""
    if not name or Path(name).name != name:
        return None
    path = folder / name
    try:
        if not path.is_file() or path.resolve().parent != folder.resolve():
            return None
    except OSError:
        return None
    return path


def resolve_image(folder: Path, image: FirmwareImage, *, need_file: bool) -> ImageFile:
    """The play's image with what the firmware folder adds. With need_file (an scp
    transfer) a missing file is an error; without it the play's hashes stand alone."""
    path = _inside(folder, image.file)
    if path is None:
        if need_file:
            raise FirmwareError(
                f"{image.file} is not in the firmware folder ({folder}); put the image "
                "there, or use a tftp or http transfer"
            )
        return ImageFile(image.file, image.version, dict(image.hashes))
    try:
        found = file_hashes(path)
        size = path.stat().st_size
    except OSError as exc:
        raise FirmwareError(f"cannot read {image.file}: {type(exc).__name__}") from None
    for name, expected in image.hashes.items():
        if found[name] != expected:
            raise FirmwareError(
                f"{image.file} in the firmware folder is not the file the play names: its "
                f"{name} is {found[name]}, and the play says {expected}"
            )
    return ImageFile(image.file, image.version, found, size, path)


@dataclass(frozen=True)
class LibraryFile:
    name: str
    size: int
    hashes: dict[str, str]


def list_images(folder: Path) -> list[LibraryFile]:
    """Every file in the firmware folder with its hashes, by name. Hashing a new large
    file takes a few seconds the first time."""
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if _inside(folder, path.name) is None:
            continue
        try:
            out.append(LibraryFile(path.name, path.stat().st_size, file_hashes(path)))
        except OSError:
            continue
    return out
