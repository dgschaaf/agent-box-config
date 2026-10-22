#!/usr/bin/env bash
# bootstrap.sh — rebuild the agent box on a fresh Linux Mint 22.x (Ubuntu noble base).
#
# Run as your normal user (not root); it calls sudo where needed.
# Safe to run repeatedly: every step checks current state before changing it,
# and any file it would overwrite with different content is backed up first.
# Contains no secrets — logins and keys are manual steps, see README.md.
#
# Environment overrides:
#   NODE_MAJOR=24         Node.js major version to pin from NodeSource (current LTS line)
#   ARDUINO_CLI_VERSION=1.5.1  arduino-cli release to install into ~/.local/bin
#   WIFI_OFF=1           turn Wi-Fi off (only if a wired connection is up)
#   FORCE_WIFI_OFF=1      with WIFI_OFF=1, turn Wi-Fi off even with no wired connection

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NODE_MAJOR="${NODE_MAJOR:-24}"
ARDUINO_CLI_VERSION="${ARDUINO_CLI_VERSION:-1.5.1}"
WIFI_OFF="${WIFI_OFF:-0}"
FORCE_WIFI_OFF="${FORCE_WIFI_OFF:-0}"

log()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }

if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user, not root. It uses sudo where needed." >&2
    exit 1
fi

# Ask for the sudo password once up front.
sudo -v

# install_file DEST MODE USE_SUDO  (content on stdin)
# Writes DEST only if its content differs. An existing, different file is
# backed up to DEST.bak.<timestamp> first. Returns 0 if changed, 1 if not.
install_file() {
    local dest=$1 mode=$2 use_sudo=$3 tmp
    local -a run=()
    [[ $use_sudo == sudo ]] && run=(sudo)
    tmp=$(mktemp)
    cat > "$tmp"
    if "${run[@]}" test -f "$dest" && "${run[@]}" cmp -s "$tmp" "$dest"; then
        rm -f "$tmp"
        note "unchanged: $dest"
        return 1
    fi
    if "${run[@]}" test -f "$dest"; then
        local backup
        backup="$dest.bak.$(date +%Y%m%d%H%M%S)"
        "${run[@]}" cp -a "$dest" "$backup"
        note "backed up existing file to $backup"
    fi
    "${run[@]}" mkdir -p "$(dirname "$dest")"
    "${run[@]}" install -m "$mode" "$tmp" "$dest"
    rm -f "$tmp"
    note "wrote: $dest"
    return 0
}

# ---------------------------------------------------------------------------
# 1. Third-party apt repositories
#    - NodeSource (Node.js LTS), Microsoft (VS Code), git-core PPA (current git).
#    Each is added only if its sources file is missing.
# ---------------------------------------------------------------------------
log "Apt repositories"

sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl gpg

# NodeSource. Pinned to NODE_MAJOR rather than "lts" so reruns are predictable;
# bump NODE_MAJOR when the next LTS line lands.
if [[ ! -f /usr/share/keyrings/nodesource.gpg ]]; then
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | sudo gpg --dearmor -o /usr/share/keyrings/nodesource.gpg
fi
install_file /etc/apt/sources.list.d/nodesource.sources 0644 sudo <<EOF || true
Types: deb
URIs: https://deb.nodesource.com/node_${NODE_MAJOR}.x
Suites: nodistro
Components: main
Architectures: amd64
Signed-By: /usr/share/keyrings/nodesource.gpg
EOF
# Prefer NodeSource's nodejs over Ubuntu's older one.
install_file /etc/apt/preferences.d/nodejs 0644 sudo <<'EOF' || true
Package: nodejs
Pin: origin deb.nodesource.com
Pin-Priority: 600
EOF

# Microsoft VS Code. Same file name and format the VS Code package itself
# maintains, so the package won't add a duplicate entry.
if [[ ! -f /usr/share/keyrings/microsoft.gpg ]]; then
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | sudo gpg --dearmor -o /usr/share/keyrings/microsoft.gpg
fi
if [[ ! -f /etc/apt/sources.list.d/vscode.sources && ! -f /etc/apt/sources.list.d/vscode.list ]]; then
    install_file /etc/apt/sources.list.d/vscode.sources 0644 sudo <<'EOF' || true
