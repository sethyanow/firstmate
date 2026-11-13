"""The host's decisions, called directly: no processes, no clock."""

import unittest

from firstmate.host import boundary, decide, scope, text, warm
from firstmate.host.decide import Handback, Returned, ReturnedKind, TurnEnd
from firstmate.host.settings import numeric_or, paths_from, settings_from


class TunablesTest(unittest.TestCase):
    def test_only_a_positive_integer_without_a_leading_zero_replaces_the_default(self):
        for value in ("", None, "0", "007", "-5", "1.5", "12a", " 12", "٣"):
            self.assertEqual(numeric_or(value, 9), 9, value)
        self.assertEqual(numeric_or("12", 9), 12)

    def test_documented_defaults(self):
        settings = settings_from({})
        self.assertEqual(
            (
                settings.park_seconds,
                settings.park_limit,
                settings.turn_timeout,
                settings.rotate_turns,
                settings.ready_timeout,
                settings.poll,
            ),
            (27000, 27000, 1200, 20, 25, 1),
        )

    def test_a_park_at_or_beyond_the_hook_registration_falls_back_to_the_default(self):
        self.assertEqual(
            settings_from({"FM_SUPERVISION_HOST_PARK_SECONDS": "28800"}).park_seconds, 27000
        )
        self.assertEqual(
            settings_from({"FM_SUPERVISION_HOST_PARK_SECONDS": "28799"}).park_seconds, 28799
        )

    def test_the_turn_limit_is_the_boundary_unless_a_later_one_fits_the_registration(self):
        def limit(seconds, value):
            return settings_from(
                {
                    "FM_SUPERVISION_HOST_PARK_SECONDS": seconds,
                    "FM_SUPERVISION_HOST_PARK_LIMIT": value,
                }
            ).park_limit

        self.assertEqual(limit("180", "1500"), 1500)
        self.assertEqual(limit("180", "100"), 180)
        self.assertEqual(limit("180", "28800"), 180)

    def test_state_and_config_follow_their_overrides_then_the_home(self):
        paths = paths_from({}, "/repo/bin")
        self.assertEqual(
            (paths.root, paths.home, paths.state, paths.config),
            ("/repo", "/repo", "/repo/state", "/repo/config"),
        )
        paths = paths_from({"FM_HOME": "/home", "FM_STATE_OVERRIDE": "/s"}, "/repo/bin")
        self.assertEqual((paths.home, paths.state, paths.config), ("/home", "/s", "/home/config"))


class BoundaryTest(unittest.TestCase):
    SETTINGS = settings_from(
        {
            "FM_SUPERVISION_HOST_PARK_SECONDS": "100",
            "FM_SUPERVISION_HOST_TURN_TIMEOUT": "40",
            "FM_SUPERVISION_HOST_PARK_LIMIT": "150",
            "FM_SUPERVISION_ENGINE_GRACE": "1",
        }
    )

    def test_the_park_ends_at_its_boundary(self):
        self.assertIs(boundary.check(99, park_seconds=100, quiet=False), boundary.Boundary.INSIDE)
        self.assertIs(
            boundary.check(100, park_seconds=100, quiet=False), boundary.Boundary.REACHED
        )

    def test_a_model_free_codex_park_restarts_its_quiet_clock_instead(self):
        quiet = boundary.model_free("codex", {"FM_CODEX_MODEL_FREE_PARK": "1"})
        self.assertTrue(quiet)
        self.assertIs(
            boundary.check(100, park_seconds=100, quiet=quiet), boundary.Boundary.QUIET_RESTART
        )
        self.assertFalse(boundary.turn_crosses(10**6, self.SETTINGS, quiet=quiet))

    def test_only_codex_with_the_opt_in_parks_model_free(self):
        self.assertFalse(boundary.model_free("claude", {"FM_CODEX_MODEL_FREE_PARK": "1"}))
        self.assertFalse(boundary.model_free("codex", {}))

    def test_a_turn_that_could_outlive_the_turn_limit_is_not_started(self):
        self.assertFalse(boundary.turn_crosses(108, self.SETTINGS, quiet=False))
        self.assertTrue(boundary.turn_crosses(109, self.SETTINGS, quiet=False))


class WarmTickTest(unittest.TestCase):
    def test_the_first_compaction_check_is_due_at_once_then_once_per_tick(self):
        self.assertTrue(warm.due(0, None, 60))
        self.assertFalse(warm.due(59, 0, 60))
        self.assertTrue(warm.due(60, 0, 60))


