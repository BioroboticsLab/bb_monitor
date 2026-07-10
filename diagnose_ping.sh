#!/bin/bash
# Diagnose "Cannot reach <host>" reports from bb_monitor_systemcheck.
#
# Run this ON THE MONITOR HOST (the machine running bb_monitor_systemcheck.py),
# ideally while the problem is happening:
#
#     bash diagnose_ping.sh feedercama.local feedercamb.local exitcama.local
#
# It reproduces exactly what check_ping() does — one ICMP packet, short timeout,
# no interactive retry — and shows the exit code and stderr that the monitor sees.
set -u
HOSTS=("$@")
if [ ${#HOSTS[@]} -eq 0 ]; then
  HOSTS=(feedercama.local feedercamb.local feedercamc.local feedercamd.local
         exitcama.local exitcamb.local exitcamc.local exitcamd.local)
fi

echo "host:      $(hostname)"
echo "user:      $(id -un) (uid $(id -u), groups $(id -G))"
echo "ping:      $(command -v ping)  |  $(ping -V 2>&1 | head -1)"
echo "unpriv ICMP group range: $(cat /proc/sys/net/ipv4/ping_group_range 2>/dev/null || echo '?')"
echo

echo "=== 1. Name resolution (.local => mDNS/avahi) ==="
systemctl is-active avahi-daemon 2>/dev/null | sed 's/^/  avahi-daemon: /'
grep '^hosts:' /etc/nsswitch.conf | sed 's/^/  nsswitch  /'
for h in "${HOSTS[@]}"; do
  addr=$(getent hosts "$h" 2>/dev/null | awk '{print $1}' | paste -sd, -)
  printf "  %-22s -> %s\n" "$h" "${addr:-<<< DOES NOT RESOLVE >>>}"
done
echo

echo "=== 2. Exactly what check_ping() runs: ping -c 1 -W 2 <host> ==="
for h in "${HOSTS[@]}"; do
  err=$(ping -c 1 -W 2 "$h" 2>&1 >/dev/null); rc=$?
  case $rc in
    0) verdict="OK" ;;
    1) verdict="NO REPLY (host silent / dropped packet)" ;;
    2) verdict="RESOLVE/SETUP FAILURE -- not the camera's fault" ;;
    *) verdict="exit $rc" ;;
  esac
  printf "  %-22s exit=%-2s %-42s %s\n" "$h" "$rc" "$verdict" "$err"
done
echo

echo "=== 3. Same hosts, 3 packets (does a retry recover it?) ==="
for h in "${HOSTS[@]}"; do
  out=$(ping -c 3 -W 2 "$h" 2>&1 | tail -2 | head -1)
  printf "  %-22s %s\n" "$h" "$out"
done
echo

echo "=== 4. Ping the IPs directly (isolates DNS from the network) ==="
for h in "${HOSTS[@]}"; do
  ip=$(getent hosts "$h" 2>/dev/null | awk '{print $1; exit}')
  if [ -z "$ip" ]; then printf "  %-22s (no IP; cannot test)\n" "$h"; continue; fi
  ping -c 1 -W 2 "$ip" >/dev/null 2>&1 && r=OK || r="FAIL"
  printf "  %-22s %-16s %s\n" "$h" "$ip" "$r"
done
echo

echo "=== 5. How often does this host's own interface drop off? ==="
# Every "no longer relevant for mDNS" is this machine losing an interface: while it
# is gone, nothing resolves and no ICMP flows, so every camera looks dead at once.
if journalctl -u avahi-daemon --since '-24h' --no-pager >/dev/null 2>&1; then
  drops=$(journalctl -u avahi-daemon --since '-24h' --no-pager 2>/dev/null \
          | grep -c 'no longer relevant for mDNS')
  echo "  interface drops in the last 24h: $drops"
  echo "  (a check runs every 10 min; two consecutive checks must both land in a"
  echo "   dropout to raise an alert, so a handful of short drops should stay silent)"
  echo
  echo "  most recent drop/rejoin pairs:"
  journalctl -u avahi-daemon --since '-24h' --no-pager 2>/dev/null \
    | grep -E 'no longer relevant|New relevant interface' \
    | tail -8 | sed 's/^/    /'
else
  echo "  (no journal access)"
fi
echo

echo "=== 6. WiFi power saving ==="
# mDNS is multicast, and a WiFi client in power save only wakes for multicast at DTIM
# beacons. Between checks the link sits idle and sleeps, so every check starts cold
# and the first lookup stalls for seconds. An interactive ping looks fast because the
# ssh session has already woken the link.
for dev in $(ls /sys/class/net 2>/dev/null); do
  [ -d "/sys/class/net/$dev/wireless" ] || continue
  ps=$(iw dev "$dev" get power_save 2>/dev/null | awk '{print $NF}')
  echo "  $dev power_save: ${ps:-unknown}"
  [ "$ps" = "on" ] && echo "    ^ likely cause of stalled lookups. Turn it off:  sudo iw dev $dev set power_save off"
done
echo

echo "=== 7. Routing (which interface reaches the cameras?) ==="
echo "  default route: $(ip route show default 2>/dev/null | head -1)"
for h in "${HOSTS[@]}"; do
  ip=$(getent hosts "$h" 2>/dev/null | awk '{print $1; exit}')
  [ -n "$ip" ] || continue
  echo "  $h -> $(ip route get "$ip" 2>/dev/null | head -1)"
  break
done
echo "  (the cameras should route over the WiFi interface while the default route"
echo "   stays on Ethernet; nothing below changes routing)"
echo

echo "=== 8. Suggested /etc/hosts lines (bypass mDNS entirely) ==="
echo "  Static entries make ping AND ssh stop depending on avahi + multicast."
echo "  They do not touch routing: the kernel still picks the interface by subnet."
echo "  Reserve the leases on the router so the IPs cannot move."
for h in "${HOSTS[@]}"; do
  ip=$(getent hosts "$h" 2>/dev/null | awk '{print $1; exit}')
  [ -n "$ip" ] && printf "    %-16s %s %s\n" "$ip" "$h" "${h%%.local}"
done
echo
echo "Reading this:"
echo "  * section 2 exit=2 / section 1 unresolved  -> mDNS on THIS host, cameras are fine"
echo "  * section 2 shows 'did not return'         -> the lookup STALLED (see section 6)"
echo "  * section 4 OK by IP while section 2 fails -> same conclusion"
echo "  * section 5 shows frequent drops           -> this host's WiFi link is unstable"
echo
echo "NOTE: run this from a COLD link to reproduce what the monitor sees. An ssh"
echo "session keeps the WiFi awake, which hides the stall. Try:"
echo "  echo 'bash $(cd "$(dirname "$0")" && pwd)/diagnose_ping.sh > /tmp/diag.txt 2>&1' | at now + 20 minutes"
