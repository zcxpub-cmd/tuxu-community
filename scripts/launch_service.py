#!/usr/bin/env python3
"""Launch an owned local service in its own process session."""
import argparse
from pathlib import Path
import subprocess

p = argparse.ArgumentParser()
p.add_argument('--cwd', required=True)
p.add_argument('--log', required=True)
p.add_argument('--pid', required=True)
p.add_argument('command', nargs=argparse.REMAINDER)
a = p.parse_args()
command = a.command[1:] if a.command[:1] == ['--'] else a.command
if not command:
    p.error('missing command')
for name in (a.log, a.pid):
    Path(name).parent.mkdir(parents=True, exist_ok=True)
with open(a.log, 'ab', buffering=0) as stream:
    child = subprocess.Popen(command, cwd=a.cwd, stdin=subprocess.DEVNULL,
                             stdout=stream, stderr=stream, start_new_session=True)
Path(a.pid).write_text(str(child.pid) + '\n')
print(child.pid)
