import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xianyu_bridge.env import load_env


class EnvTest(unittest.TestCase):
    def test_loads_file_without_overwriting_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("FROM_FILE=file-value\nKEEP=file-value\nQUOTED='quoted value'\n", encoding="utf-8")
            with patch.dict(os.environ, {"KEEP": "process-value"}, clear=True):
                load_env(path)
                self.assertEqual("file-value", os.environ["FROM_FILE"])
                self.assertEqual("process-value", os.environ["KEEP"])
                self.assertEqual("quoted value", os.environ["QUOTED"])


if __name__ == "__main__":
    unittest.main()
