"""Point selection is a decision on backend answers, without network or models."""

import unittest

from firstmate.brief_points import memory_points, points_from


class BriefPointTests(unittest.TestCase):
    def test_absence_of_recall_is_not_an_expectation(self):
        points, dropped = points_from(
            "I don't have the review's findings in the available record.", 10
        )
        self.assertEqual(points, {})
        self.assertEqual(dropped, 0)


class PointSelectionTests(unittest.TestCase):
    def test_bulleted_nonanswers_are_removed_before_the_cap(self):
        points, dropped = points_from(
            "- No record of that topic.\n- Keep the helper generic.\n- Use public interfaces.", 1
        )
        self.assertEqual(points, {"p1": "Keep the helper generic."})
        self.assertEqual(dropped, 1)

    def test_mixed_answers_keep_substantive_evidence(self):
        for answer in (
            "I don't have the review. He prefers public interface tests.",
            "I don't have the review, but keep the helper generic.",
            "Do not treat absence of a record as approval.",
            "I don't have to approve routine refactors.",
            "No record should contain credentials.",
            "I cannot find fault with this plan.",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(points_from(answer, 5)[0], {"p1": answer})

    def test_list_items_round_robin_duplicates_and_errors(self):
        results = [
            {"answer": "- Plain words.\n- Add tests.", "error": None},
            {
                "answer": [{"text": "raw metadata"}],
                "items": ["Generic helper.", "Plain words.", None],
                "error": None,
            },
            {"answer": "Ignored error.", "error": "HTTP 401"},
        ]
        self.assertEqual(
            memory_points(results, 3),
            ({"p1": "Plain words.", "p2": "Generic helper.", "p3": "Add tests."}, 0),
        )

    def test_list_nonanswer_is_removed_too(self):
        self.assertEqual(
            memory_points([{"answer": ["I cannot find that topic in memory."], "error": None}], 5),
            ({}, 0),
        )


if __name__ == "__main__":
    unittest.main()
