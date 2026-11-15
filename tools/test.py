"""Run local checks with all temporary files inside this workspace."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.path.insert(0, str(ROOT))
    folder = ROOT / "output" / "tmp"
    folder.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(folder)
    pattern = sys.argv[1] if len(sys.argv) > 1 else "test_*.py"
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=pattern)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