Types: deb
URIs: https://packages.microsoft.com/repos/code
Suites: stable
Components: main
Architectures: amd64
Signed-By: /usr/share/keyrings/microsoft.gpg
EOF
else
    note "unchanged: VS Code repo already configured"
fi

# git-core PPA for an up-to-date git.
if ! compgen -G '/etc/apt/sources.list.d/git-core-ppa*' >/dev/null; then
    sudo add-apt-repository -y ppa:git-core/ppa
else
    note "unchanged: git-core PPA already configured"
fi

# ---------------------------------------------------------------------------
# 2. Apt packages
#    The packages installed on top of a stock Mint install. apt-get install is
#    a no-op for anything already present.
# ---------------------------------------------------------------------------
log "Apt packages"

PACKAGES=(
    git              # from git-core PPA
    gh               # GitHub CLI
    tmux
    curl
    build-essential
    python3-pip
    python3-venv
    openssh-server
    nodejs           # from NodeSource (includes npm)
    code             # VS Code, from Microsoft repo
    arduino
    timeshift        # ships with Mint; listed so it's guaranteed
)
sudo apt-get update -qq
sudo apt-get install -y "${PACKAGES[@]}"
note "node $(node --version), npm $(npm --version)"

# ---------------------------------------------------------------------------
# 3. Claude Code (native installer)
#    Installs to ~/.local/bin/claude and keeps itself updated, so skip if present.
# ---------------------------------------------------------------------------
log "Claude Code"

if [[ -x "$HOME/.local/bin/claude" ]] || command -v claude >/dev/null 2>&1; then
    note "already installed: $("$HOME/.local/bin/claude" --version 2>/dev/null || claude --version)"
else
    curl -fsSL https://claude.ai/install.sh | bash
fi
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) note "~/.local/bin is not on PATH in this shell; open a new terminal before running claude." ;;
esac

# ---------------------------------------------------------------------------
# 4. Sleep and lid
#    This box is always-on: never suspend/hibernate. Closing the lid only
#    blanks the screen, so it can run lid-shut without the panel staying lit.
# ---------------------------------------------------------------------------
log "Sleep and lid settings"

# Mask all sleep targets so nothing (logind, desktop, idle timer) can suspend.
for target in sleep.target suspend.target hibernate.target hybrid-sleep.target; do
    if [[ $(systemctl is-enabled "$target" 2>/dev/null || true) == masked ]]; then
        note "already masked: $target"
    else
        sudo systemctl mask "$target"
    fi
done

# logind: ignore the lid switch in every power state, so it never tries to
# suspend; blanking on lid close is Cinnamon's job (below). Takes effect after
# reboot (restarting logind would kill the desktop session).
if install_file /etc/systemd/logind.conf.d/lid.conf 0644 sudo <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
then
    note "lid setting takes effect after the next reboot"
fi

# Cinnamon power settings: never suspend, never blank on idle while the lid is
# open, and blank (not suspend) when the lid closes. Needs the desktop
# session's D-Bus; skipped over plain SSH.
P=org.cinnamon.settings-daemon.plugins.power
if [[ -n ${DBUS_SESSION_BUS_ADDRESS:-} ]] && command -v gsettings >/dev/null 2>&1 \
    && gsettings list-keys $P >/dev/null 2>&1; then
    gsettings set $P sleep-inactive-ac-timeout 0
    gsettings set $P sleep-inactive-battery-timeout 0
    gsettings set $P sleep-display-ac 0
    gsettings set $P lid-close-ac-action 'blank'
    gsettings set $P lid-close-battery-action 'blank'
    note "Cinnamon power settings applied"
else
    warn "no desktop session found; Cinnamon power settings skipped. Rerun from a terminal in the desktop to apply them."
fi

# ---------------------------------------------------------------------------
# 5. SSH: key-only authentication
#    Drop-in under sshd_config.d so the stock sshd_config stays untouched.
# ---------------------------------------------------------------------------
log "SSH server (keys only)"

sudo systemctl enable --now ssh

if [[ ! -s "$HOME/.ssh/authorized_keys" ]]; then
    warn "~/.ssh/authorized_keys is empty: after this, you can't SSH in until you add a key (see README)."
fi

