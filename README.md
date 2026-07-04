# LXC Commander – Proxmox LXC Manager

Selbst gehostetes Update-Dashboard für Proxmox-VE-LXC-Container – mit geführtem
Update-Wizard, Live-Terminal-Ausgabe, Kritikalitäts-Klassifizierung,
DNS-Schutzregeln und automatischer Erkennung neuer Container.

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser (Vue 3, Dark UI)                                       │
│    Dashboard  ·  Update-Wizard  ·  Live-Terminal  ·  Verlauf    │
└───────────────▲───────────────────────────▲─────────────────────┘
                │ REST (Bearer-Token)       │ WebSocket (Live-Logs)
┌───────────────┴───────────────────────────┴─────────────────────┐
│  Backend: FastAPI (Python 3.11+)                                │
│   • WizardManager  (Schritte, Pause, Bestätigungen)             │
│   • Safety-Locks   (pro CT · exclusive_groups · Critical-Sem.)  │
│   • Proxmox-Wrapper (LocalRunner / SSHRunner)                   │
│   • SQLite-Store   (Jobs, Logs, Update-Historie)                │
└───────────────┬─────────────────────────────────────────────────┘
                │ pct list / exec / reboot / snapshot
┌───────────────▼─────────────────────────────────────────────────┐
│  Proxmox VE Host  →  alle LXC-Container (automatisch erkannt)   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 1. Installation (Einzeiler, Community-Script-Stil)

Auf dem **Proxmox-VE-Host** als `root` ausführen:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/LarsMT35/proxmox-lxc-manager/main/install.sh)
```

Der Installer fragt interaktiv (Whiptail-Dialoge) nach dem Installationsziel:

| Modus | Beschreibung |
|---|---|
| **Eigener Management-LXC** (empfohlen) | Erstellt automatisch einen unprivilegierten Debian-12-Container (Standard: 1 Core, 512 MB RAM, 4 GB Disk, DHCP – alles anpassbar), installiert die App darin und richtet einen **eingeschränkten SSH-Zugang** zum Host ein (nur `pct list/status/exec/reboot/snapshot`). |
| **Direkt auf dem Host** | Installiert die App nach `/opt/lxc-commander`, `pct` wird lokal ausgeführt. |

Festlegungen zum LXC (CTID, Hostname, Disk, RAM, Cores, Storage, Bridge,
DHCP/statische IP) können über **„Erweiterte Einstellungen"** im Dialog
angepasst werden; mit **Standard-Einstellungen** läuft die Installation ohne
weitere Rückfragen durch.

Am Ende zeigt der Installer die Dashboard-URL (`http://<ip>:8420`) und den
generierten **API-Token** an – diesen beim ersten Öffnen des Dashboards
eingeben.

### Nicht-interaktiv / weitere Modi

```bash
install.sh --lxc         # direkt Variante "eigener Container"
install.sh --host        # direkt Variante "auf dem Host"
install.sh --update      # bestehende Installation aktualisieren
install.sh --uninstall   # deinstallieren (Config + Datenbank bleiben erhalten)
```

Ein erneuter Aufruf des Einzeilers auf einem bereits installierten System
bietet automatisch ein **Update** an – `config/containers.yaml` und die
SQLite-Datenbank werden dabei nie überschrieben.

### Betrieb / nützliche Befehle

```bash
systemctl status lxc-commander        # Dienststatus
journalctl -u lxc-commander -f        # Live-Logs des Backends
systemctl restart lxc-commander       # nach Config-Änderungen
```

Datenbank (Logs/Historie): `/var/lib/lxc-commander/commander.db`
Konfiguration: `/opt/lxc-commander/config/containers.yaml`

---

## 2. Umgang mit neuen Containern

Es muss **nichts** vorkonfiguriert werden:

1. **Automatische Erkennung** – jeder Container aus `pct list` erscheint
   sofort im Dashboard (mit echtem Hostnamen), auch wenn er nicht in
   `containers.yaml` steht. Er wird mit Kritikalität `normal` behandelt und
   mit dem Tag **„neu"** markiert; Update-Check und Update-Wizard
   funktionieren sofort.
