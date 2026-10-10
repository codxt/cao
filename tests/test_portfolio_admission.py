"""Advisory global capacity gates never synthesize write authority."""

import unittest

from cao.portfolio_admission import (
    AdmissionError, ActiveTask, OccupancySnapshot, TaskProposal,
    plan_admission,
)


def snapshot(*items):
    return OccupancySnapshot(True, tuple(items), sum(x.mode == "WRITE" for x in items))


def proposal(name, mode="WRITE", repo="org/repo"):
    return TaskProposal(name, repo, mode)


class AdmissionTests(unittest.TestCase):
    def test_cap_two_defers_third_writer_across_repositories(self):
        tasks = [proposal("a", repo="org/a"), proposal("b", repo="org/b"), proposal("c")]
        plan = plan_admission(tasks, snapshot())
        self.assertEqual([x.verdict for x in plan.decisions], ["ADMIT", "ADMIT", "DEFER"])
        self.assertEqual((plan.available_write_slots, plan.remaining_write_slots), (2, 0))
        self.assertEqual(plan.as_dict()["authority"], "ADVISORY")

    def test_one_active_writer_leaves_one_slot(self):
        plan = plan_admission([proposal("new"), proposal("defer")], snapshot(ActiveTask("old", "org/another", "WRITE")))
        self.assertEqual([x.verdict for x in plan.decisions], ["ADMIT", "DEFER"])
        self.assertEqual(plan.available_write_slots, 1)

    def test_full_capacity_defers_writes(self):
        plan = plan_admission([proposal("c")], snapshot(ActiveTask("a", "org/a", "WRITE"), ActiveTask("b", "org/b", "WRITE")))
        self.assertEqual(plan.decisions[0].verdict, "DEFER")

    def test_read_bypasses_full_capacity(self):
        plan = plan_admission([proposal("r", "READ")], snapshot(ActiveTask("w", "org/a", "WRITE")), max_writers=1)
        self.assertEqual(plan.decisions[0].verdict, "ADMIT")
        self.assertEqual(plan.remaining_write_slots, 0)

    def test_active_id_is_rejected_even_if_mode_differs(self):
        plan = plan_admission([proposal("same", "READ")], snapshot(ActiveTask("same", "org/a", "WRITE")))
        self.assertEqual((plan.decisions[0].verdict, plan.decisions[0].reason), ("REJECT", "already_active"))

    def test_duplicate_proposals_are_invalid(self):
        with self.assertRaises(AdmissionError):
            plan_admission([proposal("a"), proposal("a", "READ")], snapshot())

    def test_duplicate_active_ids_are_invalid(self):
        with self.assertRaises(AdmissionError):
            plan_admission([], OccupancySnapshot(True, (ActiveTask("a", "a", "READ"), ActiveTask("a", "b", "READ")), 0))

    def test_missing_unverified_or_wrong_snapshot_fails_closed(self):
        for value in (None, "unknown", OccupancySnapshot(False, (), 0), OccupancySnapshot(1, (), 0)):
            with self.subTest(value=value), self.assertRaises(AdmissionError):
                plan_admission([proposal("w")], value)

    def test_unknown_invalid_or_contradictory_count_refused(self):
        for count in (None, False, -1, 2, "0", 0.0):
            with self.subTest(count=count), self.assertRaises(AdmissionError):
                plan_admission([proposal("a")], OccupancySnapshot(True, (ActiveTask("w", "org/a", "WRITE"),), count))

    def test_zero_capacity_refuses_write_allows_read(self):
        plan = plan_admission([proposal("a"), proposal("b", "READ")], snapshot(), max_writers=0)
        self.assertEqual([x.verdict for x in plan.decisions], ["DEFER", "ADMIT"])

    def test_overcapacity_remains_deferred(self):
        plan = plan_admission([proposal("a")], snapshot(ActiveTask("w", "org/a", "WRITE")), max_writers=0)
        self.assertEqual(plan.decisions[0].verdict, "DEFER")

    def test_invalid_max_writers(self):
        for value in (-1, None, True, 1.5, "2"):
            with self.subTest(value=value), self.assertRaises(AdmissionError):
                plan_admission([proposal("a")], snapshot(), max_writers=value)

    def test_malformed_task_identity_mode_and_type(self):
        cases = [proposal(""), proposal("has spaces"), proposal("name", "DELETE"), proposal("ok", repo=""),
                 proposal("x", repo="a\nb"), {"task_id": "x"}, proposal(123)]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(AdmissionError):
                plan_admission([item], snapshot())

    def test_malformed_active_identity(self):
        with self.assertRaises(AdmissionError):
            plan_admission([], OccupancySnapshot(True, (ActiveTask(" bad", "org/repo", "READ"),), 0))

    def test_unbounded_generators_and_collections_refused(self):
        with self.assertRaises(AdmissionError):
            plan_admission((proposal(str(n)) for n in range(3)), snapshot())
        with self.assertRaises(AdmissionError):
            plan_admission([proposal("a")] * 100001, snapshot())

    def test_replay_deterministic_does_not_mutate_input(self):
        tasks = [proposal("a"), proposal("b", "READ")]
        active = [ActiveTask("z", "org/b", "WRITE")]
        snap = OccupancySnapshot(True, active, 1)
        first = plan_admission(tasks, snap)
        second = plan_admission(tasks, snap)
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(tasks, [proposal("a"), proposal("b", "READ")])
        self.assertEqual(active, [ActiveTask("z", "org/b", "WRITE")])
        self.assertEqual(snap.active_write_count, 1)


if __name__ == "__main__":
    unittest.main()
