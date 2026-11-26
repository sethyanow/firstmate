#!/usr/bin/env python3
"""Descriptor-safe filesystem engine for fm-record-export.sh.

The shell wrapper owns the public command line and task-lock integration.
This helper owns configured path traversal, dedicated roots, snapshots,
archive transactions, and durable scout report packages.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import html
import json
import os
import posixpath
import re
import stat
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable
from urllib.parse import unquote


ROOT_SCHEMA = "firstmate-record-export-root.v1"
ROOT_MARKER = ".firstmate-record-export-root.v1.json"
REPORT_CONFIG = "report-collection-root"
REPORT_SCHEMA = "firstmate-report-collection-root.v1"
REPORT_MARKER = ".firstmate-report-collection-root.v1.json"
LIVE_GENERATIONS = ".live-generations"
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
INBOX_MESSAGE = re.compile(r"^[0-9]+\.msg$")
GENERATION_NAME = re.compile(r"^g[0-9]+\.[0-9]+\.[a-f0-9]+$")
STAGE_NAME = re.compile(r"^\.stage-[0-9]+-[a-f0-9]+$")
LIVE_POINTER_NAME = re.compile(r"^\.live-next-[0-9]+-[a-f0-9]+$")
ARCHIVE_STAGE_NAME = re.compile(r"^\.archive-stage-[0-9]+-[a-f0-9]+$")
UTC_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class ExportError(RuntimeError):
    pass


def fail(message: str) -> "NoReturn":
    raise ExportError(message)


def json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def parse_json(data: bytes, label: str) -> object:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=json_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"invalid {label}: {exc}")


def safe_component(value: str, label: str) -> None:
    if not SAFE_COMPONENT.fullmatch(value) or value in {".", ".."}:
        fail(f"unsafe {label}: {value}")


def plain_name(part: str) -> bool:
    """A path component that names a visible entry in its own directory."""
    return bool(part) and not part.startswith(".") and "/" not in part and "\x00" not in part


def read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            fail("short write while publishing record export")
        view = view[written:]


def fd_identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def stat_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def stable_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def open_absolute_directory(path: str, label: str) -> int:
    if not os.path.isabs(path):
        fail(f"{label} must be absolute: {path}")
    parts = [part for part in path.split(os.sep) if part]
    fd = os.open(os.sep, DIR_FLAGS)
    try:
        for part in parts:
            if part in {".", ".."}:
                fail(f"{label} contains an unsafe component: {path}")
            next_fd = os.open(part, DIR_FLAGS | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def open_parent_for_new_directory(path: str, label: str) -> tuple[int, str]:
    normalized = path.rstrip(os.sep)
    if not normalized or normalized == os.sep:
        fail(f"{label} must not be the filesystem root")
    parent, name = os.path.split(normalized)
    if name in {"", ".", ".."}:
        fail(f"{label} has an unsafe final component")
    return open_absolute_directory(parent or os.sep, f"{label} parent"), name


def open_regular_at(parent_fd: int, name: str, label: str) -> tuple[int, os.stat_result]:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(before.st_mode):
        fail(f"{label} is not a regular non-symlink file")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
    except OSError as exc:
        fail(f"cannot open {label}: {exc.strerror}")
    after = os.fstat(fd)
    if not stat.S_ISREG(after.st_mode) or stat_identity(before) != fd_identity(fd):
        os.close(fd)
        fail(f"{label} changed while it was opened")
    return fd, after


def open_directory_at(parent_fd: int, name: str, label: str) -> int:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        fail(f"cannot inspect {label}: {exc.strerror}")
    if not stat.S_ISDIR(before.st_mode):
        fail(f"{label} is not a real directory")
    try:
        fd = os.open(name, DIR_FLAGS | NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        fail(f"cannot open {label}: {exc.strerror}")
    if stat_identity(before) != fd_identity(fd):
        os.close(fd)
        fail(f"{label} changed while it was opened")
    return fd


def ensure_directory_at(parent_fd: int, name: str, label: str) -> int:
    if name != LIVE_GENERATIONS and not plain_name(name):
        fail(f"unsafe {label}: {name!r}")
    try:
        os.mkdir(name, 0o750, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    fd = open_directory_at(parent_fd, name, label)
    os.fchmod(fd, 0o750)
    return fd


def atomic_write_at(parent_fd: int, name: str, data: bytes, mode: int = 0o640) -> None:
    token = uuid.uuid4().hex
    temporary = f".record-export-{os.getpid()}-{token}"
    fd = -1
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=parent_fd,
        )
        write_all(fd, data)
        os.fchmod(fd, mode)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def read_config(config_path: str, name: str = "record-export-root") -> str | None:
    label = "record export" if name == "record-export-root" else "report collection"
    config_fd = open_absolute_directory(config_path, f"{label} config directory")
    try:
        try:
            fd, _ = open_regular_at(config_fd, name, f"{label} config")
        except FileNotFoundError:
            return None
        try:
            data = read_all(fd)
        finally:
            os.close(fd)
    finally:
        os.close(config_fd)
    if b"\x00" in data:
        fail(f"{label} config contains a NUL byte")
    lines = data.splitlines()
    if len(lines) != 1 or not lines[0]:
        fail(f"{label} config must contain exactly one non-empty line")
    try:
        root = os.fsdecode(lines[0])
    except UnicodeDecodeError:
        fail(f"{label} config is not valid filesystem text")
    if not os.path.isabs(root):
        fail(f"{label} root must be absolute")
    return root


def canonical_path(path: str) -> str:
    return os.path.normpath(os.path.realpath(path))


def paths_overlap(left: str, right: str) -> bool:
    try:
        common = os.path.commonpath([left, right])
    except ValueError:
        return False
    return common == left or common == right


def validate_disjoint_root(root: str, protected: Iterable[tuple[str, str]], label: str = "record export") -> None:
    root_path = canonical_path(root)
    for protected_label, path in protected:
        protected_path = canonical_path(path)
        if paths_overlap(root_path, protected_path):
            fail(f"{label} root overlaps the effective {protected_label} root: {root}")


def export_root_marker(home: str) -> dict[str, object]:
    return {"schema": ROOT_SCHEMA, "schema_version": 1, "home": canonical_path(home)}


def validate_root_marker(root_fd: int, document: dict[str, object], marker: str, label: str) -> None:
    try:
        fd, _ = open_regular_at(root_fd, marker, f"{label} ownership marker")
    except FileNotFoundError:
        fail(f"existing {label} root has no ownership marker")
    try:
        value = parse_json(read_all(fd), f"{label} ownership marker")
    finally:
        os.close(fd)
    if value != document:
        fail(f"{label} root has a foreign or invalid ownership marker")


def validate_root_entries(root_fd: int) -> None:
    allowed = {ROOT_MARKER, LIVE_GENERATIONS, "live", "archive"}
    for name in os.listdir(root_fd):
        if LIVE_POINTER_NAME.fullmatch(name):
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISLNK(info.st_mode):
                fail("record export stale live pointer is unsafe")
            target = os.readlink(name, dir_fd=root_fd)
            if not re.fullmatch(r"\.live-generations/" + GENERATION_NAME.pattern[1:-1], target):
                fail("record export stale live pointer has an unsafe target")
            os.unlink(name, dir_fd=root_fd)
            os.fsync(root_fd)
            continue
        if name not in allowed:
            fail(f"unknown artifact in dedicated record export root: {name}")
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if name == ROOT_MARKER and not stat.S_ISREG(info.st_mode):
            fail("record export ownership marker is unsafe")
        if name in {LIVE_GENERATIONS, "archive"} and not stat.S_ISDIR(info.st_mode):
            fail(f"record export managed directory is unsafe: {name}")
        if name == "live":
            if not stat.S_ISLNK(info.st_mode):
                fail("record export live pointer is unsafe")
            target = os.readlink(name, dir_fd=root_fd)
            if not re.fullmatch(r"\.live-generations/" + GENERATION_NAME.pattern[1:-1], target):
                fail("record export live pointer has an unsafe target")


def open_marked_root(
    root: str, document: dict[str, object], protected: Iterable[tuple[str, str]], label: str, marker: str
) -> int:
    if os.path.normpath(root) == os.sep:
        fail(f"{label} root must not be the filesystem root")
    validate_disjoint_root(root, protected, label)
    parent_fd, name = open_parent_for_new_directory(root, f"{label} root")
    created = False
    try:
        try:
            root_fd = open_directory_at(parent_fd, name, f"{label} root")
        except ExportError:
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(name, 0o750, dir_fd=parent_fd)
                os.fsync(parent_fd)
                root_fd = open_directory_at(parent_fd, name, f"{label} root")
                created = True
            else:
                raise
    finally:
        os.close(parent_fd)
    try:
        if created:
            fd = os.open(
                marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                0o640,
                dir_fd=root_fd,
            )
            try:
                write_all(fd, (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode())
                os.fchmod(fd, 0o640)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(root_fd)
        validate_root_marker(root_fd, document, marker, label)
        return root_fd
    except Exception:
        os.close(root_fd)
        raise


def open_export_root(root: str, home: str, protected: Iterable[tuple[str, str]]) -> int:
    root_fd = open_marked_root(root, export_root_marker(home), protected, "record export", ROOT_MARKER)
    try:
        validate_root_entries(root_fd)
        os.fchmod(root_fd, 0o750)
        os.fsync(root_fd)
        return root_fd
    except Exception:
        os.close(root_fd)
        raise


@dataclass
class Source:
    relative: str
    fd: int
    initial: os.stat_result

    def close(self) -> None:
        os.close(self.fd)


def add_source(sources: list[Source], parent_fd: int, name: str, relative: str, label: str) -> None:
    try:
        fd, info = open_regular_at(parent_fd, name, label)
    except FileNotFoundError:
        return
    sources.append(Source(relative, fd, info))


def collect_data_sources(data_fd: int) -> list[Source]:
    sources: list[Source] = []
    for name in ("projects.md", "backlog.md", "done-archive.md"):
        add_source(sources, data_fd, name, f"data/{name}", f"data/{name}")
    for task in sorted(os.listdir(data_fd)):
        if task in {"projects.md", "backlog.md", "done-archive.md"}:
            continue
        try:
            info = os.stat(task, dir_fd=data_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        task_fd = open_directory_at(data_fd, task, f"data task directory {task}")
        try:
            names = set(os.listdir(task_fd))
            candidates = [name for name in ("brief.md", "report.md") if name in names]
            candidates.extend(sorted(name for name in names if name.startswith("status") and name.endswith(".log")))
            if not candidates:
                continue
            safe_component(task, "task directory in record export allowlist")
            for name in candidates:
                safe_component(name, "data record name")
                add_source(sources, task_fd, name, f"data/{task}/{name}", f"data/{task}/{name}")
        finally:
            os.close(task_fd)
    return sources


def collect_inbox_sources(state_fd: int, inbox_name: str, task: str, sources: list[Source]) -> None:
    inbox_fd = open_directory_at(state_fd, inbox_name, f"state inbox {inbox_name}")
    try:
        for name in sorted(os.listdir(inbox_fd)):
            if INBOX_MESSAGE.fullmatch(name):
                add_source(sources, inbox_fd, name, f"state/{inbox_name}/{name}", f"state/{inbox_name}/{name}")
        try:
            handled_info = os.stat("handled", dir_fd=inbox_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(handled_info.st_mode):
            fail(f"state/{inbox_name}/handled is not a real directory")
        handled_fd = open_directory_at(inbox_fd, "handled", f"state/{inbox_name}/handled")
        try:
            for name in sorted(os.listdir(handled_fd)):
                if INBOX_MESSAGE.fullmatch(name):
                    add_source(
                        sources,
                        handled_fd,
                        name,
                        f"state/{task}.inbox/handled/{name}",
                        f"state/{task}.inbox/handled/{name}",
                    )
        finally:
            os.close(handled_fd)
    finally:
        os.close(inbox_fd)


def collect_state_sources(state_fd: int) -> list[Source]:
    sources: list[Source] = []
    add_source(sources, state_fd, "fleet-ledger.jsonl", "state/fleet-ledger.jsonl", "state/fleet-ledger.jsonl")
    names = sorted(os.listdir(state_fd))
    for name in names:
        if name.startswith("."):
            continue
        if name.endswith(".meta"):
            task = name[: -len(".meta")]
            safe_component(task, "task metadata name")
            add_source(sources, state_fd, name, f"state/{name}", f"state/{name}")
        elif name.endswith(".status"):
            task = name[: -len(".status")]
            safe_component(task, "task status name")
            add_source(sources, state_fd, name, f"state/{name}", f"state/{name}")
        elif name.endswith(".inbox"):
            task = name[: -len(".inbox")]
            safe_component(task, "task inbox name")
            collect_inbox_sources(state_fd, name, task, sources)
    return sources


def ensure_relative_directory(root_fd: int, parts: list[str]) -> int:
    fd = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = ensure_directory_at(fd, part, "record export snapshot directory")
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def copy_source(source: Source, stage_fd: int) -> str:
    parts = source.relative.split("/")
    if not all(plain_name(part) for part in parts):
        fail(f"unsafe record export relative path: {source.relative!r}")
    parent_fd = ensure_relative_directory(stage_fd, parts[:-1])
    destination_fd = -1
    source_digest = hashlib.sha256()
    try:
        destination_fd = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o640,
            dir_fd=parent_fd,
        )
        os.lseek(source.fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source.fd, 1024 * 1024)
            if not chunk:
                break
            source_digest.update(chunk)
            write_all(destination_fd, chunk)
        os.fchmod(destination_fd, 0o640)
        os.fsync(destination_fd)
        after = os.fstat(source.fd)
        if stable_identity(source.initial) != stable_identity(after):
            fail(f"record export source changed during capture: {source.relative}")
        os.lseek(source.fd, 0, os.SEEK_SET)
        verify_digest = hashlib.sha256()
        while True:
            chunk = os.read(source.fd, 1024 * 1024)
            if not chunk:
                break
            verify_digest.update(chunk)
        if source_digest.digest() != verify_digest.digest():
            fail(f"record export source changed during capture: {source.relative}")
        return source_digest.hexdigest()
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.fsync(parent_fd)
        os.close(parent_fd)


def remove_tree_at(parent_fd: int, name: str) -> None:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        fail(f"refusing to remove unsafe record export artifact: {name}")
    fd = open_directory_at(parent_fd, name, f"record export artifact {name}")
    try:
        for child in os.listdir(fd):
            child_info = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(child_info.st_mode):
                remove_tree_at(fd, child)
            elif stat.S_ISREG(child_info.st_mode):
                os.unlink(child, dir_fd=fd)
            else:
                fail(f"refusing to remove unsafe record export artifact: {name}/{child}")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def live_directory_allowed(relative: str) -> bool:
    if relative in {"data", "state"}:
        return True
    parts = relative.split("/")
    if len(parts) == 2 and parts[0] == "data":
        return bool(SAFE_COMPONENT.fullmatch(parts[1]))
    if len(parts) == 2 and parts[0] == "state" and parts[1].endswith(".inbox"):
        return bool(SAFE_COMPONENT.fullmatch(parts[1][: -len(".inbox")]))
    if len(parts) == 3 and parts[0] == "state" and parts[1].endswith(".inbox") \
            and parts[2] == "handled":
        return bool(SAFE_COMPONENT.fullmatch(parts[1][: -len(".inbox")]))
    return False


def live_file_allowed(relative: str) -> bool:
    if relative in {"data/projects.md", "data/backlog.md", "data/done-archive.md", "state/fleet-ledger.jsonl"}:
        return True
    parts = relative.split("/")
    if len(parts) == 3 and parts[0] == "data" and SAFE_COMPONENT.fullmatch(parts[1]):
        return parts[2] in {"brief.md", "report.md"} \
            or (parts[2].startswith("status") and parts[2].endswith(".log") and SAFE_COMPONENT.fullmatch(parts[2]))
    if len(parts) == 2 and parts[0] == "state":
        if parts[1].endswith(".meta"):
            return bool(SAFE_COMPONENT.fullmatch(parts[1][: -len(".meta")]))
        if parts[1].endswith(".status"):
            return bool(SAFE_COMPONENT.fullmatch(parts[1][: -len(".status")]))
    if len(parts) in {3, 4} and parts[0] == "state" and parts[1].endswith(".inbox"):
        task = parts[1][: -len(".inbox")]
        message = parts[-1]
        handled_shape = len(parts) == 3 or (len(parts) == 4 and parts[2] == "handled")
        return bool(SAFE_COMPONENT.fullmatch(task) and handled_shape and INBOX_MESSAGE.fullmatch(message))
    return False


def validate_live_generation(generations_fd: int, name: str) -> None:
    generation_fd = open_directory_at(generations_fd, name, f"record export live generation {name}")
    try:
        def walk(directory_fd: int, prefix: str) -> None:
            for child in os.listdir(directory_fd):
                relative = f"{prefix}/{child}" if prefix else child
                info = os.stat(child, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if not live_directory_allowed(relative):
                        fail(f"unknown record export live directory: {relative}")
                    child_fd = open_directory_at(directory_fd, child, f"record export live directory {relative}")
                    try:
                        walk(child_fd, relative)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(info.st_mode):
                    if not live_file_allowed(relative):
                        fail(f"unknown record export live file: {relative}")
                else:
                    fail(f"unsafe record export live artifact: {relative}")

        walk(generation_fd, "")
    finally:
        os.close(generation_fd)


def digest_fd(fd: int) -> tuple[int, str]:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return size, digest.hexdigest()
        size += len(chunk)
        digest.update(chunk)


@dataclass
class ArchiveSource:
    source_relative: str
    archive_relative: str
    fd: int
    initial: os.stat_result

    def close(self) -> None:
        os.close(self.fd)


def add_archive_source(
    sources: list[ArchiveSource],
    parent_fd: int,
    name: str,
    source_relative: str,
    archive_relative: str,
    label: str,
    required: bool = False,
) -> None:
    try:
        fd, info = open_regular_at(parent_fd, name, label)
    except FileNotFoundError:
        if required:
            fail(f"required record export archive source is missing: {source_relative}")
        return
    sources.append(ArchiveSource(source_relative, archive_relative, fd, info))


def collect_archive_sources(state_fd: int, task: str) -> list[ArchiveSource]:
    sources: list[ArchiveSource] = []
    add_archive_source(
        sources,
        state_fd,
        f"{task}.meta",
        f"state/{task}.meta",
        "meta",
        f"state/{task}.meta",
        required=True,
    )
    add_archive_source(
        sources,
        state_fd,
        f"{task}.status",
        f"state/{task}.status",
        "status",
        f"state/{task}.status",
    )
    inbox_name = f"{task}.inbox"
    try:
        inbox_info = os.stat(inbox_name, dir_fd=state_fd, follow_symlinks=False)
    except FileNotFoundError:
        return sources
    if not stat.S_ISDIR(inbox_info.st_mode):
        fail(f"state/{inbox_name} is not a real directory")
    inbox_fd = open_directory_at(state_fd, inbox_name, f"state/{inbox_name}")
    try:
        for name in sorted(os.listdir(inbox_fd)):
            if INBOX_MESSAGE.fullmatch(name):
                add_archive_source(
                    sources,
                    inbox_fd,
                    name,
                    f"state/{inbox_name}/{name}",
                    f"inbox/{name}",
                    f"state/{inbox_name}/{name}",
                )
        try:
            handled_info = os.stat("handled", dir_fd=inbox_fd, follow_symlinks=False)
        except FileNotFoundError:
            return sources
        if not stat.S_ISDIR(handled_info.st_mode):
            fail(f"state/{inbox_name}/handled is not a real directory")
        handled_fd = open_directory_at(inbox_fd, "handled", f"state/{inbox_name}/handled")
        try:
            for name in sorted(os.listdir(handled_fd)):
                if INBOX_MESSAGE.fullmatch(name):
                    add_archive_source(
                        sources,
                        handled_fd,
                        name,
                        f"state/{inbox_name}/handled/{name}",
                        f"inbox/handled/{name}",
                        f"state/{inbox_name}/handled/{name}",
                    )
        finally:
            os.close(handled_fd)
    finally:
        os.close(inbox_fd)
    return sources


def metadata_spawn_generation(metadata_fd: int) -> str:
    os.lseek(metadata_fd, 0, os.SEEK_SET)
    try:
        text = read_all(metadata_fd).decode("utf-8")
    except UnicodeDecodeError:
        fail("task metadata is not valid UTF-8")
    values = [line.partition("=")[2] for line in text.splitlines() if line.partition("=")[0] == "spawn_gen"]
    if len(values) != 1:
        fail("task metadata must contain exactly one spawn_gen for record export")
    safe_component(values[0], "spawn_gen for record export archive")
    return values[0]


def copy_archive_source(source: ArchiveSource, stage_fd: int) -> dict[str, object]:
    parts = source.archive_relative.split("/")
    for part in parts:
        safe_component(part, "record export archive path")
    parent_fd = ensure_relative_directory(stage_fd, parts[:-1])
    destination_fd = -1
    digest = hashlib.sha256()
    size = 0
    try:
        destination_fd = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o640,
            dir_fd=parent_fd,
        )
        os.lseek(source.fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source.fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            write_all(destination_fd, chunk)
        os.fchmod(destination_fd, 0o640)
        os.fsync(destination_fd)
        if stable_identity(source.initial) != stable_identity(os.fstat(source.fd)):
            fail(f"record export source changed during archive capture: {source.source_relative}")
        verify_size, verify_digest = digest_fd(source.fd)
        if size != verify_size or digest.hexdigest() != verify_digest:
            fail(f"record export source changed during archive capture: {source.source_relative}")
        return {
            "source": source.source_relative,
            "archive": source.archive_relative,
            "bytes": size,
            "sha256": digest.hexdigest(),
        }
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.fsync(parent_fd)
        os.close(parent_fd)


def archive_path_mapping(task: str, archive_relative: str) -> str | None:
    if archive_relative == "meta":
        return f"state/{task}.meta"
    if archive_relative == "status":
        return f"state/{task}.status"
    if re.fullmatch(r"inbox/[0-9]+\.msg", archive_relative):
        return f"state/{task}.inbox/{archive_relative.removeprefix('inbox/')}"
    if re.fullmatch(r"inbox/handled/[0-9]+\.msg", archive_relative):
        return f"state/{task}.inbox/handled/{archive_relative.removeprefix('inbox/handled/')}"
    return None


def validate_manifest_shape(value: object, task: str, spawn_gen: str) -> list[dict[str, object]]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "schema_version", "task", "spawn_gen", "captured_at", "complete", "files"
    }:
        fail("record export archive manifest has unknown or missing fields")
    if value["schema"] != "firstmate-record-export-archive.v1" \
            or type(value["schema_version"]) is not int or value["schema_version"] != 1 \
            or value["task"] != task or value["spawn_gen"] != spawn_gen \
            or type(value["captured_at"]) is not str or not UTC_TIMESTAMP.fullmatch(value["captured_at"]) \
            or type(value["complete"]) is not bool or value["complete"] is not True \
            or type(value["files"]) is not list:
        fail("record export archive manifest has invalid field values")
    entries: list[dict[str, object]] = []
    seen_source: set[str] = set()
    seen_archive: set[str] = set()
    for raw in value["files"]:
        if not isinstance(raw, dict) or set(raw) != {"source", "archive", "bytes", "sha256"}:
            fail("record export archive manifest file entry has unknown or missing fields")
        source = raw["source"]
        archive = raw["archive"]
        byte_count = raw["bytes"]
        digest = raw["sha256"]
        if type(source) is not str or type(archive) is not str \
                or type(byte_count) is not int or byte_count < 0 \
                or type(digest) is not str or not SHA256.fullmatch(digest):
            fail("record export archive manifest file entry has invalid field values")
        if archive_path_mapping(task, archive) != source:
            fail("record export archive manifest contains an invalid source mapping")
        if source in seen_source or archive in seen_archive:
            fail("record export archive manifest contains duplicate file entries")
        seen_source.add(source)
        seen_archive.add(archive)
        entries.append(raw)
    if not entries or entries[0]["archive"] != "meta" or "meta" not in seen_archive:
        fail("record export archive manifest omits mandatory metadata")
    return entries


def walk_generation_files(generation_fd: int) -> dict[str, tuple[int, str]]:
    found: dict[str, tuple[int, str]] = {}

    def walk(directory_fd: int, prefix: str) -> None:
        for name in sorted(os.listdir(directory_fd)):
            relative = f"{prefix}/{name}" if prefix else name
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if relative not in {"inbox", "inbox/handled"}:
                    fail(f"unknown record export archive directory: {relative}")
                child_fd = open_directory_at(directory_fd, name, f"record export archive directory {relative}")
                try:
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                if relative == "manifest.v1.json":
                    continue
                if archive_path_mapping("TASK", relative) is None:
                    fail(f"unknown record export archive file: {relative}")
                fd, _ = open_regular_at(directory_fd, name, f"record export archive file {relative}")
                try:
                    found[relative] = digest_fd(fd)
                finally:
                    os.close(fd)
            else:
                fail(f"unsafe record export archive artifact: {relative}")

    walk(generation_fd, "")
    return found


def load_and_validate_manifest(generation_fd: int, task: str, spawn_gen: str) -> list[dict[str, object]]:
    try:
        manifest_fd, _ = open_regular_at(
            generation_fd, "manifest.v1.json", "record export archive manifest"
        )
    except FileNotFoundError:
        fail("record export archive manifest is missing")
    try:
        value = parse_json(read_all(manifest_fd), "record export archive manifest")
    finally:
        os.close(manifest_fd)
    entries = validate_manifest_shape(value, task, spawn_gen)
    actual = walk_generation_files(generation_fd)
    expected = {entry["archive"]: (entry["bytes"], entry["sha256"]) for entry in entries}
    if actual != expected:
        fail("record export archive manifest does not match its generation files")
    return entries


def validate_committed_against_sources(
    entries: list[dict[str, object]], sources: list[ArchiveSource]
) -> None:
    by_source = {entry["source"]: entry for entry in entries}
    for source in sources:
        entry = by_source.get(source.source_relative)
        if entry is None or entry["archive"] != source.archive_relative:
            fail("committed archive differs from current sources")
        size, digest = digest_fd(source.fd)
        if entry["bytes"] != size or entry["sha256"] != digest:
            fail("committed archive differs from current sources")


def revalidate_archive_sources(state_fd: int, task: str, sources: list[ArchiveSource], entries: list[dict[str, object]]) -> None:
    entry_by_source = {entry["source"]: entry for entry in entries}
    for original in sources:
        size, digest = digest_fd(original.fd)
        entry = entry_by_source[original.source_relative]
        if entry["bytes"] != size or entry["sha256"] != digest:
            fail("record export source changed during archive capture")
    # Reopen the source paths after the slow digest pass, so that a path replaced
    # during that pass is still seen before publication.
    current = collect_archive_sources(state_fd, task)
    try:
        original_keys = [(item.source_relative, item.archive_relative) for item in sources]
        current_keys = [(item.source_relative, item.archive_relative) for item in current]
        if original_keys != current_keys:
            fail("record export source set changed during archive capture")
        for original, reopened in zip(sources, current):
            if fd_identity(original.fd) != fd_identity(reopened.fd) \
                    or stable_identity(original.initial) != stable_identity(os.fstat(original.fd)):
                fail("record export source changed during archive capture")
    finally:
        for source in current:
            source.close()


def write_archive_manifest(
    stage_fd: int, task: str, spawn_gen: str, entries: list[dict[str, object]]
) -> None:
    document = {
        "schema": "firstmate-record-export-archive.v1",
        "schema_version": 1,
        "task": task,
        "spawn_gen": spawn_gen,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "complete": True,
        "files": entries,
    }
    data = (json.dumps(document, indent=2, separators=(",", ": ")) + "\n").encode("utf-8")
    fd = os.open(
        "manifest.v1.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o640,
        dir_fd=stage_fd,
    )
    try:
        write_all(fd, data)
        os.fchmod(fd, 0o640)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(stage_fd)


def prune_archives(archive_fd: int) -> None:
    cutoff = time.time() - 30 * 24 * 60 * 60
    try:
        tasks = os.listdir(archive_fd)
    except OSError as exc:
        print(f"warning: could not scan record export archive: archive: {exc.strerror}", file=sys.stderr)
        return
    for task in tasks:
        if not SAFE_COMPONENT.fullmatch(task):
            print(f"warning: refusing to prune unsafe record export archive: archive/{task}", file=sys.stderr)
            continue
        try:
            task_fd = open_directory_at(archive_fd, task, f"record export archive task {task}")
        except (ExportError, OSError) as exc:
            print(f"warning: could not scan record export archive: archive/{task}: {exc}", file=sys.stderr)
            continue
        try:
            try:
                generations = os.listdir(task_fd)
            except OSError as exc:
                print(f"warning: could not scan record export archive: archive/{task}: {exc.strerror}", file=sys.stderr)
                continue
            for spawn_gen in generations:
                if not SAFE_COMPONENT.fullmatch(spawn_gen):
                    if not ARCHIVE_STAGE_NAME.fullmatch(spawn_gen):
                        print(
                            f"warning: refusing to prune unsafe record export archive: archive/{task}/{spawn_gen}",
                            file=sys.stderr,
                        )
                    continue
                try:
                    generation_fd = open_directory_at(
                        task_fd, spawn_gen, f"record export archive generation {task}/{spawn_gen}"
                    )
                except (ExportError, OSError) as exc:
                    print(
                        f"warning: could not scan record export archive: archive/{task}/{spawn_gen}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                try:
                    try:
                        manifest_info = os.stat(
                            "manifest.v1.json", dir_fd=generation_fd, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(manifest_info.st_mode) or manifest_info.st_mtime >= cutoff:
                        continue
                    try:
                        load_and_validate_manifest(generation_fd, task, spawn_gen)
                    except ExportError:
                        print(
                            f"warning: refusing to prune unsafe record export archive: archive/{task}/{spawn_gen}",
                            file=sys.stderr,
                        )
                        continue
                finally:
                    os.close(generation_fd)
                try:
                    remove_tree_at(task_fd, spawn_gen)
                except (ExportError, OSError) as exc:
                    print(
                        f"warning: could not prune record export archive: archive/{task}/{spawn_gen}: {exc}",
                        file=sys.stderr,
                    )
        finally:
            os.close(task_fd)


def archive_task(home: str, config: str, data: str, state: str, task: str) -> None:
    safe_component(task, "task id for record export archive")
    configured_root = read_config(config)
    if configured_root is None:
        return
    state_fd = open_absolute_directory(state, "state root")
    root_fd = -1
    sources: list[ArchiveSource] = []
    try:
        sources = collect_archive_sources(state_fd, task)
        spawn_gen = metadata_spawn_generation(sources[0].fd)
        root_fd = open_export_root(
            configured_root,
            home,
            (("home", home), ("config", config), ("data", data), ("state", state)),
        )
        archive_fd = ensure_directory_at(root_fd, "archive", "record export archive root")
        try:
            task_fd = ensure_directory_at(archive_fd, task, "record export archive task")
            try:
                try:
                    existing_fd = open_directory_at(
                        task_fd, spawn_gen, f"record export archive generation {task}/{spawn_gen}"
                    )
                except ExportError:
                    try:
                        os.stat(spawn_gen, dir_fd=task_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        existing_fd = -1
                    else:
                        raise
                if existing_fd >= 0:
                    try:
                        if "manifest.v1.json" in os.listdir(existing_fd):
                            entries = load_and_validate_manifest(existing_fd, task, spawn_gen)
                            validate_committed_against_sources(entries, sources)
                            prune_archives(archive_fd)
                            return
                        walk_generation_files(existing_fd)
                    finally:
                        os.close(existing_fd)

                stage_name = f".archive-stage-{os.getpid()}-{uuid.uuid4().hex}"
                os.mkdir(stage_name, 0o750, dir_fd=task_fd)
                stage_fd = open_directory_at(task_fd, stage_name, "record export archive staging generation")
                try:
                    entries = [copy_archive_source(source, stage_fd) for source in sources]
                    write_archive_manifest(stage_fd, task, spawn_gen, entries)
                    current_task = os.stat(task, dir_fd=archive_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(current_task.st_mode) \
                            or stat_identity(current_task) != fd_identity(task_fd):
                        fail("record export archive task directory changed before publication")
                    if existing_fd >= 0:
                        remove_tree_at(task_fd, spawn_gen)
                    revalidate_archive_sources(state_fd, task, sources, entries)
                    os.rename(stage_name, spawn_gen, src_dir_fd=task_fd, dst_dir_fd=task_fd)
                    os.fsync(task_fd)
                    os.fsync(archive_fd)
                    os.fsync(root_fd)
                except Exception:
                    try:
                        remove_tree_at(task_fd, stage_name)
                    except (FileNotFoundError, ExportError, OSError):
                        pass
                    raise
                finally:
                    os.close(stage_fd)
            finally:
                os.close(task_fd)
            prune_archives(archive_fd)
        finally:
            os.close(archive_fd)
    finally:
        for source in sources:
            source.close()
        if root_fd >= 0:
            os.close(root_fd)
        os.close(state_fd)


def sync_records(home: str, config: str, data: str, state: str) -> None:
    configured_root = read_config(config)
    if configured_root is None:
        return
    data_fd = open_absolute_directory(data, "data root")
    state_fd = open_absolute_directory(state, "state root")
    root_fd = -1
    sources: list[Source] = []
    try:
        root_fd = open_export_root(
            configured_root,
            home,
            (("home", home), ("config", config), ("data", data), ("state", state)),
        )
        sources = collect_data_sources(data_fd) + collect_state_sources(state_fd)
        generations_fd = ensure_directory_at(root_fd, LIVE_GENERATIONS, "record export live generations")
        try:
            for existing in os.listdir(generations_fd):
                if LIVE_POINTER_NAME.fullmatch(existing):
                    info = os.stat(existing, dir_fd=generations_fd, follow_symlinks=False)
                    if not stat.S_ISLNK(info.st_mode):
                        fail("record export stale live pointer is unsafe")
                    target = os.readlink(existing, dir_fd=generations_fd)
                    if not re.fullmatch(r"\.live-generations/" + GENERATION_NAME.pattern[1:-1], target):
                        fail("record export stale live pointer has an unsafe target")
                    os.unlink(existing, dir_fd=generations_fd)
                    os.fsync(generations_fd)
                    continue
                if not (GENERATION_NAME.fullmatch(existing) or STAGE_NAME.fullmatch(existing)):
                    fail(f"unknown record export live generation artifact: {existing}")
                validate_live_generation(generations_fd, existing)
            stage_name = f".stage-{os.getpid()}-{uuid.uuid4().hex}"
            os.mkdir(stage_name, 0o750, dir_fd=generations_fd)
            stage_fd = open_directory_at(generations_fd, stage_name, "record export live staging generation")
            try:
                for source in sources:
                    copy_source(source, stage_fd)
                os.fsync(stage_fd)
            except Exception:
                os.close(stage_fd)
                stage_fd = -1
                remove_tree_at(generations_fd, stage_name)
                raise
            finally:
                if stage_fd >= 0:
                    os.close(stage_fd)

            current_generations = os.stat(LIVE_GENERATIONS, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(current_generations.st_mode) \
                    or stat_identity(current_generations) != fd_identity(generations_fd):
                remove_tree_at(generations_fd, stage_name)
                fail("record export live generations directory changed before publication")

            generation_name = f"g{time.time_ns()}.{os.getpid()}.{uuid.uuid4().hex}"
            os.rename(stage_name, generation_name, src_dir_fd=generations_fd, dst_dir_fd=generations_fd)
            os.fsync(generations_fd)
            pointer_name = f".live-next-{os.getpid()}-{uuid.uuid4().hex}"
            os.symlink(f"{LIVE_GENERATIONS}/{generation_name}", pointer_name, dir_fd=generations_fd)
            try:
                os.replace(pointer_name, "live", src_dir_fd=generations_fd, dst_dir_fd=root_fd)
                os.fsync(generations_fd)
                os.fsync(root_fd)
            finally:
                try:
                    os.unlink(pointer_name, dir_fd=generations_fd)
                except FileNotFoundError:
                    pass

            for name in os.listdir(generations_fd):
                if name == generation_name:
                    continue
                if not (GENERATION_NAME.fullmatch(name) or STAGE_NAME.fullmatch(name)):
                    fail(f"unknown record export live generation artifact: {name}")
                if GENERATION_NAME.fullmatch(name):
                    info = os.stat(name, dir_fd=generations_fd, follow_symlinks=False)
                    if info.st_mtime >= time.time() - 30 * 24 * 60 * 60:
                        continue
                try:
                    remove_tree_at(generations_fd, name)
                except ExportError as exc:
                    print(f"warning: could not remove stale live generation {name}: {exc}", file=sys.stderr)
        finally:
            os.close(generations_fd)
    finally:
        for source in sources:
            source.close()
        if root_fd >= 0:
            os.close(root_fd)
        os.close(state_fd)
        os.close(data_fd)


def env_or(name: str, default: str) -> str:
    return os.environ.get(name) or default


REPORT_CATALOGUE = "catalogue.v1.json"
SECONDMATE_MARKER = ".fm-secondmate-home"
UNKNOWN = "unknown"
URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
REPORT_DATE = re.compile(r"^date:\s*(\d{4}-\d\d-\d\d)\b", re.IGNORECASE)
LINK_ATTRIBUTES = {"link_open": "href", "image": "src"}
CATALOGUE_TEXT_FIELDS = (
    "id", "title", "summary", "project", "origin_home", "origin_path", "task_id",
    "authored", "completed", "captured_at", "source", "source_sha256", "capture",
)


@dataclass(frozen=True)
class Origin:
    """The home a report comes from: a readable label and its canonical path."""

    label: str
    path: str


@dataclass(frozen=True)
class ReportReading:
    """What a report's Markdown says about itself, with unknown where it says nothing."""

    title: str
    summary: str
    authored: str
    links: tuple[str, ...]


