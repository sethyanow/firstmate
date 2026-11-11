"""The `fm` command: one subcommand per converted script."""

import sys

COMMANDS = {}


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if not args or args[0] not in COMMANDS:
        sys.stderr.write("usage: fm <command> [arguments...]\n")
        return 2
    return COMMANDS[args[0]](args[1:])