if install_file /etc/ssh/sshd_config.d/10-keys-only.conf 0644 sudo <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
then
    # Validate before reloading so a bad config can't take sshd down.
    sudo sshd -t
    sudo systemctl reload ssh
    note "sshd reloaded"
fi

# ---------------------------------------------------------------------------
# 6. ~/.tmux.conf
# ---------------------------------------------------------------------------
log "tmux config"

install_file "$HOME/.tmux.conf" 0644 nosudo <<'EOF' || true
set -g mouse on
set -g history-limit 50000
EOF

# ---------------------------------------------------------------------------
# 7. VS Code Remote machine settings
#    Applies when this box is opened over Remote-SSH. Git auth is left to the
#    gh credential helper (set up by `gh auth setup-git`) instead of VS Code.
# ---------------------------------------------------------------------------
log "VS Code Remote machine settings"

install_file "$HOME/.vscode-server/data/Machine/settings.json" 0644 nosudo <<'EOF' || true
{
    "git.terminalAuthentication": false,
    "github.gitAuthentication": false
}
EOF

# ---------------------------------------------------------------------------
# 8. Wi-Fi off (opt-in)
#    The box runs on Wi-Fi for now, so this only runs with WIFI_OFF=1. Even
#    then it's skipped unless a wired connection is up, so a bootstrap over
#    Wi-Fi doesn't cut itself off. `nmcli radio wifi off` persists across reboots.
# ---------------------------------------------------------------------------
log "Wi-Fi"

if [[ $WIFI_OFF != 1 ]]; then
    note "left as is (set WIFI_OFF=1 to turn it off)"
elif ! command -v nmcli >/dev/null 2>&1; then
    warn "nmcli not found; Wi-Fi left as is."
elif [[ $(nmcli radio wifi) == disabled ]]; then
    note "already off"
elif nmcli -t -f TYPE,STATE device | grep -qx 'ethernet:connected' || [[ $FORCE_WIFI_OFF == 1 ]]; then
    nmcli radio wifi off
    note "turned off"
else
    warn "no wired connection is up, so Wi-Fi was left on to avoid losing the network."
    warn "Plug in Ethernet and rerun, or rerun with WIFI_OFF=1 FORCE_WIFI_OFF=1."
fi

# ---------------------------------------------------------------------------
# 9. arduino-cli
#    Official installer into ~/.local/bin, which is on PATH for every shell
#    (including non-interactive ones like Claude Code's), unlike Homebrew.
#    Then the board cores, and dialout group membership for the boards'
#    serial ports.
# ---------------------------------------------------------------------------
log "arduino-cli"

if command -v arduino-cli >/dev/null 2>&1 || [[ -x "$HOME/.local/bin/arduino-cli" ]]; then
    note "already installed: $("$HOME/.local/bin/arduino-cli" version 2>/dev/null || arduino-cli version)"
else
    mkdir -p "$HOME/.local/bin"
    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
        | BINDIR="$HOME/.local/bin" sh -s "$ARDUINO_CLI_VERSION"
fi
ARDUINO_CLI=$(command -v arduino-cli || echo "$HOME/.local/bin/arduino-cli")

# Board cores. core install is a no-op for a core that's already installed.
ARDUINO_CORES=(
    arduino:avr          # Uno, Nano, Mega
    arduino:mbed_nano    # Nano 33 BLE, Nano RP2040 Connect
)
"$ARDUINO_CLI" core update-index
for core in "${ARDUINO_CORES[@]}"; do
    if "$ARDUINO_CLI" core list | awk 'NR > 1 { print $1 }' | grep -qxF "$core"; then
        note "already installed: core $core"
    else
        "$ARDUINO_CLI" core install "$core"
    fi
done

if id -nG "$USER" | grep -qw dialout; then
    note "already in dialout group"
else
    sudo usermod -aG dialout "$USER"
    note "added $USER to dialout; log out and back in for serial port access"
fi

# ---------------------------------------------------------------------------
# 10. VS Code extensions
#     Installs anything in vscode-extensions.txt that isn't installed yet.
#     Extensions are never removed, and versions aren't pinned (VS Code
#     auto-updates them).
# ---------------------------------------------------------------------------
log "VS Code extensions"