2. **Integrieren per Klick** – über den Button **„Integrieren"** auf der
   Container-Karte werden Name, Rolle, Kritikalität, Exklusiv-Gruppe,
   Validierungs-Befehl und Backup-Erinnerung festgelegt und **dauerhaft in
   `containers.yaml` gespeichert** (Kommentare in der Datei bleiben
   erhalten). Kein Dienst-Neustart nötig.
3. **Safe-Mode-Plan** – „Update all (Safe Mode)" bezieht neu erkannte
   (laufende) Container automatisch als unkritische Stufe-1-Container ein.
4. Alternativ weiterhin manuell: Eintrag in `containers.yaml` ergänzen und
   `systemctl restart lxc-commander`.

---

## 3. Architektur

**Backend (Python / FastAPI)**

| Datei | Aufgabe |
|---|---|
| `backend/main.py` | REST-API, WebSocket, Auth, statisches Frontend |
| `backend/proxmox.py` | `pct`-Wrapper (lokal oder per SSH), Streaming-Exec, Update-Erkennung |
| `backend/wizard.py` | Schritt-Engine: Bestätigungen, Pause/Resume, Locks, Sonderregeln |
| `backend/store.py` | SQLite: Jobs, zeilenweise Logs, Update-Historie |
| `backend/config.py` | YAML-Konfiguration, Container-Metadaten, Adopt-Persistenz |

**Frontend (`frontend/index.html`)** – Vue 3 als Single-File-App ohne
Build-Schritt (bewusste Entscheidung für Homelab-Wartbarkeit): Container-Grid
mit Ampel-Status, „Integrieren"-Dialog für neue Container, Wizard-Modal mit
Schritt-Leiste + Terminal, Safe-Mode-Plan, Verlauf mit Log-Ansicht.

**Sicherheitsmodell (Kernstück)**

1. **Pro-Container-Lock** – nie zwei Jobs auf demselben CT.
2. **`exclusive_groups`** – z. B. AdGuard und Unbound teilen sich die Gruppe
   `dns` und damit einen Lock: sie können **nie gleichzeitig** aktualisiert
   oder neugestartet werden.
3. **Critical-Semaphore** – maximal 1 kritischer Container gleichzeitig.
4. **Bestätigungs-Schritte** – jeder zustandsändernde Befehl (`full-upgrade`,
   `autoremove`, `reboot`, `snapshot`) zeigt zuerst den exakten Befehl an und
   wartet auf Klick. Container mit `backup_reminder`-Flag verlangen zusätzlich
   eine Backup-Checkbox; mit `validate_command` (z. B. `nginx -t`) wird nach
   dem Update automatisch die Konfiguration geprüft.
5. **Lückenlose Protokollierung** – jede Zeile jedes Befehls landet in SQLite.

**Wizard-Ablauf (pro Container)**

```
01 System-Status prüfen
02 Snapshot erstellen (nur kritische CTs, optional, Checkbox)
03 apt-get update
04 apt list --upgradable
05 apt-get full-upgrade -y        ← Bestätigung (+ optionale Backup-Checkbox)
06 apt-get autoremove -y          ← Bestätigung
07 Konfiguration validieren       (nur mit validate_command, z. B. nginx -t)
08 pct reboot                     ← Bestätigung, optional
09 Abschluss-Verifikation         (OS-Version + verbleibende Updates)
```

