"""The host's record formats, read and written in a temporary directory."""

import os
import tempfile
import unittest
from dataclasses import replace

from support import Home, write

from firstmate import files
from firstmate.host import conversation, latch, receipts, records
from firstmate.host.libs import Engine


class RecordFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "record")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, content):
        with open(self.path, "w") as handle:
            handle.write(content)


class ProcessRecordTest(RecordFileTest):
    def test_rows_round_trip_and_forget_drops_only_their_pid(self):
        records.append(self.path, "host", 10, "id-h")
        records.append(self.path, "arm", 11, "id-a")
        records.append(self.path, "turn", 12, "id\tt")
        records.forget(self.path, 11)
        self.assertEqual(
            records.rows(self.path),
            [records.ProcessRow("host", 10, "id-h"), records.ProcessRow("turn", 12, "id\tt")],
        )
        self.assertEqual(records.host_pid(self.path), 10)

    def test_refresh_rewrites_only_a_changed_identity(self):
        records.append(self.path, "arm", 11, "before-exec")
        records.append(self.path, "arm", 12, "other")
        records.refresh(self.path, 11, "after-exec")
        self.assertEqual(
            records.rows(self.path),
            [records.ProcessRow("arm", 11, "after-exec"), records.ProcessRow("arm", 12, "other")],
        )
        records.refresh(self.path, 12, "")
        self.assertEqual(records.rows(self.path)[1], records.ProcessRow("arm", 12, "other"))

    def test_an_unterminated_last_row_names_no_process(self):
        self.write("arm\t1\tid\narm\t2\tpart")
        self.assertEqual(records.rows(self.path), [records.ProcessRow("arm", 1, "id")])

    def test_a_pid_that_is_not_plain_digits_names_no_process(self):
        self.write("host\t-1\tid\narm\t 2\tid\narm\t0\tid\n")
        self.assertEqual([row.pid for row in records.rows(self.path)], [None, None, None])
        self.assertIsNone(records.host_pid(self.path))

    def test_the_ledger_stays_bounded_and_keeps_its_newest_rows_in_order(self):
        written = [f"5\trow {n}" for n in range(5000)]
        for row in written:
            records.log(self.path, row.split("\t", 1)[1], 5)
        kept = files.lines(files.read_text(self.path) or "")
        self.assertLess(len(kept), len(written))
        self.assertEqual(kept, written[len(written) - len(kept) :])


class PidIdentityRecordTest(RecordFileTest):
    def test_the_first_line_names_the_pid_and_identity(self):
        self.write("42\tbash 1 2\nignored\n")
        self.assertEqual(records.read_pid_identity(self.path), records.PidIdentity(42, "bash 1 2"))

    def test_an_unterminated_line_counts_only_where_a_bash_read_would_accept_it(self):
        self.write("42\tid")
        self.assertEqual(records.read_pid_identity(self.path), records.PidIdentity(42, "id"))
        self.assertIsNone(records.read_pid_identity(self.path, terminated=True))

    def test_a_missing_record_names_nothing(self):
        self.assertIsNone(records.read_pid_identity(self.path))

    def test_a_written_record_reads_back(self):
        self.assertTrue(records.write_pid_identity(self.path, 7, "id"))
        self.assertEqual(files.read_text(self.path), "7\tid\n")

    def test_a_record_that_cannot_be_a_regular_file_is_not_written(self):
        os.mkdir(self.path)
        self.assertFalse(records.write_pid_identity(self.path, 7, "id"))


class ReceiptsTest(RecordFileTest):
    def test_a_turns_receipts_are_its_own_rows(self):
        self.write("t1\t4\troutine\tdemo\nt2\t5\tcaptain\tbeta\nt1\t6\tcaptain\tdemo")
        self.assertEqual(
            receipts.of_turn(self.path, "t1"),
            [
                receipts.Receipt("t1", "4", "routine", "demo"),
                receipts.Receipt("t1", "6", "captain", "demo"),
            ],
        )

    def test_an_unreadable_file_is_not_an_empty_turn(self):
        self.assertIsNone(receipts.of_turn(self.path, "t1"))
        receipts.clear(self.path)
        self.assertEqual(receipts.of_turn(self.path, "t1"), [])