class CloseTextTest(unittest.TestCase):
    def test_wake_lines_are_the_close_lines_main_or_the_engine_acts_on(self):
        close = text.Close(
            "watcher: started pid=4\nsignal: a.status\nnoise\nheartbeat\nheartbeats\n"
            "heartbeat: x\ncheck: y",
            4,
        )
        self.assertEqual(close.reason, "signal: a.status\nheartbeat\nheartbeat: x\ncheck: y")
        self.assertEqual(close.first_wake, "signal: a.status")
        self.assertEqual(text.Close("watcher: attached pid=4", 4).reason, "")

    def test_the_status_line_is_read_only_once_the_arm_wrote_it_in_full(self):
        self.assertEqual(text.status_line("watcher: started pid=1"), "")
        self.assertEqual(
            text.status_line("x\nwatcher: started pid=1 (beacon)\n"),
            "watcher: started pid=1 (beacon)",
        )
        self.assertEqual(text.status_line("watcher: stopped\n"), "")

    def test_a_printed_status_line_leaves_the_first_close_once(self):
        close = "watcher: started pid=1\nsignal: x\nwatcher: started pid=1"
        self.assertEqual(
            text.without_status(close, "watcher: started pid=1"),
            "signal: x\nwatcher: started pid=1",
        )

    def test_a_verified_successor_names_its_watcher_and_last_recovery_generation(self):
        line = "watcher: started pid=77 a recovery-generation=1 recovery-generation=g9"
        self.assertEqual(text.verified_watcher("x\n" + line), text.Verified(77, "g9"))
        self.assertEqual(
            text.verified_watcher("watcher: attached pid=8 (beacon fresh)"), text.Verified(8, "")
        )
        self.assertIsNone(text.verified_watcher("watcher: started (no pid)"))

    def test_an_exit_prints_the_close_then_each_non_empty_line(self):
        self.assertEqual(
            text.compose("signal: x", ["", "supervision-host: why", "a\nb"]),
            "signal: x\nsupervision-host: why\na\nb",
        )
        self.assertEqual(text.compose("", ["supervision-host: why"]), "supervision-host: why")
        self.assertEqual(text.compose("", ["", ""]), "")

    def test_the_rows_the_queue_still_holds_are_named(self):
        self.assertEqual(text.unacknowledged(("4", "5", "6"), "4\n6\n"), ("4", "6"))
        self.assertEqual(text.unacknowledged(("4", "5", "6"), ""), ())

    def test_every_granted_row_is_named_when_the_queue_cannot_be_read(self):
        self.assertEqual(text.unacknowledged(("4", "5", "6"), None), ("4", "5", "6"))


class OutcomeRowsTest(unittest.TestCase):
    ROWS = (
        '{"seq":2,"task":"demo","verdict":"routine","summary":"done\\r","silent":false}\n'
        '{"seq":3,"task":"demo","verdict":"routine","summary":"quiet","silent":true}\n'
        '{"seq":4,"task":"beta","verdict":"captain","summary":"merge?"}'
    )

    def test_silent_outcomes_are_left_out(self):
        rows = text.visible(text.outcome_rows(self.ROWS))
        self.assertEqual(text.outcome_seqs(rows), "2, 4")
        self.assertEqual(
            text.outcome_lines(rows),
            "supervision-host: outcome 2 for demo [routine]: done\n"
            "supervision-host: outcome 4 for beta [captain]: merge?",
        )

    def test_no_rows_name_no_outcome(self):
        self.assertEqual(text.outcome_rows(""), [])

    def test_a_malformed_row_is_a_lookup_failure_not_silence(self):
        for rows in ("not-json", '{"seq":1}\n7', '{"seq":1'):
            with self.assertRaises(ValueError, msg=rows):
                text.outcome_rows(rows)


class ScopeTest(unittest.TestCase):
    def test_a_scope_names_the_rows_the_branch_may_claim(self):
        claim = scope.parse(
            "eligible=1\nstatus=safe\ncorrupted=0\nrows=4 5\ntasks=demo\nunscoped=0\n"
        )
        self.assertEqual(
            (claim.eligible, claim.corrupted, claim.rows, claim.tasks, claim.claimable),
            (True, False, ("4", "5"), "demo", True),
        )

    def test_an_empty_scan_or_one_without_rows_claims_nothing(self):
        self.assertFalse(scope.parse("status=empty\nrows=4\n").claimable)
        self.assertFalse(scope.parse("status=safe\nrows=\n").claimable)

    def test_no_output_is_neither_eligible_nor_corrupted(self):
        empty = scope.parse("")
        self.assertEqual((empty.eligible, empty.corrupted, empty.claimable), (False, False, False))


