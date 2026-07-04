#!/usr/bin/env bash
# ============================================================================
#  LXC Commander – Community-Style Installer
#
#  Einzeiler (auf dem Proxmox-VE-Host als root ausführen):
#    bash <(curl -fsSL https://raw.githubusercontent.com/LarsMT35/proxmox-lxc-manager/main/install.sh)
#
#  Modi:
#    * Host-Installation      – App läuft direkt auf dem PVE-Host (pct lokal)
#    * LXC-Installation       – Installer erstellt einen eigenen Management-
#                               Container, installiert die App darin und
#                               richtet einen eingeschränkten SSH-Zugang
#                               zum Host ein (empfohlen)
#    * Update                 – bestehende Installation aktualisieren
#                               (Konfiguration & Datenbank bleiben erhalten)
#    * Deinstallation
#
#  Optionen:  --host | --lxc | --update | --uninstall   (sonst interaktiv)
# ============================================================================
set -euo pipefail

REPO_OWNER="LarsMT35"
REPO_NAME="proxmox-lxc-manager"
REPO_BRANCH="${LXC_COMMANDER_BRANCH:-main}"
TARBALL_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${REPO_BRANCH}.tar.gz"

APP="LXC Commander"
DEST=/opt/lxc-commander
DATA_DIR=/var/lib/lxc-commander
SERVICE=lxc-commander
PORT=8420

# ---------------------------------------------------------------- Ausgabe --
RD=$'\033[01;31m'; GN=$'\033[1;92m'; YW=$'\033[33m'; BL=$'\033[36m'; CL=$'\033[m'
msg_info()  { echo -e " ${YW}➜${CL}  $1"; }
msg_ok()    { echo -e " ${GN}✔${CL}  $1"; }
msg_error() { echo -e " ${RD}✖${CL}  $1" >&2; }
fatal()     { msg_error "$1"; exit 1; }

