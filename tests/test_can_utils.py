from __future__ import annotations

import unittest
from unittest.mock import patch

from teach.can_utils import require_can_interface


class CanUtilsTests(unittest.TestCase):
    @patch("teach.can_utils.Path.exists", return_value=False)
    def test_missing_interface_has_actionable_error(self, _exists: object) -> None:
        with self.assertRaises(SystemExit) as context:
            require_can_interface("can0")
        self.assertIn("does not exist", str(context.exception))
        self.assertIn("1000000", str(context.exception))


if __name__ == "__main__":
    unittest.main()
