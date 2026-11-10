# Shared Eta OS options, identity, packages and mutable state layout.
{ config, lib, pkgs, etaPkg, ... }:
let
  cfg = config.eta;
  json = builtins.toJSON;
  settings = {
    schema = 1;
    version = cfg.version;
    hostname = config.networking.hostName;
    master_user = cfg.masterUser;
    config_dir = "/etc/eta";
    log_dir = cfg.logDir;
    state_dir = cfg.stateDir;
    run_dir = cfg.runDir;
    database = {
      engine = cfg.database.engine;
      sqlite_path = cfg.database.sqlitePath;
      mariadb_socket = cfg.database.mariadbSocket;
      mariadb_database = cfg.database.mariadbDatabase;
      mariadb_user = cfg.database.mariadbUser;
      mariadb_backup_options = "/etc/eta/mariadb-backup.cnf";
      mariadb_app_password_file = "/var/lib/eta/mariadb-app.key";
    };
    backend = {
      bind = cfg.backend.bind;
      port = cfg.backend.port;
      max_workers = cfg.backend.maxWorkers;
      max_body_bytes = cfg.backend.maxBodyBytes;
    };
    monitor = {
      interval = cfg.monitoring.interval;
      max_attempts = cfg.monitoring.maxAttempts;
      attempt_window = cfg.monitoring.attemptWindow;
      backoff_base = cfg.monitoring.backoffBase;
      backoff_max = cfg.monitoring.backoffMax;
      stable_reset = cfg.monitoring.stableReset;
    };
    backup = {
      enabled = cfg.services.backup.enable;
      repository = cfg.backup.repository;
      password_file = cfg.backup.passwordFile;
      status_file = "/var/lib/eta/backup-status.json";
      snapshot_dir = "/var/lib/eta/snapshots";
      restore_root = "/var/lib/eta/restore";
      schedule = cfg.backup.schedule;
      keep_daily = cfg.backup.keepDaily;
      keep_weekly = cfg.backup.keepWeekly;
      keep_monthly = cfg.backup.keepMonthly;
      keep_yearly = cfg.backup.keepYearly;
      verify_subset = cfg.backup.verifySubset;
      paths = cfg.backup.paths;
      excludes = cfg.backup.excludes;
    };
    auth = {
      pam_service = cfg.auth.pamService;
      max_failures = cfg.auth.maxFailures;
      failure_window_seconds = cfg.auth.failureWindowSeconds;
      lockout_seconds = cfg.auth.lockoutSeconds;
    };
    maintenance = {
      require_auth = cfg.maintenance.requireAuth;
      max_auth_attempts = cfg.maintenance.maxAuthAttempts;
      allow_custom_scripts = cfg.maintenance.allowCustomScripts;
      script_dir = cfg.maintenance.scriptDir;
    };
    boot = {
      timeout_seconds = cfg.boot.timeoutSeconds;
      serial_console = cfg.boot.serialConsole;
    };
    services = {
      security = cfg.services.security.enable;
      database = cfg.services.database.enable;
      backend = cfg.services.backend.enable;
      monitor = cfg.services.monitor.enable;
      backup = cfg.services.backup.enable;
      ssh = cfg.services.ssh.enable;
      mariadb = cfg.services.mariadb.enable || cfg.database.engine == "mariadb";
    };
  };
