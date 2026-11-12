"""A dispatch scope: the rows of the wake queue the branch may claim.

bin/fm-branch-dispatch.mjs prints it as `key=value` lines, after an
`eligible=` line when it judged an attended offer.
"""

from dataclasses import dataclass

from firstmate import files


@dataclass(frozen=True)
class Scope:
    status: str
    corrupted: bool
    rows: tuple[str, ...]
    tasks: str
    unscoped: str
    eligible: bool

    @property
    def claimable(self) -> bool:
        """True when the scan found rows the branch may claim."""
        return self.status != "empty" and bool(self.rows)


def parse(text: str) -> Scope:
    return Scope(
        status=files.field_of(text, "status"),
        corrupted=files.field_of(text, "corrupted") == "1",
        rows=tuple(files.field_of(text, "rows").split()),
        tasks=files.field_of(text, "tasks"),
        unscoped=files.field_of(text, "unscoped"),
        eligible=files.field_of(text, "eligible") == "1",
    )
