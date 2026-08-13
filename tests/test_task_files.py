from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from teach.run_task_sequence import load_task, preflight


class TaskFileTests(unittest.TestCase):
    def test_load_and_preflight_minimal_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_dir = root / "layer_01_slot_01"
            task_dir.mkdir()
            (task_dir / "task.json").write_text(
                json.dumps({"task_id": "layer_01_slot_01"}), encoding="utf-8"
            )
            (task_dir / "trajectory.csv").write_text(
                "0.0,0,0,0,0,0,0\n0.005,0.01,0,0,0,0,0\n",
                encoding="utf-8",
            )
            (task_dir / "gripper_timeline.json").write_text(
                json.dumps({"events": [{"time_s": 0.003, "action": "close"}]}),
                encoding="utf-8",
            )

            task = load_task(root, "layer_01_slot_01")
            report = preflight([task], home_start_limit=0.1, boundary_limit=0.1)

            self.assertEqual(task.rows, 2)
            self.assertEqual(task.event_count, 1)
            self.assertTrue(all(item["ok"] for item in report))


if __name__ == "__main__":
    unittest.main()