class ConversationTest(RecordFileTest):
    ENGINE = Engine("claude", "sonnet")
    RECORD = conversation.EngineRecord(
        engine="claude", model="sonnet", session="s1", key="k1", turns=3, cost="0.25", ended=900
    )

    def resumable(self, record=RECORD, *, key="k1", engine=ENGINE, prompt_ready=True):
        return conversation.resumable(
            record, key=key, engine=engine, rotate_turns=20, prompt_ready=prompt_ready
        )

    def test_the_recorded_conversation_resumes_for_its_session_engine_and_model(self):
        self.assertTrue(self.resumable())
        self.assertEqual(
            conversation.resumed(self.RECORD),
            conversation.Conversation("s1", conversation.Mode.RESUME, 3, "0.25", "k1"),
        )

    def test_any_other_main_session_engine_or_model_starts_a_new_one(self):
        self.assertFalse(self.resumable(key="k2"))
        self.assertFalse(self.resumable(key=None))
        self.assertFalse(self.resumable(engine=Engine("codex", "sonnet")))
        self.assertFalse(self.resumable(engine=Engine("claude", "opus")))

    def test_a_conversation_rotates_after_its_turns_or_without_a_prompt(self):
        self.assertFalse(self.resumable(replace(self.RECORD, turns=20)))
        self.assertTrue(self.resumable(replace(self.RECORD, turns=19)))
        self.assertFalse(self.resumable(prompt_ready=False))
        self.assertFalse(self.resumable(replace(self.RECORD, session="")))

    def test_the_record_round_trips_and_compaction_marks_its_episode(self):
        self.assertTrue(conversation.write(self.path, self.RECORD))
        conversation.mark_compacted(self.path, 900)
        self.assertEqual(conversation.read(self.path), replace(self.RECORD, compacted=900))

    def test_a_record_without_a_cost_counts_from_zero(self):
        self.write("engine=claude\nconversation_cost=\nturns=x\n")
        record = conversation.read(self.path)
        self.assertEqual((record.cost, record.turns), ("0", 0))


class AutoArmLedgerTest(unittest.TestCase):
    """The auto-arm generation a host serves, as bin/fm-wake-lib.sh reads its ledger."""

    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.libs = self.home.host().ctx.libs

    def ledger(self, owner):
        write(self.home.at(".claude-autoarm-epoch"), f"epoch=7 owner_pid={owner} outcome=arming\n")

    def test_the_generation_is_current_while_its_owner_holds_it_arming(self):
        self.ledger("4242")
        self.assertTrue(self.libs.autoarm_current("7", 4242))
        self.assertFalse(self.libs.autoarm_current("7", 4243))
        self.assertFalse(self.libs.autoarm_current("8", 4242))

    def test_a_host_without_an_owner_pid_never_matches_a_malformed_owner(self):
        self.ledger("invalid")
        self.assertFalse(self.libs.autoarm_current("7", None))


class LatchTest(unittest.TestCase):
    def after(self, state, *, error, reports=0, now=1000):
        return latch.after_turn(
            state, engine_error=error, reports=reports, now=now, first_cooldown=300
        )

    def test_one_engine_error_does_not_trip(self):
        self.assertEqual(
            self.after(latch.Latch(), error=True), latch.LatchChange(latch.Latch(errors=1))
        )

    def test_the_second_consecutive_error_trips_with_one_note(self):
        change = self.after(latch.Latch(errors=1), error=True)
        self.assertEqual(change.latch, latch.Latch(errors=2, cooldown=300, retry=1300))
        self.assertTrue(
            change.note.startswith(
                "supervision-host: the supervision session is paused after repeated engine "
                "errors; every wake reaches you for the next 5 minutes"
            )
        )
        self.assertEqual(len(change.logged), 1)

    def test_a_failed_probe_doubles_the_cooldown_up_to_its_cap_without_a_note(self):
        change = self.after(latch.Latch(errors=2, cooldown=300, retry=900), error=True)
        self.assertEqual((change.latch.cooldown, change.latch.retry, change.note), (600, 1600, ""))
        change = self.after(latch.Latch(errors=5, cooldown=2400, retry=900), error=True)
        self.assertEqual(change.latch.cooldown, 3600)

    def test_a_report_without_an_engine_error_clears_it(self):
        recovered = self.after(
            latch.Latch(errors=2, cooldown=300, retry=9), error=False, reports=1
        )
        self.assertEqual(
            (recovered.latch, recovered.note, len(recovered.logged)), (latch.Latch(), "", 1)
        )
        self.assertEqual(
            self.after(latch.Latch(errors=1), error=False, reports=1),
            latch.LatchChange(latch.Latch()),
        )

    def test_a_complete_turn_without_a_report_neither_counts_nor_clears(self):
        self.assertEqual(
            self.after(latch.Latch(errors=1), error=False).latch, latch.Latch(errors=1)
        )
        self.assertEqual(
            self.after(latch.Latch(errors=2, cooldown=300, retry=1000), error=False).latch,
            latch.Latch(errors=2, cooldown=300, retry=1300),
        )
        self.assertEqual(
            self.after(latch.Latch(errors=2, cooldown=300, retry=1001), error=False).latch,
            latch.Latch(errors=2, cooldown=300, retry=1001),
        )

    def test_the_latch_holds_main_to_every_wake_until_its_probe_time(self):
        self.assertTrue(latch.cooling(1300, 1299))
        self.assertFalse(latch.cooling(1300, 1300))
        self.assertFalse(latch.cooling(None, 0))

    def test_the_latch_belongs_to_its_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "health")
            latch.save(path, "k1", latch.Latch(errors=2, cooldown=300, retry=50))
            self.assertEqual(latch.load(path, "k1"), latch.Latch(errors=2, cooldown=300, retry=50))
            self.assertEqual(latch.load(path, "k2"), latch.Latch())
            self.assertEqual(latch.load(path, None), latch.Latch())
            latch.save(path, None, latch.Latch(errors=9))
            self.assertEqual(latch.load(path, "k1"), latch.Latch(errors=2, cooldown=300, retry=50))


if __name__ == "__main__":
    unittest.main()
