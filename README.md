# Eta OS

**ETA OS** is a minimal, offline-first NixOS server distribution with a Python-based
management and monitoring layer.

- **Version**: 1.0.0
- **Upstream**: NixOS 26.05
- **Creator**: mohamedboukerch55
- **License**: MIT

> Minimal. Offline. Reliable. Controlled.

Eta OS targets small servers and appliances that must be self-contained: no cloud
dependency, no external substituters, no unnecessary services. The entire system can be
installed and run with zero network access after the ISO is built.

## Layout

| Path | Purpose |
| --- | --- |
| `flake.nix` | Flake outputs: `nixosConfigurations.eta` (server), `nixosConfigurations.eta-installer` (ISO), packages and checks |
| `configuration.nix` | Top-level NixOS configuration for the target system |
| `eta-local.nix` | Per-install settings, rewritten by the installer |
| `hardware-configuration.nix` | Placeholder for the ISO target closure, replaced at install time |
| `eta/` | Python management/monitoring layer (the `eta-os` package) |
| `modules/` | NixOS modules (`base`, `boot`, `networking`, `security`, `logging`, `database`, `backend`, `backup`, `monitoring`, `admin`, `installer`) |
| `profiles/server.nix` | Server profile: terminal only, no desktop, boot display on |
| `tests/` | pytest suite for the Python layer |
| `package.nix` | `buildPythonApplication` packaging (offline, wrapped) |
| `pyproject.toml` | Python packaging metadata and console scripts |

## Components (Python layer, `eta/`)

All privileged actions funnel through a single helper; nothing else runs as root.

| Binary | Module | Role |
| --- | --- | --- |
| `eta-admin` | `eta.admin` | Master administration CLI (guided, authenticated) |
| `eta-priv` | `eta.priv` | The one privileged helper (sudo NOPASSWD for `eta-admin` group only) |
| `eta-backend` | `eta.backend` | Unprivileged, dependency-free HTTP backend (loopback by default) |
| `eta-monitor` | `eta.monitor` | Health monitor and bounded recovery supervisor |
| `eta-boot` | `eta.boot` | Terminal boot display with recovery status |
| `eta-backup` | `eta.backup` | Scheduled restic backups with bounded resources |
| `eta-health` | `eta.health` | Health check entry points |
| `eta-maintenance` | `eta.maintenance` | Logged maintenance tasks (optionally authenticated) |
| `eta-install` | `eta.install` | Offline installer (ISO) |
| `eta-db-init` | `eta.database` | Database initialization (SQLite default, optional MariaDB) |
| `eta-settings-apply` | `eta.settings_apply` | Applies `/etc/eta/settings.json` at boot |
| `eta-security-check` | `eta.security_check` | Permission and account audit |
| `eta-log-rotate` | `eta.logrotate` | JSONL log rotation |

## NixOS modules

- **base** – Eta OS options/identity, `defaults.json`, users/groups
  (`eta-backend`, `eta-admin`, `eta-log`, master user `eta-master`), tmpfiles,
  no auto-update, no desktop, `eta.installerMode` toggle.
- **boot** – Terminal boot experience, `eta.target`, `eta-boot.service`, serial console support.
- **networking** – DHCP by default, firewall with SSH closed (explicit opt-in) and backend
  LAN exposure off by default. SSH only when enabled, key-only, master user only.
- **security** – `eta-admin` PAM service, sudo NOPASSWD scoped to `eta-priv`, audit service,
  root account locked.
- **logging** – Bounded journald + JSONL rotation timer.
- **database** – SQLite by default; optional low-memory MariaDB (`eta.database.engine = "mariadb"`).
- **backend** – Hardened `eta-backend.service` (no network exposure, `ProtectSystem=strict`,
  `MemoryMax`, watchdog).
- **backup** – Scheduled restic backups (`eta-backup.timer`), CPU/IO/memory bounded,
  exclusion set for repositories and restore roots.
- **monitoring** – `eta-monitor.service` with watchdog and bounded recovery
  (`monitor.max_attempts` inside `monitor.attempt_window`).
- **admin** – `admin.json` and interactive shell banner for the master account.
- **installer** – Live ISO assembly: bundles the source tree at `/etc/eta/source` and the
  prebuilt target closure at `/etc/eta/target-system`.

## Configuration layers (Python layer)

Lowest to highest priority:

1. Built-in defaults (`eta/config.py`)
2. `/etc/eta/defaults.json` (written by the NixOS module)
3. `/etc/eta/settings.json` (written by installer/administrator)
4. `ETA_SETTINGS` (explicit file) or environment variables (`ETA_BACKEND_PORT`, ...)

The configuration is validated on load; `load_settings(strict=False)` falls back to built-in
defaults on invalid input. Passwords never appear in any of these files: authentication uses
PAM/shadow, the restic password lives in a root-only key file
(`/var/lib/eta/restic.key`), and MariaDB credentials live in option files.

## Build and test