class LinkKind(Enum):
    WEB = "web"
    OUTSIDE = "outside"
    MATERIAL = "material"


def home_origin(home: str) -> Origin:
    """Label a secondmate home by its marker id and any other home as main."""
    path = canonical_path(home)
    marker = os.path.join(path, SECONDMATE_MARKER)
    try:
        info = os.lstat(marker)
    except FileNotFoundError:
        return Origin("main", path)
    if not stat.S_ISREG(info.st_mode):
        return Origin("main", path)
    with open(marker, "rb") as handle:
        label = "".join(os.fsdecode(handle.readline()).split())
    return Origin(label if SAFE_COMPONENT.fullmatch(label) else "main", path)


def plain_text(inline: object) -> str:
    words: list[str] = []
    for child in getattr(inline, "children", None) or ():
        if child.type in {"text", "code_inline"}:
            words.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            words.append(" ")
    return " ".join("".join(words).split())


def read_report_markdown(text: str) -> ReportReading:
    """Take the first h1 as title, the first top-level paragraph as summary, and every link."""
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark")
    # The default rejects file: destinations; the catalogue lists them as uncaptured.
    parser.validateLink = lambda _url: True
    tokens = parser.parse(text)
    title = summary = authored = UNKNOWN
    links: dict[str, None] = {}
    for opener, inline in zip(tokens, tokens[1:]):
        if inline.type != "inline":
            continue
        words = plain_text(inline)
        if opener.type == "heading_open" and opener.tag == "h1" and title == UNKNOWN and words:
            title = words[:240]
        elif opener.type == "paragraph_open" and opener.level == 0:
            date = REPORT_DATE.match(words)
            if date and authored == UNKNOWN:
                authored = date[1]
            elif not date and summary == UNKNOWN and words[:1].isalpha():
                summary = words[:400]
        for child in inline.children or ():
            attribute = LINK_ATTRIBUTES.get(child.type)
            target = child.attrGet(attribute) if attribute else None
            if isinstance(target, str) and target:
                links[target] = None
    return ReportReading(title, summary, authored, tuple(links))


