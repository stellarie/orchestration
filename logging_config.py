import logging
import logging.handlers
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_FMT = logging.Formatter("%(asctime)s [%(name)-20s] %(levelname)s  %(message)s")


class _WinRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that silently skips rotation when Windows file
    locking prevents the rename (e.g. uvicorn --reload child process)."""

    def doRollover(self):
        try:
            super().doRollover()
        except PermissionError:
            pass  # another process holds the file — skip this rotation cycle


def setup_logging():
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)

    fh = _WinRotatingFileHandler(
        LOG_DIR / "orchestration.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(_FMT)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(_FMT)
    root.addHandler(sh)
