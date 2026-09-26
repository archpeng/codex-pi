#!/usr/bin/env python3
"""Ignore SIGTERM, record pid, sleep. The runtime must SIGKILL the group."""
import os
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(600)