def classify_link(link: str) -> tuple[LinkKind, str]:
    """Sort a report link by what publication does with it.

    A MATERIAL link also returns its normalized path relative to the task folder.
    """
    path = re.split(r"[?#]", link, maxsplit=1)[0]
    if not path or path.startswith("//") or (URL_SCHEME.match(path) and not path.lower().startswith("file:")):
        return LinkKind.WEB, ""
    relative = posixpath.normpath(unquote(path))
    if path.startswith("/") or URL_SCHEME.match(path) or relative == ".." or relative.startswith("../"):
        return LinkKind.OUTSIDE, ""
    return LinkKind.MATERIAL, relative


@contextmanager
def report_collection(config: str, protected: list[tuple[str, str]]):
    root = read_config(config, REPORT_CONFIG)
    if root is None:
        fail(f"configure {REPORT_CONFIG} first")
    record_root = read_config(config)
    if record_root is not None:
        protected = [*protected, ("operational export", record_root)]
    document: dict[str, object] = {"schema": REPORT_SCHEMA, "schema_version": 1}
    with ExitStack() as stack:
        root_fd = open_marked_root(root, document, protected, "report collection", REPORT_MARKER)
        stack.callback(os.close, root_fd)
        # Every home that shares the collection takes this lock before it writes.
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        os.fchmod(root_fd, 0o750)
        os.fsync(root_fd)
        reports_fd = ensure_directory_at(root_fd, "reports", "report packages")
        stack.callback(os.close, reports_fd)
        for entry in os.listdir(reports_fd):
            if STAGE_NAME.fullmatch(entry):
                remove_tree_at(reports_fd, entry)
        yield root_fd, reports_fd


