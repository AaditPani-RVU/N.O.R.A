from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from nora.config import get_config


def setup_logger() -> logging.Logger:
    cfg = get_config().get("logging", {})
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = cfg.get("file", "nora.log")

    # Rotation, not a plain FileHandler. NORA runs as a login-session daemon and
    # nothing ever truncates this file, so an unrotated handler is an unbounded
    # write to the user's home directory — measured at 177 MB / 2.4 M lines
    # before this, and still growing. The cap makes the worst case
    # max_bytes * (backup_count + 1), which is a number that can be stated.
    #
    # 10 MB x 5 is chosen to keep roughly the last day of DEBUG-level logs at
    # NORA's normal rate. Raise backup_count rather than max_bytes if you need
    # more history: grep is fine across files, and a single file large enough to
    # matter is one an editor cannot open.
    max_bytes = int(cfg.get("max_bytes", 10 * 1024 * 1024))
    backup_count = int(cfg.get("backup_count", 5))

    # "nora" is the parent of every nora.* module logger — one handler catches all
    logger = logging.getLogger("nora")
    logger.setLevel(level)

    if not logger.handlers:
        # Console handler
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(level)
        fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
        ch.setFormatter(fmt)
        logger.addHandler(ch)

        # File handler
        try:
            fh: logging.Handler = RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count,
                encoding="utf-8",
            )
        except OSError:
            # A log we cannot open must not stop NORA from starting: the console
            # handler is already attached, so this degrades to stdout only.
            logger.warning("Could not open %s for writing — logging to console only.", log_file)
            return logger
        fh.setLevel(level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger
