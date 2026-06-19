mkdir -p ~/duckdns
cat > ~/duckdns/duck.sh << 'EOF'
echo url="https://www.duckdns.org/update?domains=amphive&token=4ac6a7ee-98e9-4cc9-bb0d-05db5bb5ed4b&ip=" | curl -k -o ~/duckdns/duck.log -K -
EOF
chmod 700 ~/duckdns/duck.sh
(crontab -l 2>/dev/null; echo "*/5 * * * * ~/duckdns/duck.sh >/dev/null 2>&1") | crontab -
~/duckdns/duck.sh