def open_report_file(root_fd: int, relative: str) -> Source:
    parts = relative.split("/")
    if not all(plain_name(part) for part in parts):
        fail(f"unsafe report path: {relative!r}")
    with ExitStack() as stack:
        parent_fd = root_fd
        for part in parts[:-1]:
            parent_fd = open_directory_at(parent_fd, part, f"report directory {part!r}")
            stack.callback(os.close, parent_fd)
        fd, info = open_regular_at(parent_fd, parts[-1], f"report file {relative!r}")
        return Source(relative, fd, info)


def find_materials(task_fd: int, links: Iterable[str]) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Digest each task-folder file a report links.

    Returns the digests by relative path, the links outside the task folder,
    and each material link that cannot be published with the reason.
    """
    digests: dict[str, str] = {}
    outside: list[str] = []
    failed: dict[str, str] = {}
    for link in links:
        kind, relative = classify_link(link)
        if kind is LinkKind.WEB or relative in digests or relative == "report.md":
            continue
        if kind is LinkKind.OUTSIDE:
            outside.append(link)
            continue
        try:
            source = open_report_file(task_fd, relative)
        except FileNotFoundError:
            failed[link] = "the file does not exist in the task folder"
            continue
        except ExportError as exc:
            failed[link] = str(exc)
            continue
        try:
            digests[relative] = digest_fd(source.fd)[1]
        finally:
            source.close()
    return digests, outside, failed


def load_catalogue(home_fd: int, label: str, name: str) -> dict:
    with ExitStack() as stack:
        package_fd = open_directory_at(home_fd, name, f"report package {label}/{name}")
        stack.callback(os.close, package_fd)
        fd, _ = open_regular_at(package_fd, REPORT_CATALOGUE, "report catalogue record")
        stack.callback(os.close, fd)
        record = parse_json(read_all(fd), "report catalogue record")
    if (
        not isinstance(record, dict)
        or record.get("id") != f"{label}/{name}"
        or any(not isinstance(record.get(field), str) for field in CATALOGUE_TEXT_FIELDS)
        or type(record.get("revision")) is not int
        or type(record.get("legacy")) is not bool
        or not isinstance(record.get("files"), dict)
        or "report.md" not in record["files"]
        or not isinstance(record.get("uncaptured_links"), list)
    ):
        fail(f"invalid report catalogue record: {label}/{name}")
    return record


def read_packages(home_fd: int, label: str) -> dict[str, dict]:
    """A home folder's catalogue records by package name."""
    return {
        name: load_catalogue(home_fd, label, name)
        for name in sorted(os.listdir(home_fd))
        if not name.startswith(".")
    }


