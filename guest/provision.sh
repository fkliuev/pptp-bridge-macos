#!/bin/sh
set -eu
command -v socat >/dev/null
command -v pppd >/dev/null
modprobe ppp_mppe
install -d -m 755 /etc/ppp/peers /etc/ppp/ip-up.d
cat > /etc/ppp/peers/ssh-only <<'PEER'
pty "/usr/bin/socat STDIO OPEN:/dev/virtio-ports/local.pptp"
user "@VPN_USER@"
linkname ssh-vpn
ipparam ssh-vpn
noauth
noipdefault
nodefaultroute
noipv6
ipcp-accept-local
ipcp-accept-remote
refuse-pap
refuse-eap
refuse-chap
refuse-mschap
require-mppe-128
hide-password
mtu 1400
mru 1400
lcp-echo-interval 15
lcp-echo-failure 4
PEER
cat > /etc/ppp/ip-up.d/95pptp-bridge <<'UP'
#!/bin/sh
set -eu
[ "$6" = ssh-vpn ] || exit 0
@ROUTES@
rm -f /run/ssh-vpn-secret
UP
chmod 755 /etc/ppp/ip-up.d/95pptp-bridge
cat > /etc/systemd/system/ssh-pptp.service <<'UNIT'
[Unit]
Description=PPTP Bridge
[Service]
Type=simple
@GUARDS@
ExecStart=/usr/sbin/pppd nodetach call ssh-only file /run/ssh-vpn-secret
ExecStopPost=/usr/bin/rm -f /run/ssh-vpn-secret
TimeoutStopSec=8
UNIT
systemctl daemon-reload