header() {
  cat <<'EOF'
    __   _  ________   ______                                          __
   / /  | |/ / ____/  / ____/___  ____ ___  ____ ___  ____ _____  ____/ /__  _____
  / /   |   / /      / /   / __ \/ __ `__ \/ __ `__ \/ __ `/ __ \/ __  / _ \/ ___/
 / /___/   / /___   / /___/ /_/ / / / / / / / / / / / /_/ / / / / /_/ /  __/ /
/_____/_/|_\____/   \____/\____/_/ /_/ /_/_/ /_/ /_/\__,_/_/ /_/\__,_/\___/_/

        Update-Dashboard für Proxmox-VE-LXC-Container
EOF
  echo
}

# ------------------------------------------------------------- Umgebung ---
require_root() {
  [ "$(id -u)" -eq 0 ] || fatal "Bitte als root ausführen."
}

on_pve_host() { command -v pct >/dev/null 2>&1 && command -v pveversion >/dev/null 2>&1; }

have_whiptail() { command -v whiptail >/dev/null 2>&1; }

ask() { # ask "Frage" "Default"  -> REPLY
  local q="$1" def="${2:-}"
  if have_whiptail; then
    REPLY=$(whiptail --backtitle "$APP" --inputbox "$q" 10 68 "$def" \
            --title "$APP" 3>&1 1>&2 2>&3) || fatal "Abgebrochen."
  else
    read -rp "$q [$def]: " REPLY
    REPLY="${REPLY:-$def}"
  fi
}

confirm() { # confirm "Frage" -> 0/1
  if have_whiptail; then
    whiptail --backtitle "$APP" --title "$APP" --yesno "$1" 10 68
  else
    read -rp "$1 [j/N]: " r; [[ "$r" =~ ^[jJyY] ]]
  fi
}

# --------------------------------------------------------- Quellen holen --
fetch_sources() { # legt Quellcode nach $SRC_DIR
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /)"
  if [ -d "$script_dir/backend" ] && [ -f "$script_dir/requirements.txt" ]; then
    SRC_DIR="$script_dir"
    msg_ok "Lokale Quellen gefunden: $SRC_DIR"
    return
  fi
  msg_info "Lade $APP ($REPO_BRANCH) von GitHub …"
  SRC_TMP="$(mktemp -d /tmp/lxc-commander.XXXXXX)"
  curl -fsSL "$TARBALL_URL" | tar -xz -C "$SRC_TMP" --strip-components=1 \
    || fatal "Download fehlgeschlagen: $TARBALL_URL"
  SRC_DIR="$SRC_TMP"
  msg_ok "Quellen geladen."
}

# ------------------------------------------------- App-Installation (lokal)
install_app() { # $1 = proxmox-mode (local|ssh)  – installiert nach $DEST
  local mode="$1"

  msg_info "Installiere Systempakete …"
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv curl ca-certificates >/dev/null
  msg_ok "Systempakete installiert."

  msg_info "Kopiere Anwendung nach $DEST …"
  mkdir -p "$DEST" "$DATA_DIR"
  cp -r "$SRC_DIR/backend" "$SRC_DIR/frontend" "$SRC_DIR/systemd" "$DEST/"
  cp "$SRC_DIR/requirements.txt" "$DEST/"
  # Konfiguration niemals überschreiben
  if [ ! -f "$DEST/config/containers.yaml" ]; then
    mkdir -p "$DEST/config"
    cp "$SRC_DIR/config/containers.yaml" "$DEST/config/"
    [ "$mode" = "ssh" ] && sed -i 's/^  mode: local/  mode: ssh/' "$DEST/config/containers.yaml"
  fi
  msg_ok "Anwendung kopiert."

  msg_info "Erstelle Python-Umgebung …"
  python3 -m venv "$DEST/venv"
  "$DEST/venv/bin/pip" install -q --upgrade pip
  "$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt"
  msg_ok "Python-Umgebung bereit."

  if grep -q "CHANGE-ME-LONG-RANDOM-TOKEN" "$DEST/config/containers.yaml"; then
    TOKEN=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)
    sed -i "s/CHANGE-ME-LONG-RANDOM-TOKEN/$TOKEN/" "$DEST/config/containers.yaml"
    msg_ok "API-Token generiert."
  else
    TOKEN="(unverändert – siehe $DEST/config/containers.yaml)"
  fi

  msg_info "Richte systemd-Dienst ein …"
  cp "$DEST/systemd/$SERVICE.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now "$SERVICE" >/dev/null
  msg_ok "Dienst '$SERVICE' läuft."
}

update_app() {
  [ -d "$DEST" ] || fatal "Keine Installation unter $DEST gefunden."
  fetch_sources
  msg_info "Aktualisiere Anwendung (Konfiguration & Datenbank bleiben erhalten) …"
  systemctl stop "$SERVICE" 2>/dev/null || true
  cp -r "$SRC_DIR/backend" "$SRC_DIR/frontend" "$SRC_DIR/systemd" "$DEST/"
  cp "$SRC_DIR/requirements.txt" "$DEST/"
  "$DEST/venv/bin/pip" install -q --upgrade -r "$DEST/requirements.txt"
  cp "$DEST/systemd/$SERVICE.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl restart "$SERVICE"
  msg_ok "$APP wurde aktualisiert."
}

uninstall_app() {
  confirm "$APP wirklich deinstallieren?\n\nKonfiguration und Datenbank ($DATA_DIR) bleiben erhalten." || exit 0
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  rm -f "/etc/systemd/system/$SERVICE.service"
  systemctl daemon-reload
  rm -rf "$DEST/backend" "$DEST/frontend" "$DEST/systemd" "$DEST/venv" "$DEST/requirements.txt"
  msg_ok "$APP deinstalliert. Verblieben: $DEST/config, $DATA_DIR"
}

# ------------------------------------------------ Variante B: eigener LXC --
next_ctid()   { pvesh get /cluster/nextid 2>/dev/null || echo 200; }
host_ip()     { ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}'; }

pick_template() {
  msg_info "Suche Debian-12-Template …"
  pveam update >/dev/null 2>&1 || true
  local tmpl
  tmpl=$(pveam available --section system 2>/dev/null \
         | awk '{print $2}' | grep -E '^debian-12-standard' | sort -V | tail -1)
  [ -n "$tmpl" ] || fatal "Kein debian-12-standard-Template gefunden (pveam available)."
  if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$tmpl"; then
    msg_info "Lade Template $tmpl nach '$TEMPLATE_STORAGE' …"
    pveam download "$TEMPLATE_STORAGE" "$tmpl" >/dev/null
  fi
  TEMPLATE="$TEMPLATE_STORAGE:vztmpl/$tmpl"
  msg_ok "Template: $tmpl"
}

lxc_settings() {
  local def_ctid; def_ctid="$(next_ctid)"
  CTID="$def_ctid"; HOSTNAME_CT="lxc-commander"; DISK="4"; RAM="512"; CORES="1"
  BRIDGE="vmbr0"; IPCONF="dhcp"; GATEWAY=""
  ROOTFS_STORAGE="$(pvesm status -content rootdir 2>/dev/null | awk 'NR==2{print $1}')"
  ROOTFS_STORAGE="${ROOTFS_STORAGE:-local-lvm}"
  TEMPLATE_STORAGE="$(pvesm status -content vztmpl 2>/dev/null | awk 'NR==2{print $1}')"
  TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"

  if confirm "Standard-Einstellungen für den Management-Container verwenden?\n\n  CTID: $CTID   Hostname: $HOSTNAME_CT\n  Disk: ${DISK}G   RAM: ${RAM}MB   Cores: $CORES\n  Storage: $ROOTFS_STORAGE   Netz: $BRIDGE (DHCP)\n  unprivilegiert: ja"; then
    return
  fi
  ask "Container-ID (CTID)" "$CTID";               CTID="$REPLY"
  ask "Hostname" "$HOSTNAME_CT";                   HOSTNAME_CT="$REPLY"
  ask "Disk-Größe in GB" "$DISK";                  DISK="$REPLY"
  ask "RAM in MB" "$RAM";                          RAM="$REPLY"
  ask "CPU-Kerne" "$CORES";                        CORES="$REPLY"
  ask "Rootfs-Storage" "$ROOTFS_STORAGE";          ROOTFS_STORAGE="$REPLY"
  ask "Netzwerk-Bridge" "$BRIDGE";                 BRIDGE="$REPLY"
  ask "IP (dhcp oder CIDR, z. B. 192.168.1.150/24)" "$IPCONF"; IPCONF="$REPLY"
  if [ "$IPCONF" != "dhcp" ]; then
    ask "Gateway" "";                              GATEWAY="$REPLY"
  fi
  [[ "$CTID" =~ ^[0-9]+$ ]] || fatal "CTID muss eine Zahl sein."
}

create_lxc() {
  pct status "$CTID" >/dev/null 2>&1 && fatal "CT $CTID existiert bereits."
  pick_template

  local net="name=eth0,bridge=$BRIDGE,ip=$IPCONF"
  [ -n "$GATEWAY" ] && net+=",gw=$GATEWAY"

  msg_info "Erstelle Container $CTID ($HOSTNAME_CT) …"
  pct create "$CTID" "$TEMPLATE" \
    --hostname "$HOSTNAME_CT" \
    --memory "$RAM" --cores "$CORES" \
    --rootfs "$ROOTFS_STORAGE:$DISK" \
    --net0 "$net" \
    --unprivileged 1 --features nesting=1 \
    --onboot 1 \
    --description "LXC Commander – Update-Dashboard (github.com/$REPO_OWNER/$REPO_NAME)" \
    >/dev/null
  pct start "$CTID"
  msg_ok "Container $CTID gestartet."

  msg_info "Warte auf Netzwerk im Container …"
  for _ in $(seq 1 30); do
    pct exec "$CTID" -- ping -c1 -W1 deb.debian.org >/dev/null 2>&1 && break
    sleep 2
  done

  msg_info "Installiere Pakete im Container …"
  pct exec "$CTID" -- bash -c \
    "apt-get update -qq && apt-get install -y -qq python3 python3-venv openssh-client curl ca-certificates >/dev/null"
  msg_ok "Pakete installiert."

  # Quellen in den Container bringen
  msg_info "Übertrage Anwendung in den Container …"
  local tar=/tmp/lxc-commander-src.tar.gz
  tar -czf "$tar" -C "$SRC_DIR" backend frontend config systemd requirements.txt install.sh
  pct push "$CTID" "$tar" /tmp/lxc-commander-src.tar.gz
  rm -f "$tar"
  pct exec "$CTID" -- bash -c \
    "mkdir -p $DEST $DATA_DIR /tmp/lxc-commander-src &&
     tar -xzf /tmp/lxc-commander-src.tar.gz -C /tmp/lxc-commander-src &&
     cp -r /tmp/lxc-commander-src/{backend,frontend,systemd} $DEST/ &&
     cp /tmp/lxc-commander-src/requirements.txt $DEST/ &&
     mkdir -p $DEST/config &&
     cp -n /tmp/lxc-commander-src/config/containers.yaml $DEST/config/ &&
     rm -rf /tmp/lxc-commander-src /tmp/lxc-commander-src.tar.gz"
  msg_ok "Anwendung übertragen."

  msg_info "Erstelle Python-Umgebung im Container …"
  pct exec "$CTID" -- bash -c \
    "python3 -m venv $DEST/venv &&
     $DEST/venv/bin/pip install -q --upgrade pip &&
     $DEST/venv/bin/pip install -q -r $DEST/requirements.txt"
  msg_ok "Python-Umgebung bereit."

  # --- SSH: Container -> Host (eingeschränkter Schlüssel) -------------------
  msg_info "Richte eingeschränkten SSH-Zugang zum Host ein …"
  local hip; hip="$(host_ip)"
  pct exec "$CTID" -- bash -c \
    "mkdir -p $DEST/keys &&
     [ -f $DEST/keys/id_ed25519 ] || ssh-keygen -q -t ed25519 -f $DEST/keys/id_ed25519 -N '' -C lxc-commander@$HOSTNAME_CT"
  local pubkey
  pubkey="$(pct exec "$CTID" -- cat "$DEST/keys/id_ed25519.pub")"

  # Wrapper auf dem Host: erlaubt nur pct-Subkommandos, die die App braucht
  cat > /usr/local/bin/lxc-commander-shell <<'WRAP'
#!/usr/bin/env bash
# Nur die von LXC Commander benötigten pct-Kommandos zulassen
set -euo pipefail
cmd="${SSH_ORIGINAL_COMMAND:-}"
case "$cmd" in
  "pct list"|"pct status "*|"pct exec "*|"pct reboot "*|"pct snapshot "*)
    exec bash -c "$cmd" ;;
  *)
    echo "lxc-commander-shell: Kommando nicht erlaubt: $cmd" >&2; exit 126 ;;
esac
WRAP
  chmod 755 /usr/local/bin/lxc-commander-shell

  mkdir -p /root/.ssh; chmod 700 /root/.ssh; touch /root/.ssh/authorized_keys
  if ! grep -qF "$pubkey" /root/.ssh/authorized_keys; then
    echo "command=\"/usr/local/bin/lxc-commander-shell\",restrict $pubkey" >> /root/.ssh/authorized_keys
  fi
  chmod 600 /root/.ssh/authorized_keys
  msg_ok "SSH-Zugang eingerichtet (nur pct list/status/exec/reboot/snapshot)."

  # --- Konfiguration + Token + Dienst ---------------------------------------
  msg_info "Konfiguriere Anwendung (mode: ssh, Host: $hip) …"
  TOKEN=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)
  pct exec "$CTID" -- bash -c \
    "sed -i -e 's/^  mode: local/  mode: ssh/' \
            -e 's/^    host: .*/    host: $hip/' \
            -e 's#^    key_file: .*#    key_file: $DEST/keys/id_ed25519#' \
            -e 's/CHANGE-ME-LONG-RANDOM-TOKEN/$TOKEN/' \
            $DEST/config/containers.yaml"

  # Host-Key einmalig akzeptieren (accept-new) + Verbindung testen
  pct exec "$CTID" -- bash -c \
    "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i $DEST/keys/id_ed25519 root@$hip 'pct list' >/dev/null" \
    || msg_error "SSH-Test fehlgeschlagen – bitte manuell prüfen (ssh root@$hip 'pct list')."

  pct exec "$CTID" -- bash -c \
    "cp $DEST/systemd/$SERVICE.service /etc/systemd/system/ &&
     systemctl daemon-reload && systemctl enable --now $SERVICE"
  msg_ok "Dienst im Container gestartet."

  CT_IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
}

# ------------------------------------------------------------------ Abschluss
summary() { # $1 = URL-Host
  echo
  echo -e " ${GN}════════════════════════════════════════════════════════════${CL}"
  echo -e "  $APP ist installiert."
  echo
  echo -e "  Dashboard :  ${BL}http://$1:$PORT${CL}"
  echo -e "  API-Token :  ${YW}$TOKEN${CL}"
  echo -e "               (wird beim ersten Öffnen des Dashboards abgefragt)"
  echo
  echo -e "  Konfiguration :  $DEST/config/containers.yaml"
  echo -e "  Logs          :  journalctl -u $SERVICE -f"
  echo -e "  Update        :  install.sh --update"
  echo
  echo -e "  Neue LXC-Container erscheinen automatisch im Dashboard und"
  echo -e "  können dort per »Integrieren« in die Konfiguration übernommen"
  echo -e "  werden (Kritikalität, Rolle, DNS-Gruppe, Backup-Erinnerung)."
  echo -e " ${GN}════════════════════════════════════════════════════════════${CL}"
}

# ------------------------------------------------------------------- Ablauf
main() {
  header
  require_root

  local action="${1:-}"
  case "$action" in
    --update)    update_app; exit 0 ;;
    --uninstall) uninstall_app; exit 0 ;;
    --host)      MODE=host ;;
    --lxc)       MODE=lxc ;;
    "" )         MODE="" ;;
    *) fatal "Unbekannte Option: $action (erlaubt: --host --lxc --update --uninstall)" ;;
  esac

  if ! on_pve_host; then
    # z. B. innerhalb eines Containers ausgeführt: nur die App installieren
    msg_info "Kein Proxmox-Host erkannt – installiere nur die Anwendung (mode: ssh)."
    if [ -d "$DEST/venv" ]; then update_app; exit 0; fi
    fetch_sources
    install_app ssh
    summary "$(hostname -I | awk '{print $1}')"
    echo -e "  ${YW}Hinweis:${CL} SSH-Zugang zum Proxmox-Host muss manuell eingerichtet"
    echo -e "  werden – siehe README, Abschnitt »Variante B«."
    exit 0
  fi

  # Bereits installiert? -> Update anbieten
  if [ -z "$MODE" ] && [ -d "$DEST/venv" ]; then
    if confirm "$APP ist auf diesem Host bereits installiert.\nJetzt aktualisieren?"; then
      update_app; exit 0
    fi
  fi

  if [ -z "$MODE" ]; then
    if have_whiptail; then
      MODE=$(whiptail --backtitle "$APP" --title "$APP – Installation" --menu \
        "Wo soll $APP installiert werden?" 14 72 2 \
        "lxc"  "Eigener Management-Container (empfohlen)" \
        "host" "Direkt auf diesem Proxmox-Host" \
        3>&1 1>&2 2>&3) || fatal "Abgebrochen."
    else
      ask "Installation: 'lxc' (eigener Container, empfohlen) oder 'host'" "lxc"
      MODE="$REPLY"
    fi
  fi

  fetch_sources
  case "$MODE" in
    host)
      install_app local
      summary "$(host_ip)"
      ;;
    lxc)
      lxc_settings
      create_lxc
      summary "${CT_IP:-<Container-IP>}"
      ;;
    *) fatal "Ungültiger Modus: $MODE" ;;
  esac
}

main "$@"
