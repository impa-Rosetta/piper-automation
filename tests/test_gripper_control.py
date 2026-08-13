from __future__ import annotations

import unittest

from gripper.control_gripper import command_payload


class GripperControlTests(unittest.TestCase):
    def test_close_protocol(self) -> None:
        self.assertEqual(command_payload("close")["execute"], "on")
        self.assertEqual(command_payload("grip")["execute"], "on")

    def test_open_protocol(self) -> None:
        self.assertEqual(command_payload("open")["execute"], "off")
        self.assertEqual(command_payload("release")["execute"], "off")


if __name__ == "__main__":
    unittest.main()
