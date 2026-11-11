"""fm-supervision-host.sh - the supervision host: watcher-cycle ownership plus a
headless engine session that runs the supervision branch's contract beside a
non-Pi primary (docs/supervision-host.md owns the design).

Usage:
  fm-supervision-host.sh park [--restart]

A primary's arm owner runs this in place of bin/fm-watch-arm.sh when the home
runs the host (by default on Claude, by config/supervision-host elsewhere,
never with config/supervision-host-off; docs/configuration.md "Supervision
host"): the Claude Stop auto-arm
(bin/fm-claude-stop-autoarm.sh), the Cursor stop-hook park
(bin/fm-turnend-guard-cursor.sh), the OpenCode TUI plugin
(.opencode/plugins/fm-primary-watch-arm.js), the omp watch extension
(.omp/extensions/fm-primary-omp-watch.ts), Grok's model-owned background arm
(docs/supervision-protocols/grok.md), Codex's opt-in native Stop park
(bin/fm-codex-stop-park.sh), and Codex's foreground checkpoint
(bin/fm-watch-checkpoint.sh). To that owner it IS an arm: it prints the
arm's own lines and exits only when main is needed, and stays parked across
every close it handled itself. Each owner passes its harness as
FM_SUPERVISION_HOST_PRIMARY, which the engine carries as the primary pin.

OUTPUT, the contract every owner reads. The first cycle's status line
("watcher: started ..." or "watcher: attached ...") is printed as soon as the
arm prints it, so an owner that waits for arm readiness sees it at once;
everything else is printed in one write when the host exits: the close as
the arm printed it (without that status line), then any "supervision-host:"
lines. A "supervision-host:" line is a wake in its own right (the park
boundary prints nothing else); "supervision-host stood down: ..." means this
session or generation no longer owns supervision and the owner stands down
silently; an exit status above 128, or no output at all, means the host
itself died and the owner retries it. Any other close is judged exactly as
the arm's. --restart starts the first cycle with fm-watch-arm.sh --restart,
and an FM_WATCH_PREDECESSOR_ARM_PID the owner passes reaches that first
cycle only, for owners that start their own successor after every close
(OpenCode, omp).

THE LOOP. It owns watcher cycles through bin/fm-watch-arm.sh. The posture is
the away-posture record state/.afk-contract, read at every close and again
when a turn starts: only an away record is away, and no record or quiet
mode's record (fm_afk_contract_away_present, bin/fm-afk-contract.sh AWAY OR
QUIET) is a present captain. On each actionable close:
  - attended (no away record): the close reaches main exactly as the arm printed
    it, as without the host, unless the supervision session may take it: the
    home names a usable engine, its turns have every tool they need, this
    primary has a verified dialog mirror (bin/fm-host-mirror.sh verified;
    fm_supervision_host_attended_ready owns the list), the main session's
    lock holder can be identified, the session is not cooling down after
    engine errors, and the Pi branch's offer rule
    (bin/fm-branch-dispatch.mjs offer) says the branch may take this close,
    so main-only classes (check triggers, decision-owned triggers, a scan
    that is unsafe or holds nothing for the branch) stay main's. That
    pass-through starts the successor watcher cycle and leaves it running
    before the close is printed, so supervision continues when the session
    drops the handoff. It confirms no handling handoff and republishes the
    recovery marker as downtime, even over handling an earlier engine turn
    left, so the re-arm owner delivers the close to main; a failed publish
    prints a watcher FAILED line instead of the close. The host records that successor's arm before relinquishing it
    (detach_successor owns the persistence check and failure path). The
    session's next park without --restart requests a take-over of its cycle
    rather than an ordinary attach; bin/fm-watch-arm.sh's --take-over header owns the
    conditions under which that restores a single owner and the fallback;
  - away (an away record exists): every close goes to the engine.
Every turn that starts attended meets that rule again at its start, so a
close accepted away whose turn starts attended (the captain returned in
between) or an attended close whose task turned main-only while the
successor started reaches main exactly as the arm printed it, and that
successor cycle stays running.
A close the engine takes is handled in one order: it starts and verifies the
successor watcher cycle and confirms the handling handoff (the order
docs/watcher-continuity.md owns), computes the rows the branch may claim in
the turn's posture with the dispatch owner, publishes that grant
(bin/fm-wake-grant.sh), runs one bounded headless engine turn
(bin/fm-supervision-engine-lib.sh) with the generated branch prompt
(bin/fm-branch-prompt.sh), the dialog-mirror feed (bin/fm-host-mirror.sh)
at the head of an attended wake and the away tail instead when away,
releases the branch's leases and grant, and counts the wake handled only
when that turn exited cleanly, recorded a durable report
(bin/fm-branch-report.sh), and left none of its granted rows in the wake
queue. Before parking after a handled turn, it settles the successor handoff;
docs/watcher-continuity.md "Durable queue and turn-end backstop" owns how
that transition makes rows still queued for main recoverable. A handled wake
with only routine outcomes never wakes main, and neither does any handled
wake while away: captain outcomes wait in
the outcome store for the return drain's BRANCH OUTCOMES section. A handled
attended wake that recorded a captain outcome exits with one "supervision-host: branch-outcome:"
line naming its store rows, without the close it handled; main drains, where
the BRANCH OUTCOMES section (bin/fm-wake-drain.sh) presents every
unprocessed captain outcome until main acknowledges it. Otherwise the host
parks on the successor.
Every other outcome exits with the close's own reason line plus one
"supervision-host:" line saying why main has this wake, after stopping the
successor cycle so main's next turn end starts from the same state as
without the host. Whenever the captain returned during an away engine turn
that recorded visible outcomes, handled or not, the return brief was rendered
before they existed, so the host exits with the close, one "supervision-host:"
line naming them, and one line per visible outcome, for main to relay. The
default host injects nothing and has no delivery path of its own; the owner's
existing wake path delivers it, unless ACKNOWLEDGED HAND-BACK below adds
retries through its detached owner. The default fallback is
always to exit with the close's own reason line. That handoff is only a
prompt: each non-silent outcome recorded after the return is already a
durable queued wake
(bin/fm-branch-report.sh), so it still reaches main when the host dies at the
turn's end or its owner drops the handoff, as a superseded Cursor park does.

ACKNOWLEDGED HAND-BACK (Claude only, config/supervision-handoff-ack).
With that opt-in, every main hand-back first queues a token-bound check and
verifies a detached bin/fm-supervision-handoff.sh owner. That owner keeps
replacing closed arms until main acknowledges the check, and leaves its
verified arm for the next park to take over. Its header owns retry delivery
and limits. Without the flag all hand-back paths below are unchanged.

THE LATCH. An opted-in host persists engine health across short-lived
parks; docs/supervision-host.md "The broken-session latch" owns the policy.

THE PARK BOUNDARY. Claude drops the exit 2 of a Stop hook it terminated at
the hook's configured timeout (docs/verification/supervision.md), Cursor's
stop hook carries the same tracked 28800-second registration, and a host
that handles its own wakes is not shortened by them, so the host ends its
own park before that timeout: after FM_SUPERVISION_HOST_PARK_SECONDS (default
27000, under the tracked 28800-second registration) it stops this home's
watcher and exits with one "supervision-host: cycle boundary" line, which the
owner delivers as an ordinary wake; main drains, acknowledges, and ends its
turn, and that turn end starts the next park. The verified Codex asynchronous owner sets
FM_CODEX_MODEL_FREE_PARK=1: its boundary resets the quiet timer while keeping
the watcher and recovery generation alive, with no output to the model.
All other owners keep the bounded lifecycle. The boundary is checked on
every loop pass, however many closes are already waiting, and an away close
whose engine turn could still be running at the turn limit (the turn bound
plus the engine grace), judged when the close arrives and again just before
the turn starts, is not handled: the host exits through the same boundary
with that close printed ahead of the line. The turn limit is the boundary
itself unless the owner sets FM_SUPERVISION_HOST_PARK_LIMIT later: Codex's
checkpoint, whose bound is the park itself rather than a harness timeout,
lets a turn that starts before the boundary finish after it.

OWNERSHIP. Before activation, every successor cycle, and every engine turn
the host proves this session still holds the fleet lock
(bin/fm-session-lock-lib.sh) and, when launched by the auto-arm, that the
auto-arm generation it serves (FM_SUPERVISION_HOST_AUTOARM_GEN owned by
FM_SUPERVISION_HOST_OWNER_PID) is still current; otherwise it stands down
with a "supervision-host:" line and leaves the decision to its owner; a
host that stands down before activation leaves the owner's host record,
processes, arms, and leases alone. The engine runs with
FM_SUPERVISION_ACTOR=branch, the session-lock holder as FM_LEASE_HOLDER_PID,
the primary's harness pin, and this turn's report id, so every guarded
script applies the same partition, leases, and away relocation it applies to
the Pi branch. At activation the host stops anything a crashed predecessor
left running (recorded with identities, never by name), including the
engine descendants its turn recorded, removes that turn's files, and
releases the branch actor's leases; it releases them again after every
engine turn. It also reads the record of a successor a pass-through left for
main: while that arm still runs under its recorded identity, the first cycle
without --restart requests a take-over rather than an ordinary attach.
Activation removes the
record only once that identity is no longer alive, so a later host retries a
take-over that left it running.

STATE (all under state/, owned here): .supervision-host (this host's pid and
the processes it runs), .supervision-host-engine (the engine conversation:
engine, model, session id, main-session key, turn count, running cost),
.supervision-host-turn and .supervision-host-receipts (the current turn's
report scope and the reports it recorded), .supervision-host-prompt and
.supervision-host-wake (the prompt and wake text of the current turn),
.supervision-host-mirror (the dialog-mirror feed while an attended wake is
rendered), .supervision-host-left (the pid and identity of the successor arm a
pass-through or the acknowledged hand-back owner left running for main, until
that arm is gone),
.supervision-host-health (the latch: errors, cooldown, and probe time, keyed
to the main session, engine, and model), and .supervision-host.log (a bounded
ledger of where every close went, with each engine turn's usage and
outcome).

Tunables (environment): FM_SUPERVISION_HOST_PARK_SECONDS (27000; a positive
integer below the 28800-second registration, any other value is the default),
FM_SUPERVISION_HOST_PARK_LIMIT (the park boundary; a later value below the
registration lets turns run past the boundary up to it, any other value is
the boundary), FM_SUPERVISION_HOST_TURN_TIMEOUT (1200), FM_SUPERVISION_HOST_ROTATE_TURNS (20:
a new engine conversation after this many turns; every main session start
also opens a new one), FM_SUPERVISION_HOST_READY_TIMEOUT (25: how long a
successor cycle may take to verify), FM_SUPERVISION_HOST_POLL (1).
Park duration and every wait's deadline use the monotonic clock, while
durable timestamps still use epoch time. The host sees an arm close as soon
as the arm exits, between the POLL-cadence maintenance and boundary checks.
A stop signal (TERM, HUP, INT) ends the host at once, through the cleanup,
with 128 plus its number; a command the host runs in the foreground finishes
first (firstmate.signals).
FM_TEST_SUPERVISION_HOST_CLOCK names a file holding the park's elapsed
seconds, which the park and turn boundary checks read in place of that clock
only when FM_TEST_SEAM=1; tests/lib.sh arms the marker for isolated suites.
"""

import os
import signal
import sys
import traceback

from ..signals import Exit, Stop

USAGE = "usage: fm-supervision-host.sh park [--restart]"


def main(argv):
    """`fm host park [--restart]`, the command bin/fm-supervision-host.sh runs."""
    if argv[:1] in (["-h"], ["--help"]):
        sys.stdout.write(__doc__)
        return 0
    if argv not in (["park"], ["park", "--restart"]):
        sys.stderr.write(USAGE + "\n")
        return 2
    # A bash script dies of an unhandled SIGINT or a closed output pipe.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    script_dir = os.environ.pop("FM_HOST_SCRIPT_DIR", "") or _tracked_bin()

    from .park import Host

    host = Host(os.environ, script_dir, restart=len(argv) == 2)
    try:
        host.park()
        status = 0
    except (Exit, Stop) as stop:
        status = stop.status
    except Exception:
        traceback.print_exc()
        status = 1
    if host.cleanup_armed:
        status = host.cleanup(status)
    return status


def _tracked_bin():
    return os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "bin"))
