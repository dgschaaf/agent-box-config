# agent-box-config

Rebuilds the agent box (Linux Mint 22.x) from a fresh install.

## Run

```bash
git clone https://github.com/dgschaaf/agent-box-config.git
cd agent-box-config
./bootstrap.sh
```

Run it as your normal user from a terminal in the Cinnamon desktop. It uses `sudo` itself. Rerunning it is safe: it skips steps that are already done, and it backs up any file it replaces to `<file>.bak.<timestamp>`.

What it sets up:

- Apt repos: NodeSource (Node.js 24 LTS; override with `NODE_MAJOR=`), Microsoft (VS Code) and the git-core PPA
- Apt packages: git, gh, tmux, curl, build-essential, python3-pip, python3-venv, openssh-server, nodejs, code, arduino and timeshift
- Claude Code through the native installer (`~/.local/bin/claude`)
- Sleep and lid: all sleep targets masked, logind ignores the lid and Cinnamon never suspends or blanks the screen on AC
- SSH: the server is enabled and accepts keys only (`/etc/ssh/sshd_config.d/10-keys-only.conf`)
- `~/.tmux.conf`: mouse on and 50k lines of scrollback
- VS Code Remote machine settings: VS Code's own git auth is turned off, so git uses the gh credential helper
- arduino-cli in `~/.local/bin` (version pinned with `ARDUINO_CLI_VERSION=`, default 1.5.1) with the `arduino:avr` and `arduino:mbed_nano` board cores, and your user added to the `dialout` group for serial port access (log out and back in afterwards)
- VS Code extensions listed in `vscode-extensions.txt`. Anything missing gets installed; nothing gets removed. To refresh the list from the current box, run `code --list-extensions > vscode-extensions.txt`
- Wi-Fi off, but only if you ask for it. The box is on Wi-Fi for now, so the script leaves Wi-Fi alone by default. Run it with `WIFI_OFF=1` when the box moves to Ethernet. Even then it skips this step unless a wired connection is up. Adding `FORCE_WIFI_OFF=1` turns Wi-Fi off anyway.

The lid setting takes effect after a reboot. The Cinnamon power settings are skipped over plain SSH, so rerun the script from the desktop to apply them.

## Manual steps after bootstrap

The script holds no secrets, so these steps are manual.

### 1. Log in to Claude Code

```bash
claude        # opens a browser to log in; or run /login inside claude
```

### 2. Log in to GitHub as the bot

The agent works as **`dschaaf-bot`**, not your personal account. The bot can't push to `main`: the `protect-main` ruleset requires a pull request, and `dgschaaf` has to approve it.

```bash
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git     # makes git use gh for github.com credentials
gh auth status        # should show dschaaf-bot as the active account
```

### 3. Set the git name and email (the bot's)

```bash
git config --global user.name  "dschaaf-bot"
git config --global user.email "dschaaf.admin@gmail.com"
```

### 4. Pi deploy key

Create a dedicated key for deploying to the Pi. Don't reuse your personal key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/pi_deploy -C "agent-box -> pi deploy"
```

Add `~/.ssh/pi_deploy.pub` to the Pi. Either append it to the deploy user's `~/.ssh/authorized_keys`, or add it as a deploy key on the Pi's GitHub repo, depending on how the Pi deploy works. Then add a host entry to `~/.ssh/config`:

```
Host pi
    HostName <pi-hostname-or-ip>
    User <deploy-user>
    IdentityFile ~/.ssh/pi_deploy
    IdentitiesOnly yes
```

Test it with `ssh pi true`.

Also make sure your workstation's public key is in **this** box's `~/.ssh/authorized_keys`. Password login is off, so without it you can't SSH in.

### 5. Timeshift

Open **Timeshift** from the menu and match the previous setup:

- Snapshot type: **RSYNC**
- Location: the backup drive
- Schedule: **none** (take snapshots by hand before risky changes)
- Users: include `/home/<you>/**`, but exclude `/home/<you>/projects/**`, since those live in git
- Turn on the option to stop cron emails

Take a first snapshot once all the steps above are done:

```bash
sudo timeshift --create --comments "post-bootstrap"
```
