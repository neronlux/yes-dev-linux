#!/bin/bash
# Compile check + both offline suites. Exits non-zero when anything fails.
set -e
cd "$(dirname "$0")/.."
/usr/bin/python3 -m py_compile watcher_linux.py auto_click.py platform_linux.py \
    tools/doctor.py tools/consent-check.py tests/test_selfheal.py tests/test_finder.py
echo "compile: OK"
echo "--- tests/test_selfheal.py ---"
/usr/bin/python3 tests/test_selfheal.py
echo "--- tests/test_finder.py ---"
/usr/bin/python3 tests/test_finder.py
echo "ALL SUITES PASS"
