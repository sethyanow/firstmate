"""The park's boundary, on the clock the host is given."""

import unittest

from support import Home


class SetClock:
    """A park clock the test sets."""

    def __init__(self) -> None:
        self.seconds = 0

    def elapsed(self) -> int:
        return self.seconds

    def restart(self) -> None:
        self.seconds = 0


class ParkBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.clock = SetClock()

    def park(self, **env):
        host = self.home.host(clock=self.clock, FM_SUPERVISION_HOST_PARK_SECONDS="100", **env)
        return host.park

    def test_the_park_ends_at_its_boundary_on_the_clock_it_was_given(self):
        park = self.park()
        self.clock.seconds = 99
        self.assertFalse(park.boundary_reached())
        self.clock.seconds = 100
        self.assertTrue(park.boundary_reached())

    def test_a_model_free_codex_park_starts_its_clock_over_instead_of_ending(self):
        park = self.park(FM_SUPERVISION_HOST_PRIMARY="codex", FM_CODEX_MODEL_FREE_PARK="1")
        self.clock.seconds = 100
        self.assertFalse(park.boundary_reached())
        self.assertEqual(self.clock.elapsed(), 0)


if __name__ == "__main__":
    unittest.main()