def package_name(task: str, taken: Iterable[str]) -> str:
    """The task id, or the task id with the first free -N suffix from -2."""
    taken = set(taken)
    name, count = task, 1
    while name in taken:
        count += 1
        name = f"{task}-{count}"
    return name


def copy_files(task_fd: int, stage_fd: int, files: dict[str, str]) -> None:
    """Copy each task-folder file into the stage, refusing one whose bytes no longer match its digest."""
    for relative, digest in files.items():
        source = open_report_file(task_fd, relative)
        try:
            if copy_source(source, stage_fd) != digest:
                fail(f"report file changed during publication: {relative!r}")
        finally:
            source.close()


def publish_report(
    reports_fd: int, origin: Origin, data: str, source_path: str, project: str, *, legacy: bool, now: datetime
) -> None:
    task = os.path.basename(os.path.dirname(source_path))
    with ExitStack() as stack:
        task_fd = open_absolute_directory(os.path.join(data, os.path.dirname(source_path)), "report task directory")
        stack.callback(os.close, task_fd)
        report = open_report_file(task_fd, "report.md")
        stack.callback(report.close)
        text = read_all(report.fd)
        reading = read_report_markdown(text.decode("utf-8", errors="replace"))
        materials, outside, failed = find_materials(task_fd, reading.links)
        if failed and not legacy:
            details = "; ".join(f"{link}: {why}" for link, why in failed.items())
            fail(f"cannot publish the report for {task}; fix these links and retry: {details}")
        files = {"report.md": hashlib.sha256(text).hexdigest(), **materials}
        home_fd = ensure_directory_at(reports_fd, origin.label, "report home directory")
        stack.callback(os.close, home_fd)
        packages = read_packages(home_fd, origin.label)
        same_task = {
            name: record for name, record in packages.items()
            if record["origin_path"] == origin.path and record["task_id"] == task
        }
        if any(record["files"] == files for record in same_task.values()):
            return
        stage = f".stage-{os.getpid()}-{uuid.uuid4().hex}"
        os.mkdir(stage, 0o750, dir_fd=reports_fd)
        try:
            stage_fd = open_directory_at(reports_fd, stage, "report staging directory")
            stack.callback(os.close, stage_fd)
            copy_files(task_fd, stage_fd, files)
            uncaptured = [*outside, *failed]
            name = package_name(task, os.listdir(home_fd))
            record = {
                "id": f"{origin.label}/{name}",
                "title": reading.title,
                "summary": reading.summary,
                "project": project,
                "origin_home": origin.label,
                "origin_path": origin.path,
                "task_id": task,
                "revision": len(same_task) + 1,
                "legacy": legacy,
                "authored": reading.authored,
                "completed": UNKNOWN if legacy else now.date().isoformat(),
                "captured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": source_path,
                "source_sha256": files["report.md"],
                "files": files,
                "uncaptured_links": uncaptured,
                "capture": "partial" if uncaptured else "complete",
            }
            atomic_write_at(stage_fd, REPORT_CATALOGUE, (json.dumps(record, sort_keys=True) + "\n").encode())
            os.rename(stage, name, src_dir_fd=reports_fd, dst_dir_fd=home_fd)
            os.fsync(home_fd)
            os.fsync(reports_fd)
        finally:
            if stage in os.listdir(reports_fd):
                remove_tree_at(reports_fd, stage)


