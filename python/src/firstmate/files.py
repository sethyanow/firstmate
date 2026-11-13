"""Reading and writing the line-oriented records the bash scripts share.

Text is UTF-8 with surrogate escapes, so any byte a record holds is written
back unchanged.
"""

import contextlib
import os
import tempfile

_ENCODING = "utf-8"
_ERRORS = "surrogateescape"


def decode(data: bytes) -> str:
    return data.decode(_ENCODING, _ERRORS)


def encode(text: str) -> bytes:
    return text.encode(_ENCODING, _ERRORS)


def read_text(path: str) -> str | None:
    """The file's text, or None when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return decode(handle.read())
    except OSError:
        return None


def lines(text: str) -> list[str]:
    """Lines as awk and sed see them: an unterminated last line counts."""
    if not text:
        return []
    parts = text.split("\n")
    if text.endswith("\n"):
        parts.pop()
    return parts


def complete_lines(text: str) -> list[str]:
    """Lines as a bash `while read` loop sees them: only terminated ones."""
    return text.split("\n")[:-1] if text else []


def nonempty(path: str) -> bool:
    """True when `path` is a regular file holding at least one byte (`test -s`)."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def output(text: str) -> str:
    """A command's output as `$(...)` gives it: trailing newlines removed."""
    return text.rstrip("\n")


def first_line(path: str) -> str:
    """The first line, or empty: `sed -n 1p` inside `$(...)`."""
    found = lines(read_text(path) or "")
    return found[0] if found else ""


def field_of(text: str, key: str) -> str:
    """The first `key=value` line's value in `text`, or empty."""
    prefix = key + "="
    for line in lines(text):
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


def tsv(line: str, count: int) -> list[str]:
    """Split a tab-separated line into exactly `count` fields, the last taking the rest."""
    parts = line.split("\t", count - 1)
    return parts + [""] * (count - len(parts))


def mkstemp(path_prefix: str) -> tuple[int, str]:
    """Create a private temporary file named `<path_prefix>.<random>`.
    Its fd and path."""
    directory, name = os.path.split(path_prefix)
    return tempfile.mkstemp(prefix=name + ".", dir=directory or ".")


def scratch(path_prefix: str) -> str | None:
    """Create an empty private file named `<path_prefix>.<random>`. Its path, or None."""
    try:
        fd, path = mkstemp(path_prefix)
    except OSError:
        return None
    os.close(fd)
    return path


def write(path: str, text: str) -> bool:
    """Write `text` over the file in place, as `printf > path` does. False on failure."""
    try:
        with open(path, "wb") as handle:
            handle.write(encode(text))
    except OSError:
        return False
    return True


def replace(path: str, data: bytes) -> bool:
    """Write `data` to `path` through a temporary file renamed over it. False on failure."""
    try:
        fd, tmp = mkstemp(path + ".tmp")
    except OSError:
        return False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except OSError:
        remove(tmp)
        return False
    return True


def remove(*paths: str | None) -> None:
    """Remove each path that exists, as `rm -f` does."""
    for path in paths:
        if path:
            with contextlib.suppress(OSError):
                os.unlink(path)
