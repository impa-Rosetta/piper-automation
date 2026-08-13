from __future__ import annotations

import unittest

from teach.trajectory_math import (
    max_abs_joint_error,
    rows_to_samples,
    sample_linear,
)


class TrajectoryMathTests(unittest.TestCase):
    def test_delta_times_become_cumulative(self) -> None:
        rows = [
            [0.0, 0, 0, 0, 0, 0, 0],
            [0.1, 1, 1, 1, 1, 1, 1],
            [0.2, 2, 2, 2, 2, 2, 2],
        ]
        samples = rows_to_samples(rows, have_gripper=False)
        self.assertEqual(
            [round(sample.time_from_start, 6) for sample in samples],
            [0.0, 0.1, 0.3],
        )

    def test_linear_interpolation_preserves_position_path(self) -> None:
        rows = [
            [0.0, 0, 0, 0, 0, 0, 0, 0],
            [0.1, 1, 2, 3, 4, 5, 6, 10],
        ]
        samples = rows_to_samples(rows, have_gripper=True)
        middle = sample_linear(samples, 0.05)
        self.assertEqual(middle.joints, (0.5, 1.0, 1.5, 2.0, 2.5, 3.0))
        self.assertEqual(middle.gripper, 5.0)

    def test_interpolation_is_bounded_at_ends(self) -> None:
        rows = [
            [0.0, 0, 0, 0, 0, 0, 0],
            [0.1, 1, 1, 1, 1, 1, 1],
        ]
        samples = rows_to_samples(rows, have_gripper=False)
        self.assertEqual(sample_linear(samples, -1).joints, samples[0].joints)
        self.assertEqual(sample_linear(samples, 5).joints, samples[-1].joints)

    def test_joint_error(self) -> None:
        error = max_abs_joint_error(
            [0, 1, 2, 3, 4, 5],
            [0, 1.1, 1.7, 3, 4, 5],
        )
        self.assertAlmostEqual(error, 0.3)

    def test_negative_delta_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rows_to_samples(
                [
                    [0.0, 0, 0, 0, 0, 0, 0],
                    [-0.1, 0, 0, 0, 0, 0, 0],
                ],
                have_gripper=False,
            )


if __name__ == "__main__":
    unittest.main()
