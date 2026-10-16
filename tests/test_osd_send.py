"""Sending keys: the 150 ms spacing before the first key, from the key time in the state file."""

from datetime import UTC, datetime, timedelta

from support import SafeTestCase

from xeneon_edge_ctl import osd
from xeneon_edge_ctl.errors import TransportError
from xeneon_edge_ctl.osd import Key, Level, OsdState

NOW = datetime(2026, 10, 16, 9, 0, 0, tzinfo=UTC)
SPACING = osd.KEY_SPACING_S
PICTURE = 0


class FakePresser:
    """Records the selectors and, with them, what the fake clock said when each key went out."""

    def __init__(self, world: "FakeWorld") -> None:
        self.world = world
        self.sent: list[tuple[int, datetime]] = []

    def press_key(self, selector: int) -> None:
        self.sent.append((selector, self.world.now))


class FakeWorld:
    """The injected clock and sleep: a pause moves the clock, so the wait is visible in the key time."""

    def __init__(self) -> None:
        self.now = NOW
        self.sleeps: list[float] = []

    def clock(self) -> datetime:
        return self.now

    def sleep(self, pause: float) -> None:
        self.sleeps.append(pause)
        self.now += timedelta(seconds=pause)


class SpacingTestCase(SafeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.world = FakeWorld()
        self.presser = FakePresser(self.world)
        self.path = self.tmp / "xeneon-edge-osd.json"

    def open_menu(self, key_age_s: float | None) -> OsdState:
        """An open menu whose last key was ``key_age_s`` before the fake clock's now (None: never recorded)."""
        stamp = None if key_age_s is None else NOW - timedelta(seconds=key_age_s)
        return OsdState(Level.PAGES, PICTURE, last_key_time=stamp)

    def send_plan(self, state: OsdState, repeat: int = 1) -> OsdState:
        plan = osd.plan_keys(state, self.world.now, Key.RIGHT, repeat)
        return osd.send_plan(self.presser, self.path, state, plan, self.world.clock, self.world.sleep)

    def send_sequence(self, state: OsdState, keys: tuple[Key, ...]) -> OsdState:
        return osd.send_sequence(
            self.presser, self.path, state, keys, lambda now: osd.closed_state(now), self.world.clock, self.world.sleep
        )


class SendPlanSpacingTests(SpacingTestCase):
    def test_a_recent_last_key_makes_the_first_key_wait_for_the_rest_of_the_spacing(self) -> None:
        self.send_plan(self.open_menu(0.05))
        self.assertEqual(len(self.world.sleeps), 1)
        self.assertAlmostEqual(self.world.sleeps[0], SPACING - 0.05)
        sent_at = self.presser.sent[0][1]
        self.assertAlmostEqual((sent_at - (NOW - timedelta(seconds=0.05))).total_seconds(), SPACING)

    def test_a_last_key_just_now_waits_the_whole_spacing(self) -> None:
        self.send_plan(self.open_menu(0.0))
        self.assertEqual(self.world.sleeps, [SPACING])

    def test_no_wait_when_the_last_key_is_exactly_the_spacing_old_or_older(self) -> None:
        for age in (SPACING, SPACING + 0.001, 1.0, 7.0):
            with self.subTest(age=age):
                self.world.sleeps.clear()
                self.send_plan(self.open_menu(age))
                self.assertEqual(self.world.sleeps, [])

    def test_no_wait_without_a_recorded_key_time(self) -> None:
        state = OsdState(Level.CLOSED)
        plan = osd.plan_keys(state, self.world.now, Key.BACK)
        osd.send_plan(self.presser, self.path, state, plan, self.world.clock, self.world.sleep)
        self.assertEqual(self.world.sleeps, [])
        self.assertEqual(len(self.presser.sent), 1)

    def test_a_clock_that_went_backwards_never_waits_longer_than_the_spacing(self) -> None:
        # The key time lies in the future; planning refuses an open menu then, so use a closed one.
        state = OsdState(Level.CLOSED, last_key_time=NOW + timedelta(seconds=3600))
        plan = osd.plan_keys(state, self.world.now, Key.BACK)
        osd.send_plan(self.presser, self.path, state, plan, self.world.clock, self.world.sleep)
        self.assertEqual(self.world.sleeps, [SPACING])

    def test_the_spacing_inside_the_run_stays_and_only_the_first_key_waits_extra(self) -> None:
        self.send_plan(self.open_menu(0.05), repeat=3)
        self.assertEqual(len(self.world.sleeps), 3)
        self.assertAlmostEqual(self.world.sleeps[0], SPACING - 0.05)
        self.assertEqual(self.world.sleeps[1:], [SPACING, SPACING])

    def test_the_run_without_a_recent_key_keeps_its_two_inner_pauses_only(self) -> None:
        self.send_plan(self.open_menu(5.0), repeat=3)
        self.assertEqual(self.world.sleeps, [SPACING, SPACING])

    def test_the_wait_comes_before_the_first_key_and_the_key_is_stamped_after_it(self) -> None:
        final = self.send_plan(self.open_menu(0.05))
        self.assertEqual(final.last_key_time, self.presser.sent[0][1])
        self.assertGreater(final.last_key_time, NOW)

    def test_a_state_file_that_cannot_be_written_stops_the_run_without_a_wait_or_a_key(self) -> None:
        self.path = self.tmp / "missing-directory" / "xeneon-edge-osd.json"
        with self.assertRaises(TransportError):
            self.send_plan(self.open_menu(0.05))
        self.assertEqual((self.world.sleeps, self.presser.sent), ([], []))


class SendSequenceSpacingTests(SpacingTestCase):
    def test_a_recent_last_key_makes_open_or_close_wait_before_its_first_key(self) -> None:
        for name, keys in (("open", osd.OPEN_KEYS), ("close", osd.CLOSE_KEYS)):
            with self.subTest(command=name):
                self.world.now = NOW
                self.world.sleeps.clear()
                self.presser.sent.clear()
                self.send_sequence(self.open_menu(0.05), keys)
                self.assertEqual(len(self.world.sleeps), len(keys))
                self.assertAlmostEqual(self.world.sleeps[0], SPACING - 0.05)
                self.assertEqual(self.world.sleeps[1:], [SPACING] * (len(keys) - 1))
                self.assertEqual(len(self.presser.sent), len(keys))

    def test_no_wait_when_the_last_key_is_old_or_missing(self) -> None:
        for name, keys in (("open", osd.OPEN_KEYS), ("close", osd.CLOSE_KEYS)):
            for age in (None, SPACING, 3.0):
                with self.subTest(command=name, age=age):
                    self.world.sleeps.clear()
                    self.send_sequence(self.open_menu(age), keys)
                    self.assertEqual(self.world.sleeps, [SPACING] * (len(keys) - 1))

    def test_a_clock_that_went_backwards_never_waits_longer_than_the_spacing(self) -> None:
        state = OsdState(Level.PAGES, PICTURE, last_key_time=NOW + timedelta(seconds=3600))
        self.send_sequence(state, osd.CLOSE_KEYS)
        self.assertEqual(self.world.sleeps, [SPACING] * len(osd.CLOSE_KEYS))

    def test_an_unreadable_state_with_no_key_time_does_not_wait(self) -> None:
        self.send_sequence(OsdState(unknown_reason="there is no state file"), osd.OPEN_KEYS)
        self.assertEqual(self.world.sleeps, [SPACING] * (len(osd.OPEN_KEYS) - 1))
