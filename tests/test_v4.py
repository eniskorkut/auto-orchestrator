import unittest
from tools.supervisor import validate_plan

class V4Tests(unittest.TestCase):
    def test_empty_plan_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan({'tasks': []})
