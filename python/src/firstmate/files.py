"""Reading and writing the line-oriented records the bash scripts share.

Text is UTF-8 with surrogate escapes, so any byte a record holds is written
back unchanged.
"""

import contextlib
import os
import tempfile

_ENCODING = "utf-8"
_ERRORS = "surrogateescape"


def decode(data):
    return data.decode(_ENCODING, _ERRORS)


def encode(text):
    return text.encode(_ENCODING, _ERRORS)


def read_text(path):
    """The file's text, or None when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return decode(handle.read())
    except OSError:
        return None


def lines(text):
    """Lines as awk and sed see them: an unterminated last line counts."""
    if not text:
        return []
    parts = text.split("\n")
    if text.endswith("\n"):
        parts.pop()
    return parts


def complete_lines(text):
    """Lines as a bash `while read` loop sees them: only terminated ones."""
    return text.split("\n")[:-1] if text else []


def output(text):
    """A command's output as `$(...)` gives it: trailing newlines removed."""
    return text.rstrip("\n")


def first_line(path):
    """The first line, or empty: `sed -n 1p` inside `$(...)`."""
    found = lines(read_text(path) or "")
    return found[0] if found else ""


def field(path, key):
    """The first `key=value` line's value, or empty: `sed -n 's/^key=//p' | head -n 1`."""
    prefix = key + "="
    for line in lines(read_text(path) or ""):
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""


def tsv(line, count):
    """Split a tab-separated line into exactly `count` fields, the last taking the rest."""
    parts = line.split("\t", count - 1)
    return parts + [""] * (count - len(parts))


def mkstemp(path_prefix):
    """Create a private temporary file named `<path_prefix>.<random>`; return its fd and path."""
    directory, name = os.path.split(path_prefix)
    return tempfile.mkstemp(prefix=name + ".", dir=directory or ".")


def replace(path, data):
    """Write `data` to `path` through a temporary file renamed over it; False on failure."""
    try:
        fd, tmp = mkstemp(path + ".tmp")
    except OSError:
        return False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
        return True
    except OSError:
        remove(tmp)
        return False


def remove(*paths):
    """Remove each path that exists, as `rm -f` does."""
    for path in paths:
        with contextlib.suppress(OSError):
            os.unlink(path)