def collection_records(reports_fd: int) -> list[dict]:
    records: list[dict] = []
    for label in sorted(os.listdir(reports_fd)):
        if label.startswith("."):
            continue
        home_fd = open_directory_at(reports_fd, label, f"report home directory {label}")
        try:
            records.extend(read_packages(home_fd, label).values())
        finally:
            os.close(home_fd)
    return records


def escape_markdown(value: str) -> str:
    return re.sub(r"([\\\[\]|*_`])", r"\\\1", html.escape(value, quote=False))


def report_date(record: dict) -> str:
    return record["authored"] if record["authored"] != UNKNOWN else record["completed"]


def render_report_index(records: list[dict]) -> str:
    """The collection's Markdown entry point: homes, then reports grouped by project, newest first."""
    lines = ["# Scout reports", "", "## Homes", "", "| Home | Reports | Last capture |", "| --- | --- | --- |"]
    homes: dict[str, list[dict]] = {}
    projects: dict[str, list[dict]] = {}
    for record in sorted(records, key=lambda row: row["id"]):
        homes.setdefault(record["origin_home"], []).append(record)
        projects.setdefault(record["project"], []).append(record)
    for home in sorted(homes):
        last = max(row["captured_at"] for row in homes[home])
        lines.append(f"| {escape_markdown(home)} | {len(homes[home])} | {last} |")
    for project in sorted(projects, key=lambda name: (name == UNKNOWN, name)):
        heading = "Project unknown" if project == UNKNOWN else escape_markdown(project)
        lines.extend(["", f"## {heading}", ""])
        rows = sorted(projects[project], key=lambda row: (report_date(row) != UNKNOWN, report_date(row)), reverse=True)
        for row in rows:
            title = row["title"] if row["title"] != UNKNOWN else row["task_id"]
            notes = ["legacy"] if row["legacy"] else []
            if row["capture"] == "partial":
                notes.append("partial capture")
            suffix = f" ({', '.join(notes)})" if notes else ""
            lines.append(f"- [{escape_markdown(title)}](reports/{row['id']}/report.md){suffix}  ")
            lines.append(f"  {report_date(row)} · {escape_markdown(row['id'])}  ")
            lines.append(f"  {escape_markdown(row['summary'])}")
    return "\n".join(lines) + "\n"


