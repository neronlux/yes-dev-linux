#!/bin/bash
# Post-reboot validation for yes-dev-linux, run once by
# yes-dev-postboot.service (systemd user, oneshot). Waits for the
# desktop to exist, proves the stack with --selftest, ensures Chrome is
# up, fires a consent attach, and records the engine's verdict in
# ~/.local/share/YesDev/postboot.log.
set -u
LOG="$HOME/.local/share/YesDev/postboot.log"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "=== postboot check $(date -Is) ==="

# 1. wait for the graphical session (the service starts before it)
for _ in $(seq 1 60); do
    [ -S "/run/user/$(id -u)/wayland-0" ] && break
    sleep 2
done
sleep 10   # let the shell, portal and a11y bus settle

# 2. engine liveness
echo "engine: $(systemctl --user is-active yes-dev.service) / $(systemctl --user is-enabled yes-dev.service)"
tail -n 3 "$HOME/.local/share/YesDev/yes-dev.log" || true

# 3. full stack proof
/usr/bin/python3 "$HOME/yes-dev-linux/watcher_linux.py" --selftest || true

# 4. Chrome up (launch with session restore if the reboot didn't)
if ! ss -tlnp 2>/dev/null | grep -q 9222; then
    echo "chrome not listening - launching with session restore"
    setsid -f env XDG_RUNTIME_DIR="/run/user/$(id -u)" \
        WAYLAND_DISPLAY=wayland-0 \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus" \
        /usr/bin/google-chrome --ozone-platform=wayland --restore-last-session
    for _ in $(seq 1 30); do
        ss -tlnp 2>/dev/null | grep -q 9222 && break
        sleep 2
    done
fi

# 5. the real end-to-end: attach, prompt, engine approval
if ss -tlnp 2>/dev/null | grep -q 9222; then
    sleep 5   # let Chrome map its windows
    echo "--- consent attach (up to 120s) ---"
    /usr/bin/python3 "$HOME/yes-dev-linux/tools/consent-check.py" 120 || true
    echo "--- engine log tail ---"
    tail -n 8 "$HOME/.local/share/YesDev/yes-dev.log" || true
    if grep -q "APPROVED" <(tail -n 30 "$HOME/.local/share/YesDev/yes-dev.log"); then
        echo "VERDICT: post-reboot auto-approval WORKS"
    else
        echo "VERDICT: no APPROVED in the recent log - inspect above"
    fi
else
    echo "VERDICT: chrome debug port never came up - skipped the attach test"
fi
echo "=== postboot check complete $(date -Is) ==="
