# Eta OS Project Progress

## Project Overview
Eta OS is a minimal, offline-first NixOS server distribution with a Python-based
management and monitoring layer.

- **Creator**: mohamedboukerch55
- **Author identity**: Created by mohamedboukerch55
- **Version**: 1.0.0
- **Upstream**: NixOS 26.05 (locked: `nixos-26.05` @ `ac588ef1`)
- **License**: MIT

## Progress Log

### 2026-11-10 17:38:00 - Started Project Completion
- Explored project structure: NixOS flake + `eta-os` Python package (management/monitoring layer).
- Key components:
  - Flake configuration (`flake.nix`), `package.nix`, `pyproject.toml`
  - Python modules in `eta/` directory (21 modules)
  - NixOS modules in `modules/` directory (11 modules, installer added)
  - Server profile (`profiles/server.nix`)

### 2026-11-10 17:55:00 - Python layer: build & unit tests
- Fixed test pollution: `tests/test_config.py` mutated module-global `DEFAULT_SETTINGS`
  via shallow copies (`dict(DEFAULT_SETTINGS)`); now uses `copy.deepcopy`.
- `eta.config.Settings.__init__` now deep-copies input to avoid cross-call mutation.
- Unit tests: **69/69 passing** with `.venv` (Python 3.14.7). Integration marker unused/OK.
- Wheel builds clean via `pip wheel --no-build-isolation` (license field modernized
  to SPDX string for setuptools>=77).
- Smoke-tested all 13 console scripts from a scratch venv (`eta-priv` refuses non-root by design).

### 2026-11-10 17:56:00 - Documentation
- Wrote missing `README.md` (build, test, security model, layout) referenced by `pyproject.toml`.
- Identity line `Created by mohamedboukerch55` preserved on every interactive surface.

### 2026-11-10 17:58:00 - Flake completeness
- `flake.nix` referenced a missing `modules/installer.nix` → created the installer ISO module
  (live image autologin root on tty1, `/etc/eta/source` flake tree + `/etc/eta/target-system`
  baked closure, substituters emptied, boot tooling from `installation-cd-minimal`).
- Locked flake inputs committed (`flake.lock` → `nixpkgs` @ `ac588ef1`, nixos-26.05, 2026-11-08).

### 2026-11-10 18:10:00 - Nix fixes validated against nixpkgs source
- `system.nixos.distroCodename` / `system.nixos.distroVersion` do not exist →
  replaced with `distroName`/`distroId`/`system.nixos.label` (in `modules/base.nix`).
- `python3.pkgs.pam` renamed → `python-pam` in `package.nix`.
- `pytestFlagsArray` deprecated/hard-error → `disabledTestMarks = [ "integration" ]`.
- Verified `.#nixosConfigurations.eta.config.system.build.toplevel.drvPath` **evaluates**
  under nix 2.35.6 (container) after fixes.
- `eta-installer` ISO: fixed `networkmanager`/`wireless`/`iwd` and `getty.autologinUser`
  collisions with `mkForce` (installation-cd-minimal profile wins by default). Local
  evaluation of the ISO was interrupted at user request; **CI will build the ISO**, which
  is the real check.

### 2026-11-10 18:14:00 - GitHub
- Repository created: `mohamedboukerche22/eta-os` (public).
- Added `.github/workflows/iso.yml`: on tag push `v*` (and manual dispatch) it builds
  `.#iso` on GitHub Actions (ubuntu-latest + install-nix-action) and attaches the ISO
  plus `SHA256SUMS` to the GitHub Release.

---

## Build & Test Status
- [x] Python package build (`pip wheel`) — OK, `eta_os-1.0.0-py3-none-any.whl`
- [x] Unit tests pass — 69/69 (`python -m pytest`)
- [x] Smoke test console scripts — OK (`eta-priv` requires root by design)
- [x] `nix flake lock` committed
- [x] `nixosConfigurations.eta` drvPath evaluates (server closure)
- [ ] ISO build verified — deferred to GitHub Actions (no `/nix` locally)
- [ ] NixOS VM/integration tests — none written (candidates: installer, services)

## Known Gaps / Next Steps
1. Wait for Actions run for `v1.0.0` and confirm the ISO attach to the release.
2. ISO build on 2-core GitHub runners fetches from cache.nixos.org; keep build simple
   (no broken services) and watch job time (< 6h limit).
3. Release asset limit is 2 GB — if the ISO exceeds it, split the release (torrent/parts)
   or reduce closure (serverless `checks.eta-python` only).
4. Consider `nix flake check` (builds toplevel) in CI on `main`, not only on tags.
5. PAM dependency `python-pam` on target system is provided by flake-overridden build;
   installer/cache substitutes still needed for offline target installs.
6. No integration tests yet (`restic` health paths); `eta-health` is a stub for now.
