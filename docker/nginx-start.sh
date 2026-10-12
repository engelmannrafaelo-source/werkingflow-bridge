#!/bin/sh
# nginx-start.sh — start command of the bridge LB (docker-compose.yml and
# docker-compose-prod.yml mount it to /etc/nginx/nginx-start.sh).
#
# 1. BRIDGE_NET_GATEWAY = gateway of the container's network, read from its
#    default route. docker-proxy relays connections from that address (external
#    IPv6 on [::]:8000, host-local calls), so nginx.conf must NOT trust it
#    ($origin_sender_trusted, IDEMR R3). Docker assigns the subnet at network
#    creation — no file can know it beforehand. The LB sits on exactly one
#    network in both topologies, so the default route IS that gateway.
#    No usable default route → exit loud; starting with the gateway trusted
#    would silently reopen the hole.
# 2. envsubst nginx.conf + the upstreams include, copy the worker map, exec
#    openresty — what the compose command did inline before.
set -eu

hex=$(awk '$2 == "00000000" && $3 != "00000000" { print $3; exit }' /proc/net/route)
if [ -z "$hex" ] || [ ${#hex} -ne 8 ]; then
    echo "nginx-start: FATAL no IPv4 default route in /proc/net/route — cannot" \
         "determine BRIDGE_NET_GATEWAY; refusing to start with the gateway trusted" >&2
    exit 1
fi
# /proc/net/route is little-endian hex: 010012AC -> 172.18.0.1
BRIDGE_NET_GATEWAY=$(printf '%d.%d.%d.%d' \
    "0x$(echo "$hex" | cut -c7-8)" "0x$(echo "$hex" | cut -c5-6)" \
    "0x$(echo "$hex" | cut -c3-4)" "0x$(echo "$hex" | cut -c1-2)")
export BRIDGE_NET_GATEWAY
echo "nginx-start: BRIDGE_NET_GATEWAY=${BRIDGE_NET_GATEWAY} (not a trusted sender)"

envsubst '$BRIDGE_BACKUP_HOST $BRIDGE_ID $METRICS_READER_TARGET $BRIDGE_NET_GATEWAY' \
    < /etc/nginx/nginx.conf > /tmp/nginx.conf
envsubst '$BRIDGE_BACKUP_HOST $BRIDGE_ID' < /etc/nginx/upstreams.conf > /tmp/upstreams.conf
cp /etc/nginx/worker-map.conf /tmp/worker-map.conf
exec openresty -c /tmp/nginx.conf -g 'daemon off;'