def legacy_reports(data: str, state: str) -> Iterable[str]:
    for relative in ("", "old"):
        root = os.path.join(data, relative)
        try:
            fd = open_absolute_directory(root, "legacy reports directory")
        except FileNotFoundError:
            if not relative:
                raise
            continue
        try:
            for task in sorted(os.listdir(fd)):
                if not SAFE_COMPONENT.fullmatch(task) or (not relative and task == "old"):
                    continue
                info = os.stat(task, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    continue
                if not relative and os.path.lexists(os.path.join(state, f"{task}.meta")):
                    continue
                candidate = os.path.join(relative, task, "report.md")
                if os.path.lexists(os.path.join(data, candidate)):
                    yield candidate
        finally:
            os.close(fd)


def scout_metadata(state: str, task: str) -> dict[str, str]:
    safe_component(task, "scout task id")
    with ExitStack() as stack:
        state_fd = open_absolute_directory(state, "state root")
        stack.callback(os.close, state_fd)
        meta_fd, _ = open_regular_at(state_fd, f"{task}.meta", "scout task metadata")
        stack.callback(os.close, meta_fd)
        metadata = dict(line.split("=", 1) for line in os.fsdecode(read_all(meta_fd)).splitlines() if "=" in line)
    if metadata.get("kind") != "scout":
        fail(f"task {task} is not a scout")
    return metadata


def run_report_export(home: str, config: str, data: str, state: str, argv: list[str]) -> None:
    now = datetime.now(timezone.utc)
    origin = home_origin(home)
    protected = [("home", home), ("config", config), ("data", data), ("state", state)]
    project = UNKNOWN
    if argv[0] == "publish-report":
        project = os.path.basename(scout_metadata(state, argv[1]).get("project", "").rstrip("/")) or UNKNOWN
    with report_collection(config, protected) as (root_fd, reports_fd):
        if argv[0] == "publish-report":
            publish_report(reports_fd, origin, data, f"{argv[1]}/report.md", project, legacy=False, now=now)
        elif argv[0] == "import-reports":
            for relative in legacy_reports(data, state):
                publish_report(reports_fd, origin, data, relative, UNKNOWN, legacy=True, now=now)
        index = render_report_index(collection_records(reports_fd))
        atomic_write_at(root_fd, "index.md", index.encode("utf-8"))


def main(argv: list[str]) -> int:
    script_dir = os.path.dirname(os.path.realpath(__file__))
    root = env_or("FM_ROOT_OVERRIDE", os.path.dirname(script_dir))
    home = env_or("FM_HOME", root)
    config = env_or("FM_CONFIG_OVERRIDE", os.path.join(home, "config"))
    data = env_or("FM_DATA_OVERRIDE", os.path.join(home, "data"))
    state = env_or("FM_STATE_OVERRIDE", os.path.join(home, "state"))
    if argv == ["sync"]:
        sync_records(home, config, data, state)
    elif len(argv) == 2 and argv[0] == "archive-task":
        archive_task(home, config, data, state, argv[1])
    elif argv in (["import-reports"], ["index-reports"]) or len(argv) == 2 and argv[0] == "publish-report":
        run_report_export(home, config, data, state, argv)
    else:
        print("usage: see fm-record-export.sh", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except OSError as exc:
        detail = exc.strerror or str(exc)
        if exc.filename:
            detail = f"{detail}: {exc.filename}"
        print(f"error: record export filesystem operation failed: {detail}", file=sys.stderr)
        raise SystemExit(1)
