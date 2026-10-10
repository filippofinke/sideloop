import atexit
import os
import shutil
import tempfile

# sideloop.config reads these on import, so they are set before any test module imports sideloop.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="sideloop-test-")
atexit.register(shutil.rmtree, os.environ["DATA_DIR"], True)
os.environ.setdefault("MUX", "builtin")


def reset_data():
    from sideloop.config import DATA
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)


class FakeJob:
    def __init__(self):
        self.lines, self.cancelled = [], False

    def say(self, line):
        self.lines.append(line)
