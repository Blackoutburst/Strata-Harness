#!/bin/bash
# Reverse tunnel for the Strata harness, reconnecting forever while this script runs:
#   Ubuntu 127.0.0.1:19140 -> Mac Board 127.0.0.1:9140
#   Ubuntu 127.0.0.1:19145 -> current Mac emulator bridge (port re-read on every reconnect)
# dispatch_task.py ends the current ssh after restarting the bridge so the new port is picked up.
HOST=${1:-ubuntu@blackoutburst.com}
PORT_FILE=~/.local/state/strata-board-harness-bridge/tunnel-port
while true; do
  PORT=$(/usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["port"])' "$PORT_FILE" 2>/dev/null)
  FWD=(-R 127.0.0.1:19140:127.0.0.1:9140)
  [ -n "$PORT" ] && FWD+=(-R "127.0.0.1:19145:127.0.0.1:$PORT")
  echo "$(date '+%F %T') connect bridge_port=${PORT:-none}"
  # The remote side runs a named marker process instead of -N, so a stale session of OURS
  # can be told apart from any other sshd session and ended (see below). The marker exits
  # once its sshd session is gone, so killed connections leave nothing behind.
  /usr/bin/ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 \
    -o ServerAliveCountMax=3 -o ConnectTimeout=15 "${FWD[@]}" "$HOST" \
    'exec -a strata-tunnel-hold bash -c "while kill -0 \$PPID 2>/dev/null; do sleep 20; done"' </dev/null
  echo "$(date '+%F %T') ssh exited $?; reconnecting in 10s"
  # If our Mac side dropped without a FIN, Ubuntu's sshd keeps the dead session and its
  # listeners, and every reconnect fails ("remote port forwarding failed"). When the board
  # forward is bound but answers nothing, end our own marked session: killing its marker
  # process closes that sshd session and frees the ports. Other sessions are never touched.
  /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" '
    bound=$(ss -ltn | grep -cE ":1914[05] ")
    b=$(curl -s -m3 -o /dev/null -w "%{http_code}" http://127.0.0.1:19140/)
    if [ "$bound" -gt 0 ] && [ "$b" = 000 ]; then
      pkill -u "$(id -un)" -f "^strata-tunnel-hold" && echo "ended stale marked tunnel session"
    fi' 2>&1
  sleep 10
done