**„Update all (Safe Mode)"** erzeugt einen gestaffelten Plan: Stufe 1 = alle
unkritischen Container (sequenziell, inkl. neu erkannter CTs), danach jeder
kritische Container in einer **eigenen** Stufe – Container einer
Exklusiv-Gruppe landen dadurch nie in derselben Stufe. Ausgeführt wird jede
Stufe weiterhin über den Wizard inkl. aller Bestätigungen (bewusst kein
„Feuer-und-vergessen").

---

## 4. REST-API

Alle Endpunkte erwarten `Authorization: Bearer <token>`.

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/containers` | Inventar + Status + Ressourcen + Update-Cache (`configured`-Flag) |
| POST | `/api/containers/{id}/check` | `apt update` + upgradbare Pakete (inkl. Security-Zählung) |
| POST | `/api/containers/{id}/snapshot` | `pct snapshot` |
| POST | `/api/containers/{id}/wizard` | Update-Wizard starten (409 bei Lock-Konflikt) |
| POST | `/api/containers/{id}/adopt` | neu erkannten Container dauerhaft konfigurieren |
| POST | `/api/update-all-safe` | gestaffelten Safe-Mode-Plan berechnen |
| GET | `/api/jobs/{job}` / `/api/jobs/{job}/log` | Job-Status / persistiertes Log |
| GET | `/api/containers/{id}/history` | Update-Historie |
| WS | `/ws/jobs/{job}?token=…` | Live-Ausgabe + Steuerung (`confirm/pause/resume/abort`) |

---

## 5. Manuelle Installation (ohne Installer)

<details>
<summary>Variante A – direkt auf dem Proxmox-Host</summary>

```bash
git clone https://github.com/LarsMT35/proxmox-lxc-manager.git
cd proxmox-lxc-manager
./install.sh --host
```
</details>

<details>
<summary>Variante B – eigener Management-LXC, manuell</summary>

```bash
# 1) Management-Container auf dem Proxmox-Host anlegen
pct create 150 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname lxc-commander --memory 512 --cores 1 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp --unprivileged 1 --features nesting=1
pct start 150

# 2) In den Container, Projekt installieren
pct enter 150
apt update && apt install -y git curl
git clone https://github.com/LarsMT35/proxmox-lxc-manager.git
cd proxmox-lxc-manager && ./install.sh     # erkennt: kein PVE-Host → mode: ssh

# 3) SSH-Key für Host-Zugriff erzeugen
mkdir -p /opt/lxc-commander/keys
ssh-keygen -t ed25519 -f /opt/lxc-commander/keys/id_ed25519 -N ""
# Public Key auf dem Proxmox-Host in /root/.ssh/authorized_keys eintragen –
# idealerweise eingeschränkt per command=-Wrapper (der automatische Installer
# legt dafür /usr/local/bin/lxc-commander-shell an).

# 4) Konfiguration prüfen: /opt/lxc-commander/config/containers.yaml
#      proxmox.mode: ssh, ssh.host: <IP des Proxmox-Hosts>
systemctl restart lxc-commander
```
</details>

### Hinter NGINX als Reverse Proxy

```nginx
server {
    listen 443 ssl;
    server_name updates.home.lan;

    location / {
        proxy_pass http://<backend-ip>:8420;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;   # WebSocket!
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;                 # lange Upgrade-Läufe
    }
}
```

---

## 6. Sicherheitsüberlegungen

- **Kein Shell-Injection-Pfad:** Alle Befehle werden als argv-Listen gebaut;
  CT-IDs werden als Integer validiert, Snapshot-Namen per Regex. In den
  Containern laufen ausschließlich **fest definierte** App-Befehle – es gibt
  bewusst keinen „freien Terminal"-Endpunkt.
- **Auth:** Bearer-Token für REST und WebSocket. Der Installer generiert einen
  zufälligen 40-Zeichen-Token. Für den Fernzugriff zusätzlich hinter NGINX mit
  TLS + BasicAuth/SSO legen; Port 8420 per Firewall auf das LAN beschränken.
- **Least Privilege (LXC-Variante):** Der Installer trägt den SSH-Key auf dem
  Host mit `command="/usr/local/bin/lxc-commander-shell",restrict` ein – der
  Wrapper erlaubt nur `pct list`, `pct status`, `pct exec`, `pct reboot`,
  `pct snapshot`.
- **Kritische Infrastruktur:** Exklusiv-Gruppen mit gegenseitigem Ausschluss,
  Critical-Semaphore (max. 1), Backup-Checkbox, Konfig-Validierung – alles
  serverseitig erzwungen, nicht nur in der UI.
- **Nachvollziehbarkeit:** Jeder ausgeführte Befehl wird vor der Ausführung
  angezeigt und vollständig (zeilenweise, mit Zeitstempel) in SQLite
  protokolliert.
- **Selbst-Update-Falle:** Wenn die App hinter einem NGINX-Container läuft und
  dieser rebootet wird, bricht die UI-Verbindung ab – der Job läuft
  serverseitig weiter; Dashboard direkt über `:8420` erneut öffnen.
- **Nicht exponieren:** Diese App gehört ins LAN/VPN (WireGuard/Tailscale),
  niemals direkt ins Internet – sie kann Container rebooten.

---

## Lizenz

[MIT](LICENSE)
