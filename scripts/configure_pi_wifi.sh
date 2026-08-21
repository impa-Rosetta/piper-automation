#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/configure_pi_wifi.sh --ssid "NETWORK_NAME" [--interface wlan0]

The Wi-Fi password is read interactively and is never accepted as a command-line
argument. Keep Ethernet or a local console available while changing networks.
EOF
}

SSID=""
INTERFACE="wlan0"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssid)
      SSID="${2:-}"
      shift 2
      ;;
    --interface)
      INTERFACE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$SSID" ]]; then
  echo "--ssid is required." >&2
  exit 2
fi

read -r -s -p "Wi-Fi password for '$SSID': " WIFI_PASSWORD
echo
if [[ -z "$WIFI_PASSWORD" ]]; then
  echo "Password cannot be empty." >&2
  exit 2
fi

echo "Current addresses:"
ip -br address || true

if command -v nmcli >/dev/null 2>&1; then
  echo "NetworkManager detected. Adding the new Wi-Fi connection ..."
  sudo nmcli device wifi rescan ifname "$INTERFACE" || true
  sudo nmcli device wifi connect "$SSID" password "$WIFI_PASSWORD" ifname "$INTERFACE"
  sudo nmcli connection modify "$SSID" connection.autoconnect yes
  echo "Connection saved. The current SSH session may disconnect when the route changes."
  exit 0
fi

DEFAULT_DEVICE="$(ip route show default 2>/dev/null | awk 'NR==1 {print $5}')"
if [[ "$DEFAULT_DEVICE" == "$INTERFACE" && -n "${SSH_TTY:-}" ]]; then
  echo "Refusing a Netplan switch over the only active Wi-Fi SSH route." >&2
  echo "Connect Ethernet or run from a local console, then retry." >&2
  exit 1
fi

echo "NetworkManager is not installed; using Netplan."
echo "Keep Ethernet or a local console connected until the new network is verified."

SSID_YAML="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$SSID")"
PASSWORD_YAML="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$WIFI_PASSWORD")"
TEMP_FILE="$(mktemp)"
trap 'rm -f "$TEMP_FILE"' EXIT
cat >"$TEMP_FILE" <<EOF
network:
  version: 2
  renderer: networkd
  wifis:
    $INTERFACE:
      dhcp4: true
      optional: true
      access-points:
        $SSID_YAML:
          password: $PASSWORD_YAML
EOF

sudo install -m 0600 "$TEMP_FILE" /etc/netplan/99-piper-wifi.yaml
sudo netplan generate
sudo netplan apply
echo "Netplan applied. Check the router/hotspot client list for the new IP."