installed_exts=$(code --list-extensions 2>/dev/null | tr '[:upper:]' '[:lower:]')
while read -r ext; do
    ext=${ext%%#*}
    ext=${ext//[[:space:]]/}
    [[ -z $ext ]] && continue
    if grep -qxF "${ext,,}" <<<"$installed_exts"; then
        note "already installed: $ext"
    elif ! code --install-extension "$ext" >/dev/null; then
        warn "failed to install VS Code extension: $ext"
    else
        note "installed: $ext"
    fi
done < "$SCRIPT_DIR/vscode-extensions.txt"

# ---------------------------------------------------------------------------
# 11. PlatformIO Core (pio CLI)
#     Official installer into ~/.platformio/penv. Its bin dir isn't on PATH in
#     non-interactive shells (headless Claude Code), so pio is linked into
#     ~/.local/bin, as arduino-cli is.
# ---------------------------------------------------------------------------
log "PlatformIO Core"

if [[ -x "$HOME/.platformio/penv/bin/pio" ]]; then
    note "already installed: $("$HOME/.platformio/penv/bin/pio" --version)"
else
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/get-platformio.py" \
        https://raw.githubusercontent.com/platformio/platformio-core-installer/master/get-platformio.py
    python3 "$tmp/get-platformio.py"
    rm -rf "$tmp"
fi
mkdir -p "$HOME/.local/bin"
for bin in pio platformio; do
    if [[ $(readlink "$HOME/.local/bin/$bin" 2>/dev/null) == "$HOME/.platformio/penv/bin/$bin" ]]; then
        note "already linked: ~/.local/bin/$bin"
    else
        ln -sf "$HOME/.platformio/penv/bin/$bin" "$HOME/.local/bin/$bin"
        note "linked: ~/.local/bin/$bin"
    fi
done

# ---------------------------------------------------------------------------
# 12. Claude Code box config
#     ~/.claude/CLAUDE.md: the box's operating rules (autonomous agent, PR-based).
#     ~/.claude/hooks/guard-main.py: blocks pushes to main and commits on main,
#     registered in ~/.claude/settings.json so it covers every repo.
# ---------------------------------------------------------------------------
log "Claude Code box config"

install_file "$HOME/.claude/CLAUDE.md" 0644 nosudo < "$SCRIPT_DIR/claude/CLAUDE.md" || true
install_file "$HOME/.claude/hooks/guard-main.py" 0755 nosudo < "$SCRIPT_DIR/claude/hooks/guard-main.py" || true
note "settings.json hook: $(python3 "$SCRIPT_DIR/claude/merge-settings.py")"

# ---------------------------------------------------------------------------
# 13. agent-runner
#     Works agent:ready GitHub issues with headless Claude in a tmux session,
#     as a systemd user service. Linger keeps it running with nobody logged in.
#     The config is only created if missing, so local edits survive reruns.
# ---------------------------------------------------------------------------
log "agent-runner"

if [[ $(readlink "$HOME/.local/bin/agent-runner" 2>/dev/null) == "$SCRIPT_DIR/runner/agent-runner.py" ]]; then
    note "already linked: ~/.local/bin/agent-runner"
else
    ln -sf "$SCRIPT_DIR/runner/agent-runner.py" "$HOME/.local/bin/agent-runner"
    note "linked: ~/.local/bin/agent-runner"
fi
if [[ -f "$HOME/.config/agent-runner/config.json" ]]; then
    note "unchanged: ~/.config/agent-runner/config.json (exists; not overwritten)"
else
    install_file "$HOME/.config/agent-runner/config.json" 0644 nosudo \
        < "$SCRIPT_DIR/runner/config.example.json" || true
fi
if install_file "$HOME/.config/systemd/user/agent-runner.service" 0644 nosudo \
    < "$SCRIPT_DIR/runner/agent-runner.service"; then
    systemctl --user daemon-reload
fi
if [[ $(loginctl show-user "$USER" -p Linger --value 2>/dev/null) == yes ]]; then
    note "already enabled: linger for $USER"
else
    sudo loginctl enable-linger "$USER"
fi
note "the service is installed but not enabled; start it once Claude and gh are logged in:"
note "  systemctl --user enable --now agent-runner"

log "Done. Remaining manual steps are in README.md."