in
{
  options.eta = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Enable the Eta OS platform.";
    };
    installerMode = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Build the live installer rather than the installed server.";
    };
    version = lib.mkOption {
      type = lib.types.str;
      default = "1.0.0";
      description = "Eta OS version.";
    };
    masterUser = lib.mkOption {
      type = lib.types.str;
      default = "eta-master";
      description = "The single master administrator account.";
    };
    logDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/log/eta";
    };
    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/eta";
    };
    runDir = lib.mkOption {
      type = lib.types.str;
      default = "/run/eta";
    };
    database = {
      engine = lib.mkOption {
        type = lib.types.enum [ "sqlite" "mariadb" ];
        default = "sqlite";
        description = "Default SQLite, optional low-memory MariaDB.";
      };
      sqlitePath = lib.mkOption {
        type = lib.types.str;
        default = "/var/lib/eta/eta.db";
      };
      mariadbSocket = lib.mkOption {
        type = lib.types.str;
        default = "/run/mysqld/mysqld.sock";
      };
      mariadbDatabase = lib.mkOption {
        type = lib.types.str;
        default = "eta";
      };
      mariadbUser = lib.mkOption {
        type = lib.types.str;
        default = "eta";
      };
    };
    backend = {
      bind = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1";
        description = "Backend listen address. Loopback by default.";
      };
      port = lib.mkOption {
        type = lib.types.port;
        default = 8080;
      };
      maxWorkers = lib.mkOption {
        type = lib.types.int;
        default = 4;
      };
      maxBodyBytes = lib.mkOption {
        type = lib.types.int;
        default = 65536;
      };
      exposeToLan = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Open the backend port in the firewall. Explicit opt-in only.";
      };
      memoryMax = lib.mkOption {
        type = lib.types.str;
        default = "128M";
      };
    };
    services = {
      security.enable = lib.mkOption { type = lib.types.bool; default = true; };
      database.enable = lib.mkOption { type = lib.types.bool; default = true; };
      backend.enable = lib.mkOption { type = lib.types.bool; default = true; };
      monitor.enable = lib.mkOption { type = lib.types.bool; default = true; };
      backup.enable = lib.mkOption { type = lib.types.bool; default = true; };
      ssh.enable = lib.mkOption { type = lib.types.bool; default = false; };
      mariadb.enable = lib.mkOption { type = lib.types.bool; default = false; };
    };
    backup = {
      schedule = lib.mkOption {
        type = lib.types.enum [ "hourly" "daily" "weekly" "off" ];
        default = "daily";
      };
      onCalendar = lib.mkOption {
        type = lib.types.str;
        default = "*-*-* 03:30:00";
        description = "systemd OnCalendar expression for the backup timer.";
      };
      repository = lib.mkOption {
        type = lib.types.str;
        default = "/var/lib/eta/backups/restic";
      };
      passwordFile = lib.mkOption {
        type = lib.types.str;
        default = "/var/lib/eta/restic.key";
      };
      keepDaily = lib.mkOption { type = lib.types.int; default = 7; };
      keepWeekly = lib.mkOption { type = lib.types.int; default = 4; };
      keepMonthly = lib.mkOption { type = lib.types.int; default = 6; };
      keepYearly = lib.mkOption { type = lib.types.int; default = 1; };
      verifySubset = lib.mkOption { type = lib.types.str; default = "5%"; };
      paths = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ "/etc/eta" "/etc/nixos" "/var/log/eta" ];
      };
      excludes = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [
          "/var/lib/eta/backups"
          "/var/lib/eta/restore"
          "/var/lib/eta/snapshots"
          "/var/log/eta/*.gz"
        ];
      };
      memoryMax = lib.mkOption { type = lib.types.str; default = "256M"; };
    };
    monitoring = {
      interval = lib.mkOption { type = lib.types.int; default = 15; };
      maxAttempts = lib.mkOption { type = lib.types.int; default = 3; };
      attemptWindow = lib.mkOption { type = lib.types.int; default = 1800; };
      backoffBase = lib.mkOption { type = lib.types.int; default = 5; };
      backoffMax = lib.mkOption { type = lib.types.int; default = 120; };
      stableReset = lib.mkOption { type = lib.types.int; default = 600; };
      memoryMax = lib.mkOption { type = lib.types.str; default = "96M"; };
    };
    auth = {
      pamService = lib.mkOption { type = lib.types.str; default = "eta-admin"; };
      maxFailures = lib.mkOption { type = lib.types.int; default = 5; };
      failureWindowSeconds = lib.mkOption { type = lib.types.int; default = 300; };
      lockoutSeconds = lib.mkOption { type = lib.types.int; default = 300; };
    };
    maintenance = {
      requireAuth = lib.mkOption { type = lib.types.bool; default = true; };
      maxAuthAttempts = lib.mkOption { type = lib.types.int; default = 3; };
      allowCustomScripts = lib.mkOption { type = lib.types.bool; default = true; };
      scriptDir = lib.mkOption { type = lib.types.str; default = "/etc/eta/maintenance.d"; };
    };
    boot = {
      display.enable = lib.mkOption { type = lib.types.bool; default = true; };
      timeoutSeconds = lib.mkOption { type = lib.types.int; default = 180; };
      serialConsole = lib.mkOption { type = lib.types.bool; default = true; };
    };
    network.lanCIDRs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "10.0.0.0/8" "172.16.0.0/12" "192.168.0.0/16" ];
      description = "LAN ranges used in documentation and optional firewall rules.";
    };
  };

  config = lib.mkIf cfg.enable {
    system.stateVersion = "26.05";
    system.nixos.distroName = "Eta OS";
    system.nixos.distroId = "eta";
    system.nixos.label = "eta-${cfg.version}";

    nix.settings.experimental-features = [ "nix-command" "flakes" ];
    nix.settings.auto-optimise-store = false;
    nix.gc.automatic = false;
    nix.optimise.automatic = false;
    system.autoUpgrade.enable = false;

    environment.systemPackages = [
      etaPkg
      pkgs.restic
      pkgs.sqlite
      pkgs.jq
      pkgs.curl
      pkgs.less
      pkgs.nano
      pkgs.procps
      pkgs.iproute2
      pkgs.parted
      pkgs.dosfstools
      pkgs.e2fsprogs
      pkgs.util-linux
      pkgs.nixos-rebuild
      pkgs.kmod
      pkgs.which
      pkgs.shadow
    ];

    environment.etc."eta/defaults.json".text = json settings;
    environment.etc."eta/version".text = ''
      ETA OS ${cfg.version}
      Upstream: NixOS 26.05
      Creator: mohamedboukerch55
      License: MIT (project), MIT (NixOS/nixpkgs)
    '';
    environment.etc."eta/catalog/index.json".text = "{}\n";

    users.groups.eta-admin = { };
    users.groups.eta-log = { };
    users.groups.eta-backend = { };
    users.users.eta-backend = {
      isSystemUser = true;
      group = "eta-backend";
      extraGroups = [ "eta-log" ];
      description = "Eta OS backend service account";
    };
    users.users.${cfg.masterUser} = {
      isNormalUser = true;
      description = "Eta OS master administrator";
      extraGroups = [ "eta-admin" ];
      shell = pkgs.bash;
    };
    users.users.root.hashedPassword = "!";
    users.motd = ''

      ETA OS ${cfg.version}  |  NixOS 26.05
      Created by mohamedboukerch55
      Minimal. Offline. Reliable. Controlled.

    '';

    systemd.tmpfiles.rules = [
      "d ${cfg.logDir} 0770 root eta-log -"
      "d ${cfg.stateDir} 0770 root eta-backend -"
      "d /var/lib/eta/backups 0700 root root -"
      "d /var/lib/eta/snapshots 0700 root root -"
      "d /var/lib/eta/restore 0700 root root -"
      "d ${cfg.runDir} 0755 root root -"
      "d /etc/eta/catalog/units 0755 root root -"
      "d ${cfg.maintenance.scriptDir} 0700 root root -"
    ];

    systemd.services.eta-settings-apply = lib.mkIf (!cfg.installerMode) {
      description = "Apply Eta OS runtime settings";
      wantedBy = [ "eta.target" ];
      before = [ "eta.target" ];
      after = [ "local-fs.target" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${etaPkg}/bin/eta-settings-apply";
        TimeoutStartSec = 30;
        Restart = "on-failure";
        RestartSec = 3;
        StartLimitBurst = 3;
        StartLimitIntervalSec = 300;
      };
    };

    # The appliance never runs a desktop session or display manager.
    services.xserver.enable = false;
    services.printing.enable = false;
    services.avahi.enable = false;
    hardware.bluetooth.enable = false;
    services.timesyncd.enable = false;

    time.timeZone = lib.mkDefault "UTC";
    i18n.defaultLocale = "en_US.UTF-8";
    console.keyMap = lib.mkDefault "us";
  };
}
