"""The `fm` command: one subcommand per converted script."""

import sys
from collections.abc import Callable, Sequence


def _host(args: Sequence[str]) -> int:
    from .host import main

    return main(args)


COMMANDS: dict[str, Callable[[Sequence[str]], int]] = {
    "host": _host,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not args or args[0] not in COMMANDS:
        sys.stderr.write("usage: fm <command> [arguments...]\n")
        return 2
    return COMMANDS[args[0]](args[1:])
