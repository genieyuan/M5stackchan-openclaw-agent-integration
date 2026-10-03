#!/bin/bash
# StackChan watchdog (every 5 min via launchd). Edge down -> restart it once, recheck. Anything still
# failing -> one alert per state change: config "notify_command" runs with the text in $STACKCHAN_ALERT.
D=$(cd "$(dirname "$0")" && pwd); S=$D/state; LOG=$D/logs/watchdog.log; PKI=$D/../pki
cfg() { python3 -c "import json,sys;print(json.load(open('$D/config.json')).get(sys.argv[1]) or '')" "$1"; }
H=(-H "Device-Id: $(cfg device_mac)" -H "Client-Id: $(cfg device_client_id)" -H "content-type: application/json")
EDGE="http://$(cfg bind):$(cfg port)"; CHAN="$(cfg channel_url)"; NOTIFY="$(cfg notify_command)"
edge_ok() { curl -s -m 5 -X POST $EDGE/xiaozhi/ota/ "${H[@]}" -d '{}' | grep -q '"websocket"'; }
chan_ok() { curl -s -m 8 --cacert $PKI/ca.crt --cert $PKI/edge.crt --key $PKI/edge.key $CHAN/v1/interrupt \
            -H 'content-type: application/json' -d '{"turn_id":"00000000-0000-4000-8000-00000000d09d"}' | grep -q '"cancelled"'; }
ts() { date '+%Y-%m-%d %H:%M:%S'; }
problems=()
if ! edge_ok; then
  echo "$(ts) edge not answering; restarting" >> $LOG
  launchctl kickstart -k gui/$(id -u)/${EDGE_LABEL:-com.stackchan.edge}; sleep 12
  edge_ok || problems+=("voice edge ($EDGE) not answering after restart")
fi
chan_ok || problems+=("StackChan channel plugin ($CHAN) not answering")
state=$( [ ${#problems[@]} -eq 0 ] && echo ok || echo bad )
prev=$(cat $S/watchdog.state 2>/dev/null || echo ok)
echo "$(ts) $state ${problems[*]}" >> $LOG
if [ "$state" != "$prev" ]; then
  if [ "$state" = bad ]; then msg="⚠️ StackChan watchdog: $(IFS='; '; echo "${problems[*]}"). Edge auto-restart attempted. Logs: .51 $D/logs/"
  else msg="✅ StackChan watchdog: recovered, edge and channel answering."; fi
  [ -n "$NOTIFY" ] && STACKCHAN_ALERT="$msg" bash -c "$NOTIFY" >> $LOG 2>&1
  echo "$state" > $S/watchdog.state
fi
