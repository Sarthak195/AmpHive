#!/bin/sh
# Sets up the DuckDNS refresh cron on the VM.
#
# The token is a live credential and must NEVER be committed — pass it via env:
#   DUCKDNS_TOKEN=<your-token> ./setup_duckdns.sh
# (The previously committed token is burned; rotate it at duckdns.org.)
if [ -z "$DUCKDNS_TOKEN" ]; then
    echo "ERROR: set the DUCKDNS_TOKEN environment variable first." >&2
    exit 1
fi
mkdir -p ~/duckdns
cat > ~/duckdns/duck.sh << EOF
echo url="https://www.duckdns.org/update?domains=amphive&token=${DUCKDNS_TOKEN}&ip=" | curl -k -o ~/duckdns/duck.log -K -
EOF
chmod 700 ~/duckdns/duck.sh
(crontab -l 2>/dev/null; echo "*/5 * * * * ~/duckdns/duck.sh >/dev/null 2>&1") | crontab -
~/duckdns/duck.sh
