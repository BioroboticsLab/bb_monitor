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

echo "=== 5. Recent avahi/network events (did the resolver restart?) ==="
journalctl -u avahi-daemon --since '-3h' --no-pager 2>/dev/null | tail -15 \
  || echo "  (no journal access)"
echo
echo "If section 1 shows names that do not resolve, or section 2 shows exit=2,"
echo "the cameras are fine and mDNS on this host is the problem."
echo "If section 4 succeeds by IP while section 2 fails by name, same conclusion."
