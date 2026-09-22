#!/usr/bin/python3
"""Reproduce the consent hang: attach a CDP client and call list_pages.

If the "Allow remote debugging?" bubble is up (and unapproved), the
tools/call hangs mid-handshake — that is the trigger used by every live
test in TESTING.md. `RESPONDED` = consent already granted (or no gate);
`HUNG` = prompt is (probably) up.

Usage:
    /usr/bin/python3 tools/consent-check.py [timeout-seconds]

Needs npx (Node) and a running Chrome with remote debugging toggled on
at chrome://inspect/#remote-debugging. Rule out a sick server first:
`ss -tlnp | grep 9222` must show chrome listening.
"""
import json
import subprocess
import sys
import threading
import time

TIMEOUT_S = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0

proc = subprocess.Popen(
    ["npx", "-y", "chrome-devtools-mcp@latest", "--autoConnect"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, text=True, bufsize=1)
got = {}


def reader():
    for line in proc.stdout:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("id") == 3:
            got["resp"] = line.strip()[:200]
            break


threading.Thread(target=reader, daemon=True).start()


def send(o):
    proc.stdin.write(json.dumps(o) + "\n")
    proc.stdin.flush()


send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "clientInfo": {"name": "probe", "version": "0.1"}}})
time.sleep(2)
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
time.sleep(1)
t0 = time.time()
send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
      "params": {"name": "list_pages", "arguments": {}}})
while time.time() - t0 < TIMEOUT_S and "resp" not in got:
    time.sleep(0.5)
dt = time.time() - t0
print(f"RESULT after {dt:.1f}s:",
      "RESPONDED (no prompt block)" if "resp" in got
      else "HUNG (prompt likely up)")
if "resp" in got:
    print(got["resp"])
proc.terminate()
