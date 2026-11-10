"""Eta OS offline installer.

The ISO contains a prebuilt target system closure *and* the complete flake source
in the Nix store. The installer prefers a declarative offline build from the
bundled source; if that is impossible it falls back to the baked closure. Nothing
is downloaded.

A disk is never touched without an explicit confirmation (typing the disk path,
or ``--confirm-erase DISK`` in automated mode).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import BANNER, CREATOR, IDENTITY_LINE, PRODUCT, UPSTREAM, __version__
from .config import DEFAULT_SETTINGS
from .logger import EtaLogger
from .util import CommandResult, EtaError, atomic_write, atomic_write_json, check_hostname, human_bytes, run, utc_iso

FLAKE_SOURCE_DEFAULT = "/etc/eta/source"
TARGET_SYSTEM_FILE = "/etc/eta/target-system"
MOUNT = Path("/mnt")
MIN_DISK_BYTES = 8 * 1024**3
COMMON_PASSWORDS = {
    "password", "password123", "admin", "administrator", "etaos", "eta-os",
    "changeme", "letmein", "qwerty", "123456789012", "nixos",
}
REQUIRED_COMMANDS = (
    "parted", "mkfs.fat", "mkfs.ext4", "mkswap", "mount", "umount", "blkid",
    "nixos-generate-config", "nixos-install", "nixos-enter", "chpasswd",
)


class InstallError(EtaError):
    pass


@dataclass
class InstallConfig:
    disk: str
    hostname: str
    password: str = field(repr=False, default="")
    master_user: str = "eta-master"
    network_mode: str = "dhcp"
    address: str = ""
    gateway: str = ""
    dns: str = ""
    interface: str = ""
    database_engine: str = "sqlite"
    backend_enabled: bool = True
    backup_enabled: bool = True
    ssh_enabled: bool = False
    mariadb_enabled: bool = False
    backup_repository: str = "/var/lib/eta/backups/restic"
    backup_schedule: str = "daily"
    firmware: str = "uefi"
    swap_mib: int = 1024
    timezone: str = "UTC"
    confirm_erase: str = ""
    source_dir: str = FLAKE_SOURCE_DEFAULT

    def redacted(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["password"] = "<redacted>"
        return data


def validate_install_config(config: InstallConfig, *, check_block_device: bool = True) -> list[str]:
    errors: list[str] = []
    if not config.disk or not config.disk.startswith("/dev/"):
        errors.append("disk must be an absolute /dev path")
    elif check_block_device:
        try:
            mode = os.stat(config.disk).st_mode
            if not stat.S_ISBLK(mode):
                errors.append(f"{config.disk} is not a block device")
        except OSError as exc:
            errors.append(f"cannot stat disk: {exc}")
    try:
        check_hostname(config.hostname)
    except EtaError as exc:
        errors.append(str(exc))
    if len(config.password or "") < 12:
        errors.append("master password must be at least 12 characters")
    if config.password.lower() in COMMON_PASSWORDS:
        errors.append("master password is too common")
    if config.password and config.password.lower() == config.hostname.lower():
        errors.append("master password must not equal the hostname")
    if config.master_user != "eta-master":
        errors.append("the master user must be eta-master")
    if config.network_mode not in {"dhcp", "static"}:
        errors.append("network mode must be dhcp or static")
    if config.network_mode == "static":
        try:
            import ipaddress

            if not config.address or "/" not in config.address:
                raise ValueError
            ipaddress.ip_interface(config.address)
            if not config.gateway:
                raise ValueError
            ipaddress.ip_address(config.gateway)
            if config.dns:
                for server in config.dns.split(","):
                    ipaddress.ip_address(server.strip())
        except ValueError:
            errors.append("static network requires address/CIDR, gateway and valid DNS")
    if config.database_engine not in {"sqlite", "mariadb"}:
        errors.append("database engine must be sqlite or mariadb")
    if config.database_engine == "mariadb" and not config.mariadb_enabled:
        errors.append("MariaDB engine selected but MariaDB service is disabled")
    if config.backup_enabled:
        if not config.backup_repository.startswith("/"):
            errors.append("backup repository must be an absolute path")
        if config.backup_schedule not in {"hourly", "daily", "weekly", "off"}:
            errors.append("backup schedule must be hourly, daily, weekly or off")
    if config.firmware not in {"uefi", "bios"}:
        errors.append("firmware must be uefi or bios")
    if config.swap_mib < 0 or config.swap_mib > 16384:
        errors.append("swap size must be between 0 and 16384 MiB")
    if config.confirm_erase != config.disk:
        errors.append(f"erase confirmation does not match disk (expected {config.disk!r})")
    return errors


def part_name(disk: str, index: int) -> str:
    if re.search(r"\d$", disk):
        return f"{disk}p{index}"
    return f"{disk}{index}"


def partition_plan(disk: str, disk_mib: int, firmware: str, swap_mib: int) -> list[dict[str, Any]]:
    """Return a simple GPT plan: [ESP or bios_grub], swap, root."""
    if disk_mib < 8192:
        raise InstallError("disk is smaller than 8 GiB")
    swap_mib = max(0, min(swap_mib, max(0, disk_mib - 6144)))
    plan: list[dict[str, Any]] = []
    cursor = 1
    if firmware == "uefi":
        plan.append({"index": 1, "name": "ETABOOT", "start": 1, "end": 513, "type": "esp"})
        cursor = 513
    else:
        plan.append({"index": 1, "name": "ETABIOS", "start": 1, "end": 3, "type": "bios_grub"})
        cursor = 3
    swap_index = len(plan) + 1
    if swap_mib > 0:
        plan.append(
            {
                "index": swap_index,
                "name": "ETASWAP",
                "start": cursor,
                "end": cursor + swap_mib,
                "type": "swap",
            }
        )
        cursor += swap_mib
    root_index = len(plan) + 1
    plan.append(
        {
            "index": root_index,
            "name": "ETAROOT",
            "start": cursor,
            "end": disk_mib,
            "type": "root",
        }
    )
    return plan


def _nix(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _schedule(value: str) -> str:
    return {
        "hourly": "*-*-* *:15:00",
        "daily": "*-*-* 03:30:00",
        "weekly": "Sun *-*-* 03:30:00",
    }.get(value, "")


def generate_eta_local(config: InstallConfig) -> str:
    """Generate the declarative per-install NixOS module."""
    lines = [
        "# Generated by the Eta OS offline installer. Edit with care.",
        "# " + IDENTITY_LINE,
        "{ lib, ... }:",
        "{",
        f"  networking.hostName = {_nix(config.hostname)};",
        "  networking.useNetworkd = true;",
        f"  time.timeZone = {_nix(config.timezone)};",
        f"  system.stateVersion = {_nix('26.05')};",
        "",
        f"  eta.database.engine = {_nix(config.database_engine)};",
        f"  eta.services.backend.enable = {'true' if config.backend_enabled else 'false'};",
        f"  eta.services.backup.enable = {'true' if config.backup_enabled and config.backup_schedule != 'off' else 'false'};",
        f"  eta.services.ssh.enable = {'true' if config.ssh_enabled else 'false'};",
        f"  eta.services.mariadb.enable = {'true' if config.mariadb_enabled else 'false'};",
        f"  eta.backup.repository = {_nix(config.backup_repository)};",
        f"  eta.backup.schedule = {_nix(config.backup_schedule)};",
        f"  eta.backup.onCalendar = {_nix(_schedule(config.backup_schedule))};",
        "",
        f"  boot.loader.grub.device = {_nix(config.disk)};",
        "  boot.loader.grub.efiInstallAsRemovable = true;",
        "",
    ]
    if config.network_mode == "dhcp":
        lines.append("  networking.useDHCP = true;")
    else:
        lines.extend(
            [
                "  networking.useDHCP = false;",
                "  systemd.network.networks.\"10-eta-lan\" = {",
                "    matchConfig.Name = " + _nix(config.interface or "en*") + ";",
                "    networkConfig.DHCP = \"no\";",
                f"    address = [ {_nix(config.address)} ];",
                f"    gateway = [ {_nix(config.gateway)} ];",
            ]
        )
        if config.dns:
            dns_values = ", ".join(_nix(server.strip()) for server in config.dns.split(",") if server.strip())
            lines.append(f"    dns = [ {dns_values} ];")
        lines.append("  };")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


@dataclass
class InstallResult:
    success: bool
    message: str
    method: str = ""
    target: str = str(MOUNT)
    log_path: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class Installer:
    def __init__(
        self,
        *,
        runner: Callable[..., CommandResult] = run,
        output: Any = None,
        input_func: Callable[[str], str] = input,
        getpass_func: Callable[[str], str] = getpass.getpass,
        clock: Callable[[], float] = time.time,
        logger: EtaLogger | None = None,
    ) -> None:
        self.runner = runner
        self.output = output or sys.stdout
        self.input_func = input_func
        self.getpass_func = getpass_func
        self.clock = clock
        self.logger = logger or EtaLogger("install", "/var/log/eta", run_dir="/run/eta", clock=clock)
        self.log_lines: list[str] = []
        self._target_log: Path | None = None

    # -- output / logging ---------------------------------------------------
    def say(self, text: str = "") -> None:
        print(text, file=self.output)
        self._append_log(text)

    def _append_log(self, text: str) -> None:
        line = f"{utc_iso(self.clock())} {text}"
        self.log_lines.append(line)
        if self._target_log is not None:
            try:
                with open(self._target_log, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    def log(self, message: str, severity: str = "info", **fields: Any) -> None:
        self._append_log(f"{severity.upper()} {message}")
        try:
            self.logger.log(severity, message, event="install", **fields)
        except Exception:
            pass

    # -- discovery ----------------------------------------------------------
    def list_disks(self) -> list[dict[str, Any]]:
        result = self.runner(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MODEL,MOUNTPOINTS"], timeout=15)
        if not result.ok:
            raise InstallError(f"cannot list disks: {result.output}")
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise InstallError("cannot parse lsblk output") from exc
        disks = []
        for device in payload.get("blockdevices", []):
            if device.get("type") != "disk":
                continue
            name = str(device.get("name"))
            if name.startswith(("loop", "zram", "sr", "ram")):
                continue
            disks.append(
                {
                    "path": f"/dev/{name}",
                    "size": int(device.get("size") or 0),
                    "model": (device.get("model") or "").strip(),
                    "mountpoints": device.get("mountpoints") or [],
                }
            )
        return disks

    def detect_firmware(self) -> str:
        return "uefi" if Path("/sys/firmware/efi").exists() else "bios"

    def detect_interface(self) -> str:
        try:
            for path in sorted(Path("/sys/class/net").iterdir()):
                if path.name == "lo":
                    continue
                if Path(f"/sys/class/net/{path.name}/device").exists():
                    return path.name
        except OSError:
            pass
        return ""

    # -- prompts ------------------------------------------------------------
    def _ask(self, prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        value = self.input_func(f"{prompt}{suffix}: ").strip()
        return value or default

    def _ask_bool(self, prompt: str, default: bool) -> bool:
        marker = "Y/n" if default else "y/N"
        value = self._ask(f"{prompt} ({marker})", "y" if default else "n").lower()
        return value in {"y", "yes"}

    def collect_interactive(self) -> InstallConfig:
        self.say(BANNER)
        self.say("Offline installation. Nothing will be downloaded.\n")
        disks = self.list_disks()
        if not disks:
            raise InstallError("no installable disks found")
        self.say("Available disks:")
        for disk in disks:
            mounted = ", ".join(str(m) for m in disk["mountpoints"] if m) or "not mounted"
            self.say(f"  {disk['path']:20} {human_bytes(disk['size']):>10}  {disk['model']}  ({mounted})")
        disk = self._ask("\nTarget disk (type the full path)")
        if not disk.startswith("/dev/"):
            raise InstallError("invalid disk path")
        self.say(f"\nWARNING: this erases {disk} completely.")
        confirmation = self._ask(f"Type {disk} to confirm")
        if confirmation != disk:
            raise InstallError("disk confirmation did not match; aborting")
        hostname = self._ask("Hostname", "eta")
        password = self.getpass_func("Master password (min 12 chars): ")
        again = self.getpass_func("Repeat master password: ")
        if password != again:
            raise InstallError("passwords do not match")
        self.say("\nNetwork: DHCP is the default. Static IPv4 is optional.")
        static = self._ask_bool("Configure a static IPv4 address?", False)
        network_mode = "static" if static else "dhcp"
        address = gateway = dns = interface = ""
        if static:
            interface = self._ask("Interface name", self.detect_interface() or "eth0")
            address = self._ask("Address/CIDR (e.g. 192.168.1.10/24)")
            gateway = self._ask("Gateway")
            dns = self._ask("DNS servers (comma separated)", "1.1.1.1")
        self.say("\nServices:")
        backend = self._ask_bool("Enable backend API?", True)
        backup = self._ask_bool("Enable scheduled backups?", True)
        ssh = self._ask_bool("Enable SSH administration? (default off)", False)
        mariadb = self._ask_bool("Install optional MariaDB instead of SQLite?", False)
        database_engine = "mariadb" if mariadb else "sqlite"
        repository = "/var/lib/eta/backups/restic"
        schedule = "daily"
        if backup:
            repository = self._ask("Backup repository path", repository)
            schedule = self._ask("Backup schedule (hourly/daily/weekly/off)", "daily")
        self.say("\nInstallation summary:")
        preview = InstallConfig(
            disk=disk,
            hostname=hostname,
            password=password,
            network_mode=network_mode,
            address=address,
            gateway=gateway,
            dns=dns,
            interface=interface,
            database_engine=database_engine,
            backend_enabled=backend,
            backup_enabled=backup,
            ssh_enabled=ssh,
            mariadb_enabled=mariadb,
            backup_repository=repository,
            backup_schedule=schedule,
            firmware=self.detect_firmware(),
            confirm_erase=disk,
            source_dir=FLAKE_SOURCE_DEFAULT,
        )
        for key, value in preview.redacted().items():
            if key in {"password"}:
                continue
            self.say(f"  {key:20} {value}")
        if not self._ask_bool("\nProceed with installation?", False):
            raise InstallError("installation cancelled")
        return preview

    def collect_auto(self, args: argparse.Namespace) -> InstallConfig:
        password = ""
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        elif sys.stdin.isatty():
            password = self.getpass_func("Master password (min 12 chars): ")
            again = self.getpass_func("Repeat master password: ")
            if password != again:
                raise InstallError("passwords do not match")
        firmware = args.firmware or self.detect_firmware()
        return InstallConfig(
            disk=args.disk or "",
            hostname=args.hostname or "eta",
            password=password,
            network_mode=args.network or "dhcp",
            address=args.address or "",
            gateway=args.gateway or "",
            dns=args.dns or "",
            interface=args.interface or self.detect_interface(),
            database_engine="mariadb" if args.mariadb else "sqlite",
            backend_enabled=not args.no_backend,
            backup_enabled=not args.no_backup,
            ssh_enabled=args.ssh,
            mariadb_enabled=args.mariadb,
            backup_repository=args.backup_repo or "/var/lib/eta/backups/restic",
            backup_schedule=args.schedule or "daily",
            firmware=firmware,
            swap_mib=args.swap,
            timezone=args.timezone or "UTC",
            confirm_erase=args.confirm_erase or "",
            source_dir=args.source or FLAKE_SOURCE_DEFAULT,
        )

    # -- preflight ----------------------------------------------------------
    def preflight(self, config: InstallConfig) -> None:
        if os.geteuid() != 0:
            raise InstallError("the installer must run as root")
        errors = validate_install_config(config)
        if errors:
            raise InstallError("; ".join(errors))
        missing = [command for command in REQUIRED_COMMANDS if not shutil.which(command)]
        if missing:
            raise InstallError(f"required installer commands missing: {', '.join(missing)}")
        source = Path(config.source_dir)
        if not (source / "flake.nix").is_file():
            raise InstallError(f"offline flake source missing: {source}")
        if not Path(config.disk).exists():
            raise InstallError(f"disk does not exist: {config.disk}")
        if self._is_mounted(config.disk):
            raise InstallError(f"refusing to erase a mounted disk: {config.disk}")
        if self._has_mounted_children(config.disk):
            raise InstallError(f"refusing to erase a disk with mounted partitions: {config.disk}")
        disk_bytes = self._disk_size(config.disk)
        if disk_bytes < MIN_DISK_BYTES:
            raise InstallError(f"disk is too small ({human_bytes(disk_bytes)}); need at least 8 GiB")
        if MOUNT.exists() and any(MOUNT.iterdir()):
            raise InstallError(f"{MOUNT} is not empty; unmount it before installing")

    def _is_mounted(self, disk: str) -> bool:
        try:
            for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[0] == disk:
                    return True
        except OSError:
            pass
        return False

    def _has_mounted_children(self, disk: str) -> bool:
        try:
            for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[0].startswith(disk):
                    return True
        except OSError:
            pass
        return False

    def _disk_size(self, disk: str) -> int:
        try:
            with open(disk, "rb") as handle:
                return os.lseek(handle.fileno(), 0, os.SEEK_END)
        except OSError:
            return 0

    # -- execution ----------------------------------------------------------
    def run_command(self, argv: Sequence[str], *, timeout: float = 600.0, input_text: str | None = None) -> CommandResult:
        self.log(f"$ {' '.join(argv)}")
        result = self.runner(argv, timeout=timeout, input_text=input_text)
        if result.stdout.strip():
            self._append_log(result.stdout.strip()[:4000])
        if result.stderr.strip():
            self._append_log("stderr: " + result.stderr.strip()[:4000])
        return result

    def check(self, argv: Sequence[str], *, timeout: float = 600.0, input_text: str | None = None) -> CommandResult:
        result = self.run_command(argv, timeout=timeout, input_text=input_text)
        if not result.ok:
            raise InstallError(f"command failed ({result.returncode}): {' '.join(argv)}\n{result.output[:1200]}")
        return result

    def install(self, config: InstallConfig) -> InstallResult:
        started = self.clock()
        self.preflight(config)
        plan = partition_plan(config.disk, self._disk_size(config.disk) // (1024 * 1024), config.firmware, config.swap_mib)
        self.log("Installation started", disk=config.disk, hostname=config.hostname, firmware=config.firmware)
        try:
            self._partition(config, plan)
            self._format_and_mount(config, plan)
            self._prepare_target_log()
            hardware = self._generate_hardware_config()
            source = self._prepare_source(config, hardware)
            method, detail = self._install_system(config, source)
            self._set_password(config)
            self._write_target_settings(config, plan)
            self._copy_source_for_updates(source)
            self._unmount()
            duration = self.clock() - started
            result = InstallResult(
                success=True,
                message=f"Eta OS installed on {config.disk} in {int(duration)}s",
                method=method,
                log_path=str(MOUNT / "var/log/eta/install.log"),
                details={"plan": plan, "duration_seconds": round(duration, 1), "detail": detail},
            )
            self.log("Installation complete", method=method, duration_seconds=round(duration, 1))
            self.say("\n" + "=" * 64)
            self.say(f"{PRODUCT} {__version__} installed successfully.")
            self.say(IDENTITY_LINE)
            self.say(f"Method: {method}")
            self.say(f"Log:    /var/log/eta/install.log (inside the installed system)")
            self.say("Reboot and remove the installation media.")
            self.say("=" * 64 + "\n")
            return result
        except Exception as exc:
            self.log(f"Installation failed: {exc}", severity="critical")
            self.say(f"\nINSTALLATION FAILED: {exc}")
            try:
                self._unmount(ignore_errors=True)
            except Exception:
                pass
            raise

    def _partition(self, config: InstallConfig, plan: list[dict[str, Any]]) -> None:
        self.say("Partitioning disk...")
        self.check(["parted", "-s", config.disk, "mklabel", "gpt"])
        for entry in plan:
            if entry["type"] == "bios_grub":
                self.check(["parted", "-s", config.disk, "mkpart", "ETABIOS", "1MiB", f"{entry['end']}MiB"])
                self.check(["parted", "-s", config.disk, "set", str(entry["index"]), "bios_grub", "on"])
            elif entry["type"] == "esp":
                self.check(
                    ["parted", "-s", config.disk, "mkpart", "ETABOOT", "fat32", f"{entry['start']}MiB", f"{entry['end']}MiB"]
                )
                self.check(["parted", "-s", config.disk, "set", str(entry["index"]), "esp", "on"])
            elif entry["type"] == "swap":
                self.check(
                    ["parted", "-s", config.disk, "mkpart", "ETASWAP", "linux-swap", f"{entry['start']}MiB", f"{entry['end']}MiB"]
                )
            elif entry["type"] == "root":
                self.check(
                    ["parted", "-s", config.disk, "mkpart", "ETAROOT", "ext4", f"{entry['start']}MiB", f"{entry['end']}MiB"]
                )
        self.check(["partprobe", config.disk]) if shutil.which("partprobe") else None
        time.sleep(1)

    def _format_and_mount(self, config: InstallConfig, plan: list[dict[str, Any]]) -> None:
        self.say("Formatting filesystems...")
        root = part_name(config.disk, plan[-1]["index"])
        self.check(["mkfs.ext4", "-F", "-L", "ETAROOT", root])
        for entry in plan:
            part = part_name(config.disk, entry["index"])
            if entry["type"] == "esp":
                self.check(["mkfs.fat", "-F32", "-n", "ETABOOT", part])
            elif entry["type"] == "swap":
                self.check(["mkswap", "-L", "ETASWAP", part])
        self.say("Mounting target...")
        self.check(["mount", root, str(MOUNT)])
        for entry in plan:
            if entry["type"] == "esp":
                boot = MOUNT / "boot"
                boot.mkdir(parents=True, exist_ok=True)
                self.check(["mount", part_name(config.disk, entry["index"]), str(boot)])
        for path in ("dev", "proc", "sys"):
            target = MOUNT / path
            target.mkdir(parents=True, exist_ok=True)

    def _prepare_target_log(self) -> None:
        try:
            log_dir = MOUNT / "var/log/eta"
            log_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(log_dir, 0o750)
            self._target_log = log_dir / "install.log"
            for line in self.log_lines:
                with open(self._target_log, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            os.chmod(self._target_log, 0o640)
        except OSError:
            self._target_log = None

    def _generate_hardware_config(self) -> str:
        self.say("Detecting hardware...")
        result = self.check(["nixos-generate-config", "--root", str(MOUNT), "--show-hardware-config"], timeout=120)
        return result.stdout

    def _prepare_source(self, config: InstallConfig, hardware: str) -> Path:
        source = Path("/tmp/eta-install-src")
        if source.exists():
            shutil.rmtree(source)
        shutil.copytree(
            config.source_dir,
            source,
            ignore=shutil.ignore_patterns(".git", ".venv", "result", "result-*", "__pycache__", "*.pyc"),
        )
        (source / "hardware-configuration.nix").write_text(hardware, encoding="utf-8")
        (source / "eta-local.nix").write_text(generate_eta_local(config), encoding="utf-8")
        os.chmod(source / "eta-local.nix", 0o644)
        return source

    def _install_system(self, config: InstallConfig, source: Path) -> tuple[str, str]:
        self.say("Installing the system closure (offline)...")
        flake = f"path:{source}#eta"
        argv = [
            "nixos-install",
            "--root",
            str(MOUNT),
            "--flake",
            flake,
            "--no-root-passwd",
            "--no-channel-copy",
            "--option",
            "substituters",
            "",
            "--option",
            "builders",
            "",
        ]
        env = {
            "NIX_CONFIG": "substituters =\nbuilders =\nconnect-timeout = 1\n",
            "http_proxy": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
        }
        result = self.runner(argv, timeout=7200, env=env)
        if result.ok:
            return "flake-offline", (result.stdout or "")[-1000:]
        self.log(f"Offline flake install failed ({result.returncode}); trying baked target closure", severity="warning")
        self.say("Offline flake build unavailable; using the prebuilt target closure.")
        target = os.environ.get("ETA_TARGET_SYSTEM") or ""
        if not target:
            try:
                target = Path(TARGET_SYSTEM_FILE).read_text(encoding="utf-8").strip()
            except OSError:
                target = ""
        if not target or not Path(target).exists():
            raise InstallError(
                "offline flake install failed and no baked target system is available: "
                + (result.output or "")[-1200:]
            )
        fallback = [
            "nixos-install",
            "--root",
            str(MOUNT),
            "--system",
            target,
            "--no-root-passwd",
            "--no-channel-copy",
        ]
        result = self.runner(fallback, timeout=3600)
        if not result.ok:
            raise InstallError(f"baked closure install failed: {result.output[-1200:]}")
        return "baked-closure", target

    def _set_password(self, config: InstallConfig) -> None:
        self.say("Setting the master password (hashed by PAM/shadow)...")
        # Never put the password on a command line or in the log.
        result = self.runner(
            ["nixos-enter", "--root", str(MOUNT), "--command", "chpasswd"],
            timeout=120,
            input_text=f"{config.master_user}:{config.password}\n",
        )
        if not result.ok:
            raise InstallError(f"failed to set master password: {result.output[-800:]}")
        shadow = MOUNT / "etc/shadow"
        try:
            lines = shadow.read_text(encoding="utf-8").splitlines()
            entry = next((line for line in lines if line.startswith(f"{config.master_user}:")), "")
            if not entry or f"{config.master_user}:$" not in entry:
                raise InstallError("master password was not stored as a hash")
            if config.password in entry:
                raise InstallError("refusing to continue: password appears in /etc/shadow in plaintext")
        except OSError as exc:
            raise InstallError(f"cannot verify /etc/shadow: {exc}") from exc

    def _write_target_settings(self, config: InstallConfig, plan: list[dict[str, Any]]) -> None:
        self.say("Writing Eta OS settings...")
        etc_eta = MOUNT / "etc/eta"
        etc_eta.mkdir(parents=True, exist_ok=True)
        os.chmod(etc_eta, 0o755)
        state = MOUNT / "var/lib/eta"
        state.mkdir(parents=True, exist_ok=True)
        os.chmod(state, 0o750)
        backup_repo = Path(config.backup_repository)
        target_repo = MOUNT / backup_repo.relative_to("/") if backup_repo.is_absolute() else backup_repo
        try:
            target_repo.mkdir(parents=True, exist_ok=True)
            os.chmod(target_repo, 0o700)
        except OSError:
            pass
        if config.backup_enabled:
            key_path = state / "restic.key"
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(32) + "\n")
            os.chmod(key_path, 0o600)
        settings = {
            "schema": 1,
            "hostname": config.hostname,
            "master_user": config.master_user,
            "database": {"engine": config.database_engine},
            "services": {
                "security": True,
                "database": True,
                "backend": config.backend_enabled,
                "monitor": True,
                "backup": config.backup_enabled and config.backup_schedule != "off",
                "ssh": config.ssh_enabled,
                "mariadb": config.mariadb_enabled,
            },
            "backup": {
                "enabled": config.backup_enabled and config.backup_schedule != "off",
                "repository": config.backup_repository,
                "schedule": config.backup_schedule,
            },
        }
        atomic_write_json(etc_eta / "settings.json", settings, mode=0o644)
        install_info = {
            "schema": "eta.install.v1",
            "eta_version": __version__,
            "upstream": UPSTREAM,
            "creator": CREATOR,
            "installed_at": utc_iso(self.clock()),
            "disk": config.disk,
            "firmware": config.firmware,
            "plan": plan,
            "network_mode": config.network_mode,
        }
        atomic_write_json(etc_eta / "install.json", install_info, mode=0o644)
        os.makedirs(MOUNT / "etc/eta/maintenance.d", exist_ok=True)
        os.chmod(MOUNT / "etc/eta/maintenance.d", 0o700)

    def _copy_source_for_updates(self, source: Path) -> None:
        destination = MOUNT / "etc/eta/source"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for root, dirs, files in os.walk(destination):
            os.chmod(root, 0o755)
            for name in files:
                path = Path(root) / name
                if path.suffix in {".sh"}:
                    os.chmod(path, 0o755)
                else:
                    os.chmod(path, 0o644)

    def _unmount(self, *, ignore_errors: bool = False) -> None:
        boot = MOUNT / "boot"
        for target in (boot, MOUNT):
            if target.exists():
                result = self.runner(["umount", str(target)], timeout=60)
                if not result.ok and not ignore_errors:
                    self.log(f"umount failed for {target}: {result.output}", severity="warning")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eta-install",
        description=f"{PRODUCT} offline installer. {IDENTITY_LINE}",
    )
    parser.add_argument("--auto", action="store_true", help="non-interactive installation")
    parser.add_argument("--disk")
    parser.add_argument("--hostname")
    parser.add_argument("--password-stdin", action="store_true", help="read the master password from stdin")
    parser.add_argument("--network", choices=["dhcp", "static"])
    parser.add_argument("--address")
    parser.add_argument("--gateway")
    parser.add_argument("--dns")
    parser.add_argument("--interface")
    parser.add_argument("--mariadb", action="store_true")
    parser.add_argument("--ssh", action="store_true")
    parser.add_argument("--no-backend", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--backup-repo")
    parser.add_argument("--schedule", choices=["hourly", "daily", "weekly", "off"])
    parser.add_argument("--firmware", choices=["uefi", "bios"])
    parser.add_argument("--swap", type=int, default=1024, help="swap size in MiB")
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--confirm-erase", metavar="DISK", help="required in automated mode")
    parser.add_argument("--source", default=FLAKE_SOURCE_DEFAULT)
    parser.add_argument("--yes", action="store_true", help="skip the final summary prompt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    installer = Installer()
    try:
        if args.auto:
            config = installer.collect_auto(args)
            if not args.yes and args.confirm_erase != config.disk:
                raise InstallError("automated install requires --confirm-erase matching --disk and --yes")
        else:
            config = installer.collect_interactive()
        installer.install(config)
        return 0
    except KeyboardInterrupt:
        print("\nInstallation cancelled.")
        return 130
    except InstallError as exc:
        print(f"eta-install: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"eta-install: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