class RouteTest(unittest.TestCase):
    def test_a_close_routes_by_its_wake_then_the_daemon_flag_then_the_posture(self):
        route = decide.route
        self.assertIs(route("", away_daemon=True, away=True), decide.Route.NO_WAKE)
        self.assertIs(route("signal: x", away_daemon=True, away=False), decide.Route.AWAY_DAEMON)
        self.assertIs(route("signal: x", away_daemon=False, away=True), decide.Route.AWAY)
        self.assertIs(route("signal: x", away_daemon=False, away=False), decide.Route.ATTENDED)

    def test_an_attended_close_stays_with_main_for_its_first_reason(self):
        def refusal(*, ready=True, main_known=True, cooling=False):
            return decide.attended_refusal(
                ready=ready,
                unready="no verified dialog mirror",
                main_known=main_known,
                cooling=cooling,
            )

        self.assertEqual(
            refusal(ready=False, main_known=False, cooling=True),
            decide.Refusal("no verified dialog mirror"),
        )
        self.assertEqual(
            refusal(main_known=False, cooling=True),
            decide.Refusal("the main session could not be identified"),
        )
        self.assertEqual(
            refusal(cooling=True),
            decide.Refusal("the supervision session is cooling down after engine errors"),
        )
        self.assertIsNone(refusal())

    def test_the_branch_offer_decides_last_and_only_its_refusal_is_main_only(self):
        self.assertEqual(
            decide.offer_refusal(None), decide.Refusal("branch eligibility could not be computed")
        )
        self.assertEqual(
            decide.offer_refusal(scope.parse("eligible=0\n")),
            decide.Refusal("main-only", main_only=True),
        )
        self.assertIsNone(decide.offer_refusal(scope.parse("eligible=1\n")))

    def test_an_away_close_reaches_main_for_its_first_missing_need(self):
        def refusal(*, runs_host=True, engine="claude", node=True, cooling=False):
            return decide.away_refusal(
                runs_host=runs_host, engine=engine, node_present=node, cooling=cooling
            )

        self.assertIs(
            refusal(runs_host=False, engine="", node=False, cooling=True),
            decide.AwayRefusal.HOST_OFF,
        )
        self.assertIs(refusal(engine="", node=False, cooling=True), decide.AwayRefusal.NO_ENGINE)
        self.assertIs(refusal(node=False, cooling=True), decide.AwayRefusal.NO_NODE)
        self.assertIs(refusal(cooling=True), decide.AwayRefusal.COOLING)
        self.assertIsNone(refusal())

    def test_each_away_refusal_tells_main_why(self):
        def line(refusal):
            return decide.away_refusal_line(refusal, problem="no engine named", until="14:05")

        self.assertEqual(
            line(decide.AwayRefusal.HOST_OFF), "the home no longer runs the supervision host"
        )
        self.assertEqual(
            line(decide.AwayRefusal.NO_ENGINE),
            "no supervision engine runs here: no engine named; this wake is yours",
        )
        self.assertEqual(
            line(decide.AwayRefusal.NO_NODE),
            "node is required to compute branch eligibility; this wake is yours",
        )
        self.assertEqual(
            line(decide.AwayRefusal.COOLING),
            "the away session is paused after repeated engine errors until 14:05; "
            "this wake is yours",
        )


