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
import re
import stat
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import quote_from_bytes, unquote_to_bytes


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


class UnresolvedReportLink(ExportError):
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
    if name in {"", ".", ".."} or "/" in name or "\x00" in name:
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


def root_marker(home: str, schema: str) -> bytes:
    document = {
        "schema": schema,
        "schema_version": 1,
        "home": canonical_path(home),
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def validate_root_marker(root_fd: int, home: str, marker: str, schema: str, label: str) -> None:
    try:
        fd, _ = open_regular_at(root_fd, marker, f"{label} ownership marker")
    except FileNotFoundError:
        fail(f"existing {label} root has no ownership marker")
    try:
        value = parse_json(read_all(fd), f"{label} ownership marker")
    finally:
        os.close(fd)
    expected = {
        "schema": schema,
        "schema_version": 1,
        "home": canonical_path(home),
    }
    if value != expected:
        fail(f"{label} root is owned by another home or has an invalid marker")


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


def open_owned_root(
    root: str, home: str, protected: Iterable[tuple[str, str]], label: str, marker: str, schema: str
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
                write_all(fd, root_marker(home, schema))
                os.fchmod(fd, 0o640)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(root_fd)
        validate_root_marker(root_fd, home, marker, schema, label)
        return root_fd
    except Exception:
        os.close(root_fd)
        raise


def open_export_root(root: str, home: str, protected: Iterable[tuple[str, str]]) -> int:
    root_fd = open_owned_root(root, home, protected, "record export", ROOT_MARKER, ROOT_SCHEMA)
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
    for part in parts:
        if part in {"", ".", ".."} or "\x00" in part:
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
REPORT_LINK = re.compile(
    rb"(?P<code>(?<!\x60)(?P<ticks>\x60+)(?!\x60).*?(?<!\x60)(?P=ticks)(?!\x60))"
    rb"|(?P<escape>\\.)|(?P<opening>\[)|(?P<closing>\])"
)
REPORT_REFERENCE = re.compile(rb"^ {0,3}\[[^\]\r\n]+\]:[ \t]*")
REPORT_TITLE = rb'''(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|\((?:\\.|[^)\\\r\n])*\))'''
REPORT_FENCE = re.compile(rb"^ {0,3}(\x60{3,}|~{3,})([^\r\n]*)")
REPORT_DATE = re.compile(rb"^(?:\*\*)?date(?:\*\*)?:(?:\*\*)?\s*(\d{4}-\d\d-\d\d)\b", re.I)


def report_destination(content: bytes, position: int, reference: bool) -> tuple[int, int, int] | None:
    while content[position:position + 1] in (b" ", b"\t"):
        position += 1
    start = position
    angled = content[position:position + 1] == b"<"
    if angled:
        position += 1
    depth = 0
    while position < len(content):
        char = content[position:position + 1]
        if char == b"\\" and position + 1 < len(content):
            if content[position + 1:position + 2].isspace():
                return None
            position += 2
            continue
        if char in (b"\r", b"\n", b"<"):
            break
        if angled:
            if char == b">":
                position += 1
                break
        elif char.isspace():
            break
        elif char == b"(":
            depth += 1
        elif char == b")":
            if not depth:
                break
            depth -= 1
        position += 1
    if position == start or depth or angled and content[position - 1:position] != b">":
        return None
    end = position
    ending = rb"(?:[ \t]+" + REPORT_TITLE + rb")?[ \t]*" + (rb"(?:\r?\n)?$" if reference else rb"\)")
    closing = re.match(ending, content[end:])
    if closing is None:
        return None
    return start, end, end + closing.end()


def report_destinations(content: bytes) -> Iterable[tuple[int, int]]:
    if reference := REPORT_REFERENCE.match(content):
        if destination := report_destination(content, reference.end(), True):
            yield destination[0], destination[1]
        return
    brackets = 0
    position = 0
    while match := REPORT_LINK.search(content, position):
        position = match.end()
        if match["opening"] is not None:
            brackets += 1
        if match["closing"] is None or not brackets:
            continue
        brackets -= 1
        if content[position:position + 1] != b"(":
            continue
        if destination := report_destination(content, position + 1, False):
            yield destination[0], destination[1]
            position = destination[2]


@contextmanager
def report_collection(home: str, config: str, protected: list[tuple[str, str]]):
    root = read_config(config, REPORT_CONFIG)
    if root is None:
        fail(f"configure {REPORT_CONFIG} first")
    record_root = read_config(config)
    if record_root is not None:
        protected.append(("operational export", record_root))
    with ExitStack() as stack:
        config_fd = open_absolute_directory(config, "report collection config directory")
        stack.callback(os.close, config_fd)
        fcntl.flock(config_fd, fcntl.LOCK_EX)
        root_fd = open_owned_root(root, home, protected, "report collection", REPORT_MARKER, REPORT_SCHEMA)
        stack.callback(os.close, root_fd)
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
    if any(not part or part.startswith(".") or "\x00" in part for part in parts):
        fail(f"unsafe report path: {relative!r}")
    with ExitStack() as stack:
        parent_fd = root_fd
        for part in parts[:-1]:
            os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            parent_fd = open_directory_at(parent_fd, part, f"report directory {part!r}")
            stack.callback(os.close, parent_fd)
        fd, info = open_regular_at(parent_fd, parts[-1], f"report file {relative!r}")
        return Source(relative, fd, info)


def report_attachment(
    target: bytes, task_dir: str, worktree: str, task_fd: int, stage_fd: int, files: dict[str, str],
    retained: tuple[int, dict[str, str]] | None, worktree_owned: bool,
) -> tuple[bytes, bool]:
    parts = re.split(rb"([?#])", target, maxsplit=1)
    path, suffix = parts[0], b"".join(parts[1:])
    if not path or path.startswith(b"//"):
        return target, False
    if path.startswith(b"file:///"):
        path = path[len(b"file://"):]
    elif re.match(rb"^[a-zA-Z][a-zA-Z0-9+.-]*:", path):
        return target, False
    decoded = os.fsdecode(unquote_to_bytes(re.sub(rb"\\([!-/:-@\[-\x60{-~])", rb"\1", path)))
    if ".." in decoded.split("/") or "\x00" in decoded:
        raise UnresolvedReportLink(f"unsafe local report link: {decoded!r}")
    absolute = os.path.normpath(os.path.join(task_dir, decoded))
    if absolute == os.path.join(task_dir, "report.md"):
        return b"report.md" + suffix, False
    if absolute.startswith(task_dir + "/"):
        relative = os.path.relpath(absolute, task_dir)
        source_path, destination = "", f"files/data/{relative}"
    elif worktree and absolute.startswith(worktree + "/"):
        relative = os.path.relpath(absolute, worktree)
        source_path, destination = worktree, f"files/worktree/{relative}"
    else:
        raise UnresolvedReportLink(f"local report link is outside the task directory and worktree: {decoded!r}")
    borrowed = False
    if destination not in files:
        with ExitStack() as stack:
            expected = None
            try:
                root_fd = task_fd
                if source_path:
                    if worktree_owned:
                        root_fd = open_absolute_directory(source_path, "scout worktree")
                        stack.callback(os.close, root_fd)
                    else:
                        if retained is None or destination not in retained[1]:
                            raise UnresolvedReportLink(f"scout worktree is no longer owned by this task: {worktree}")
                        root_fd, relative = retained[0], destination
                        expected = retained[1][destination]
                        borrowed = True
                source = open_report_file(root_fd, relative)
            except FileNotFoundError as exc:
                if retained is None or destination not in retained[1]:
                    raise UnresolvedReportLink(str(exc)) from exc
                source = open_report_file(retained[0], destination)
                expected = retained[1][destination]
                borrowed = True
            except ExportError as exc:
                raise UnresolvedReportLink(str(exc)) from exc
            stack.callback(source.close)
            source.relative = destination
            files[destination] = copy_source(source, stage_fd)
            if expected is not None and files[destination] != expected:
                fail(f"retained report file changed: {destination!r}")
    return quote_from_bytes(os.fsencode(destination), safe="/").encode("ascii") + suffix, borrowed


def report_text(value: bytes, limit: int) -> str:
    return " ".join(value[:limit].decode("utf-8", errors="replace").split()) or "unknown"


def rewrite_report_links(
    stage_fd: int, task_fd: int, task_dir: str, worktree: str, record: dict, files: dict[str, str],
    retained: tuple[int, dict[str, str]] | None, worktree_owned: bool,
) -> bool:
    borrowed = False
    original, _ = open_regular_at(stage_fd, "report.md", "staged report")
    with os.fdopen(original, "rb") as source:
        projected = os.open(".report-reading", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640, dir_fd=stage_fd)
        with os.fdopen(projected, "wb") as out:
            changed = False
            fence = b""
            quote_depth = 0
            list_indents: list[int] = []
            for line in source:
                content = line
                depth = 0
                while quote := re.match(rb"^ {0,3}>[ \t]?", content):
                    content = content[quote.end():]
                    depth += 1
                if depth != quote_depth:
                    fence = b""
                    list_indents.clear()
                    quote_depth = depth
                leading = re.match(rb"[ \t]*", content)[0]
                indent = len(leading.expandtabs(4))
                content = content[len(leading):]
                if content.strip():
                    while list_indents and indent < list_indents[-1]:
                        list_indents.pop()
                base = list_indents[-1] if list_indents else 0
                if not fence and indent - base >= 4:
                    out.write(line)
                    continue
                while not fence and (item := re.match(rb"(?:[-+*]|[0-9]{1,9}[.)])[ \t]+", content)):
                    indent += len(item[0].expandtabs(4))
                    list_indents.append(indent)
                    content = content[item.end():]
                    base = indent
                offset = len(line) - len(content)
                marker = REPORT_FENCE.match(content) if indent - base < 4 else None
                if fence:
                    if marker and marker[1].startswith(fence) and not marker[2].strip():
                        fence = b""
                    out.write(line)
                    continue
                if marker and (marker[1].startswith(b"~") or b"`" not in marker[2]):
                    fence = marker[1]
                    out.write(line)
                    continue
                if title := re.match(rb"^ {0,3}#[ \t]+(.+?)\s*#*\s*$", line):
                    if record["title"] == "unknown":
                        record["title"] = report_text(title[1], 240)
                elif date := REPORT_DATE.match(line):
                    if record["date"] == "unknown":
                        record["date"] = date[1].decode("ascii")
                elif record["summary"] == "unknown" and line.strip():
                    summary = report_text(line, 400)
                    if summary[0].isalpha():
                        record["summary"] = summary
                cursor = 0
                for start, end in report_destinations(content):
                    target = content[start:end]
                    destination = target[1:-1] if target.startswith(b"<") else target
                    try:
                        replacement, missing = report_attachment(
                            destination, task_dir, worktree, task_fd, stage_fd, files, retained, worktree_owned
                        )
                        borrowed |= missing
                    except UnresolvedReportLink as exc:
                        if not record["legacy"]:
                            fail(f"cannot publish linked material {os.fsdecode(target)!r}: {exc}")
                        record["unresolved_links"].append(report_text(target, 4096))
                        continue
                    start, end = start + offset, end + offset
                    out.write(line[cursor:start])
                    out.write(replacement if replacement != destination else target)
                    cursor = end
                    changed |= replacement != destination
                out.write(line[cursor:])
            if changed:
                out.flush()
                os.fchmod(out.fileno(), 0o640)
                os.fsync(out.fileno())
    if changed:
        if record["legacy"]:
            os.rename("report.md", "report.original", src_dir_fd=stage_fd, dst_dir_fd=stage_fd)
            files["report.original"] = files["report.md"]
        os.replace(".report-reading", "report.md", src_dir_fd=stage_fd, dst_dir_fd=stage_fd)
        rendered, _ = open_regular_at(stage_fd, "report.md", "portable report")
        try:
            files["report.md"] = digest_fd(rendered)[1]
        finally:
            os.close(rendered)
    else:
        os.unlink(".report-reading", dir_fd=stage_fd)
    return borrowed


def report_catalogue(reports_fd: int, package_id: str, verify_files: bool = False) -> dict:
    if not SHA256.fullmatch(package_id):
        fail(f"unexpected report package: {package_id!r}")
    with ExitStack() as stack:
        package_fd = open_directory_at(reports_fd, package_id, "report package")
        stack.callback(os.close, package_fd)
        fd, _ = open_regular_at(package_fd, REPORT_CATALOGUE, "report catalogue record")
        stack.callback(os.close, fd)
        record = parse_json(read_all(fd), "report catalogue record")
        if not isinstance(record, dict) or record.get("id") != package_id or any(
            not isinstance(record.get(field), str)
            for field in ("title", "summary", "date", "origin_home", "task_id", "source", "source_sha256")
        ) or type(record.get("legacy")) is not bool:
            fail(f"invalid report catalogue record: {package_id}")
        if verify_files:
            files = record.get("files")
            if not isinstance(files, dict) or "report.md" not in files:
                fail(f"report catalogue does not identify its report: {package_id}")
            for relative, digest in files.items():
                file = open_report_file(package_fd, relative)
                try:
                    if digest_fd(file.fd)[1] != digest:
                        fail(f"published report file changed: {relative!r}")
                finally:
                    file.close()
        return record


def rebuild_report_index(root_fd: int, reports_fd: int) -> None:
    records = [report_catalogue(reports_fd, name) for name in os.listdir(reports_fd)]
    records.sort(key=lambda row: (row["date"] != "unknown", row["date"], row["id"]), reverse=True)
    lines = ["# Scout reports", ""]
    for row in records:
        title = re.sub(r"([\\\[\]])", r"\\\1", html.escape(row["title"]))
        summary = html.escape(row["summary"])
        date = html.escape(row["date"])
        origin = html.escape(row["origin_home"])
        task = html.escape(row["task_id"])
        legacy = " · legacy" if row["legacy"] else ""
        lines.extend([f"- [{title}](reports/{row['id']}/report.md){legacy}",
                      f"  {date} · {task} · {origin}  ", f"  {summary}"])
    atomic_write_at(root_fd, "index.md", ("\n".join(lines) + "\n").encode("utf-8"))


def report_worktree_owned(home: str, state: str, task: str, worktree: str) -> bool:
    script_dir = os.path.dirname(os.path.realpath(__file__))
    result = subprocess.run(
        ["bash", "-c", '. "$1/fm-wake-lib.sh" && . "$1/fm-backend.sh" && . "$1/fm-secondmate-registry-lib.sh" '
         '&& fm_task_worktree_source_owned "$2" "$3" "$4"',
         "report-worktree-ownership", script_dir, state, task, worktree],
        env={**os.environ, "FM_HOME": home, "FM_STATE_OVERRIDE": state}, check=False,
    )
    if result.returncode not in (0, 1):
        fail(f"cannot check scout worktree ownership for {task}")
    return result.returncode == 0


def publish_report(
    reports_fd: int, home: str, data: str, relative: str, revision: str, state: str, worktree: str = "",
    legacy: bool = False,
) -> None:
    task = relative.split("/")[-2]
    worktree_owned = report_worktree_owned(home, state, task, worktree) if worktree else True
    with ExitStack() as stack:
        task_dir = os.path.join(data, os.path.dirname(relative))
        task_fd = open_absolute_directory(task_dir, "report task directory")
        stack.callback(os.close, task_fd)
        source = open_report_file(task_fd, "report.md")
        stack.callback(source.close)
        _, source_digest = digest_fd(source.fd)
        identity = (canonical_path(home), task, relative, "legacy" if legacy else "completed", revision, source_digest)
        candidates = []
        for name in os.listdir(reports_fd):
            previous = report_catalogue(reports_fd, name)
            if (previous["origin_home"], previous["task_id"], previous["source"], previous["legacy"],
                previous.get("revision"), previous["source_sha256"]) == (
                    identity[0], task, relative, legacy, revision or source_digest, source_digest):
                candidates.append((os.stat(name, dir_fd=reports_fd, follow_symlinks=False).st_mtime_ns, name))
        retained = None
        if candidates:
            previous_id = max(candidates)[1]
            previous = report_catalogue(reports_fd, previous_id, verify_files=True)
            previous_fd = open_directory_at(reports_fd, previous_id, "retained report package")
            stack.callback(os.close, previous_fd)
            retained = previous_fd, previous["files"]
        record = {
            "origin_home": identity[0], "task_id": task, "source": relative,
            "source_sha256": source_digest, "revision": revision or source_digest, "legacy": legacy,
            "title": "unknown", "summary": "unknown", "date": "unknown", "unresolved_links": [],
        }
        stage = f".stage-{os.getpid()}-{uuid.uuid4().hex}"
        os.mkdir(stage, 0o750, dir_fd=reports_fd)
        try:
            stage_fd = open_directory_at(reports_fd, stage, "report staging directory")
            stack.callback(os.close, stage_fd)
            os.fchmod(stage_fd, 0o750)
            if copy_source(source, stage_fd) != source_digest:
                fail(f"report changed during publication for {task}")
            files = {"report.md": source_digest}
            borrowed = rewrite_report_links(
                stage_fd, task_fd, task_dir, worktree, record, files, retained, worktree_owned
            )
            if retained is not None and borrowed and files != retained[1]:
                fail(f"cannot publish changed evidence with missing supporting files for {relative}")
            if worktree_owned and any(name.startswith("files/worktree/") for name in files) \
                    and not report_worktree_owned(home, state, task, worktree):
                fail(f"scout worktree ownership changed during publication for {task}; retry publication")
            contents = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            package_id = hashlib.sha256(os.fsencode("\x00".join((*identity, contents)))).hexdigest()
            if package_id in os.listdir(reports_fd):
                report_catalogue(reports_fd, package_id, verify_files=True)
                published_fd = open_directory_at(reports_fd, package_id, "published report package")
                stack.callback(os.close, published_fd)
                os.utime(published_fd, None)
                os.fsync(published_fd)
                return
            record["id"] = package_id
            if not legacy:
                if record["title"] == "unknown":
                    record["title"] = task
                if record["date"] == "unknown":
                    record["date"] = datetime.now(timezone.utc).date().isoformat()
            record["files"] = files
            atomic_write_at(stage_fd, REPORT_CATALOGUE, (json.dumps(record, sort_keys=True) + "\n").encode())
            os.rename(stage, package_id, src_dir_fd=reports_fd, dst_dir_fd=reports_fd)
            os.fsync(reports_fd)
        finally:
            if stage in os.listdir(reports_fd):
                remove_tree_at(reports_fd, stage)


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
    if generation := metadata.get("spawn_gen"):
        safe_component(generation, "scout spawn generation")
    return metadata


def run_report_export(home: str, config: str, data: str, state: str, argv: list[str]) -> None:
    protected = [("home", home), ("config", config), ("data", data), ("state", state)]
    if argv[0] == "publish-report":
        task = argv[1]
        metadata = scout_metadata(state, task)
        worktree = metadata.get("worktree", "").rstrip("/")
        project = metadata.get("project", "")
        if worktree:
            if not os.path.isabs(worktree) or canonical_path(worktree) == canonical_path(project):
                fail(f"scout worktree is not an isolated absolute path: {worktree!r}")
            protected.append(("scout worktree", worktree))
        if project:
            protected.append(("scout project", project))
    with report_collection(home, config, protected) as (root_fd, reports_fd):
        if argv[0] == "publish-report":
            publish_report(reports_fd, home, data, f"{task}/report.md", metadata.get("spawn_gen", ""), state, worktree)
        elif argv[0] == "import-reports":
            for relative in legacy_reports(data, state):
                publish_report(reports_fd, home, data, relative, "", state, legacy=True)
        rebuild_report_index(root_fd, reports_fd)


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
