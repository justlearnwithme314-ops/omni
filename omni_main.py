"""Entry point for the `omni` launchers.

Run as a script so that Omni's own folder - not the directory you happen to be in - is what Python imports `src`
from. The agent then works in your current directory, wherever that is.
"""
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.argv[0] = "omni"
runpy.run_module("src.cli", run_name="__main__", alter_sys=False)
