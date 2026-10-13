import tempfile
import unittest
from pathlib import Path
from tools.doctor import check
from tools.install_global import execute
class DoctorTests(unittest.TestCase):
    def test_missing_then_ready(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            self.assertFalse(all(all(x.values()) for x in check(home,'all').values()))
            execute(home,{'codex','opencode','antigravity'})
            self.assertTrue(all(all(x.values()) for x in check(home,'all').values()))