Python layer (no Nix required):

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest                      # unit tests (offline)
pytest -m integration       # integration tests (needs restic/network tools)
```

Package the Python layer with Nix:

```sh
nix build .#eta             # or: nix flake check
```

Build the server and the installer ISO:

```sh
nix build .#eta                     # python package
nix build '.#nixosConfigurations.eta.config.system.build.toplevel'
nix build .#iso                     # offline installer ISO
nix flake show
```

Run the development shell (Python, pytest, rich, restic, sqlite, shellcheck):

```sh
nix develop
```

## Build the installer ISO (step by step)

Requirements: a Linux x86_64 machine (or VM) with at least 8 GB RAM, about 30 GB free
disk, and Nix with flakes enabled.

1. Install Nix (multi-user recommended) and enable flakes:

   ```sh
   sh <(curl -L https://nixos.org/nix/install) --daemon
   mkdir -p ~/.config/nix
   echo 'experimental-features = nix-command flakes' >> ~/.config/nix/nix.conf
   ```

2. Get the source and enter it:

   ```sh
   unzip eta-os-main.zip && cd eta-os-main
   ```

3. Check that the flake evaluates before the long build:

   ```sh
   nix flake show
   nix eval .#nixosConfigurations.eta.config.system.build.toplevel.drvPath
   ```

4. Build the Python package and its tests (fast, catches packaging errors):

   ```sh
   nix build .#eta -L
   ```

5. Build the ISO:

   ```sh
   nix build .#iso -L
   ls -lh result/iso/
   ```

   The file is `result/iso/eta-os-1.0.0-x86_64-linux.iso`. Checksum it with
   `sha256sum result/iso/*.iso`.

6. Test it in a VM before flashing real hardware:

   ```sh
   qemu-system-x86_64 -enable-kvm -m 2048 -cdrom result/iso/eta-os-1.0.0-x86_64-linux.iso
   ```

   Or write it to a USB stick (replace `/dev/sdX`, this erases the drive):
   `sudo dd if=result/iso/eta-os-1.0.0-x86_64-linux.iso of=/dev/sdX bs=4M conv=fsync status=progress`

### Building the ISO in GitHub Actions instead

Push a tag such as `v1.0.0`, or run the `build-iso` workflow manually from the Actions tab.
The ISO and `SHA256SUMS` are attached to the Release. Release assets are limited to 2 GB;
if the ISO is larger, the workflow will fail at upload.

### Troubleshooting

- `experimental-features` error: add `--extra-experimental-features 'nix-command flakes'`
  or edit `nix.conf` as in step 1.
- `attribute ... missing` or option errors: the NixOS 26.05 channel has renamed options.
  Run `nix flake update` only if you intend to move to a newer nixpkgs, then re-test,
  because `flake.lock` pins the tested revision.
- Test failure during `nix build .#eta`: run `nix build .#eta -L` and read the pytest
  output. Integration tests are disabled in the Nix build by design.
- Out of memory or disk: the ISO closure is large. Add swap, or free disk space, and
  remove old results with `nix-collect-garbage -d`.
- The ISO is offline by design. Building it still needs network access to
  `cache.nixos.org` for the first build, since nothing is cached locally yet.

## Installing

1. Download/build `eta-os-<version>-x86_64-linux.iso`.
2. Boot it. The `eta-install` command is available as root on tty1.
3. Follow the prompts (or `eta-install --auto --confirm-erase /dev/sdX --disk /dev/sdX ...`).
4. Installed system logs in as `eta-master`; administration via `eta-admin`.

The installer is fully offline: it builds from the bundled flake source
(`path:/tmp/eta-install-src#eta`) with substituters disabled, and falls back to the baked
target closure if the offline build is impossible.

## Security model

- Root is locked; administration is via the single master account and `eta-priv`.
- `eta-priv` is executable by the `eta-admin` group only (sudo NOPASSWD, no other commands).
- Backend runs as `eta-backend` with `NoNewPrivileges`, `ProtectSystem=strict`, empty
  capability set, loopback bind, and an opt-in LAN firewall rule.
- Bounded recovery: at most `max_attempts` restarts inside `attempt_window`, exponential
  backoff capped by `backoff_max`, then the service is parked until an administrator
  intervenes or the window expires. Corrupt SQLite databases are never silently restored.
- Audit: `eta-security-check` (accounts, groups, permissions) and `eta-admin` audit views.

## Releases & CI

GitHub Actions workflow `.github/workflows/iso.yml` builds the offline installer ISO on every
tag `v*` (or via manual dispatch) and attaches `eta-os-<version>-x86_64-linux.iso` plus
`SHA256SUMS` to the GitHub Release:

1. Installs Nix (`cachix/install-nix-action`).
2. `nix build .#iso`
3. Creates/updates the release tag and uploads the ISO + checksums.

Releases are published automatically for every `v*` tag on this repository, e.g. `v1.0.0`.

## Identity

Every interactive surface shows `Created by mohamedboukerch55`. This attribution is part of
the project identity and must not be removed.