class TurnJudgmentTest(unittest.TestCase):
    def end(self, rc=0, *, timed_out=False, usage="error=0 cost=1", receipts=1, unacked=()):
        return TurnEnd(rc, timed_out, usage, receipts, unacked)

    def why(self, **end):
        return decide.turn_failure(self.end(**end), turn_timeout=1200, engine="claude")

    def test_a_turn_handled_its_wake_only_when_clean_reported_and_acknowledged(self):
        self.assertTrue(self.end().handled)
        self.assertFalse(self.end(rc=1).handled)
        self.assertFalse(self.end(usage="error=1").handled)
        self.assertFalse(self.end(receipts=0).handled)
        self.assertFalse(self.end(unacked=("4",)).handled)

    def test_a_failed_turn_names_its_first_failure(self):
        self.assertEqual(self.why(rc=124, timed_out=True), "the engine turn hit its 1200s bound")
        self.assertEqual(self.why(rc=127), "the claude engine could not run")
        self.assertEqual(self.why(rc=3), "the engine turn failed (exit 3)")
        self.assertEqual(
            self.why(usage="error=1 cost=1"),
            "the engine turn ended with an error or an incomplete result",
        )
        self.assertEqual(
            self.why(usage=""), "the engine turn ended with an error or an incomplete result"
        )
        self.assertEqual(self.why(receipts=0), "the engine turn recorded no outcome for its wake")
        self.assertEqual(
            self.why(unacked=("4", "5")),
            "the engine turn left its granted wake rows 4 5 unacknowledged",
        )


class AfterTurnTest(unittest.TestCase):
    NONE = Returned(ReturnedKind.NONE)
    OUTCOMES = Returned(
        ReturnedKind.OUTCOMES, "7,8", "7", "supervision-host: outcome 7 for demo [routine]: x"
    )
    LOOKUP_FAILED = Returned(ReturnedKind.LOOKUP_FAILED, "7,8")

    def after(self, failure=None, *, away=True, returned=NONE, captain_seqs="", note=""):
        return decide.after_turn(
            failure=failure,
            away=away,
            returned=returned,
            captain_seqs=captain_seqs,
            health_note=note,
        )

    def test_a_handled_turn_with_nothing_for_main_parks_on(self):
        self.assertIsNone(self.after())

    def test_a_failed_turn_hands_its_wake_to_main_in_its_posture(self):
        self.assertEqual(
            self.after(failure="the engine turn failed (exit 3)", note="paused"),
            Handback(
                "the away session could not take this wake: the engine turn failed (exit 3); "
                "this wake is yours",
                "paused",
            ),
        )
        self.assertEqual(
            self.after(failure="x", away=False).why,
            "the supervision session could not take this wake: x; this wake is yours",
        )

    def test_a_captain_who_returned_during_a_handled_turn_gets_its_visible_outcomes(self):
        self.assertEqual(
            self.after(returned=self.OUTCOMES, captain_seqs="9"),
            Handback(
                "the captain returned while the away session was handling this wake, which it "
                "finished after the return brief was rendered; relay its visible outcomes "
                "(store rows 7, listed next and in bin/fm-branch-outcome.sh list) to the captain",
                "supervision-host: outcome 7 for demo [routine]: x",
            ),
        )

    def test_a_captain_who_returned_during_a_failed_turn_gets_the_wake_and_its_outcomes(self):
        self.assertEqual(
            self.after(failure="boom", returned=self.OUTCOMES, note="paused"),
            Handback(
                "the away session could not take this wake: boom; this wake is yours, and the "
                "captain returned during its turn, so relay the visible outcomes it recorded "
                "(store rows 7, listed next and in bin/fm-branch-outcome.sh list) to the captain",
                "supervision-host: outcome 7 for demo [routine]: x\npaused",
            ),
        )

    def test_an_outcome_lookup_that_failed_is_not_silence(self):
        warning = (
            "supervision-host: outcome lookup failed for turn receipt rows 7,8; visible outcomes "
            "may require manual review"
        )
        self.assertEqual(
            self.after(returned=self.LOOKUP_FAILED),
            Handback(
                "the captain returned while the away session was handling this wake, but the "
                "recorded outcomes could not be verified; main must review them",
                warning,
            ),
        )
        self.assertEqual(
            self.after(failure="boom", returned=Returned(ReturnedKind.LOOKUP_FAILED)).further,
            "supervision-host: outcome lookup failed for turn receipt rows unknown; visible "
            "outcomes may require manual review",
        )

    def test_captain_outcomes_of_a_handled_attended_turn_go_to_main_timed_without_the_close(self):
        handback = self.after(away=False, captain_seqs="4, 6")
        self.assertEqual(
            handback,
            Handback(
                "branch-outcome: the supervision session handled this wake and recorded captain "
                "outcomes for you (store rows 4, 6); run bin/fm-wake-drain.sh, act on its BRANCH "
                "OUTCOMES section, and acknowledge them as it prints",
                "",
                with_close=False,
                stamped=True,
            ),
        )


if __name__ == "__main__":
    unittest.main()
