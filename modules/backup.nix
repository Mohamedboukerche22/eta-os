# Scheduled restic backups with bounded resource use.
{ config, lib, pkgs, etaPkg, ... }:
let
  cfg = config.eta;
  repoPathWrite = lib.optionals (lib.hasPrefix "/" cfg.backup.repository) [ cfg.backup.repository ];
in
{
  config = lib.mkIf (cfg.enable && cfg.services.backup.enable) {
    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir}/restic-cache 0700 root root -"
    ];

    systemd.services.eta-backup = {
      description = "Eta OS restic backup";
      after = [ "local-fs.target" ];
      environment = {
        PYTHONUNBUFFERED = "1";
        ETA_LOG_DIR = cfg.logDir;
        ETA_RUN_DIR = cfg.runDir;
      };
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${etaPkg}/bin/eta-backup run --reason scheduled";
        TimeoutStartSec = 3600;
        Nice = 19;
        IOSchedulingClass = "idle";
        IOSchedulingPriority = 7;
        MemoryMax = cfg.backup.memoryMax;
        CPUQuota = "40%";
        UMask = "0077";
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.stateDir cfg.logDir cfg.runDir ] ++ repoPathWrite;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        CapabilityBoundingSet = "";
        AmbientCapabilities = "";
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
        Restart = "on-failure";
        RestartSec = 300;
        StartLimitBurst = 2;
        StartLimitIntervalSec = 3600;
      };
    };

    systemd.timers.eta-backup = lib.mkIf (cfg.backup.schedule != "off") {
      description = "Eta OS scheduled backup";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.backup.onCalendar;
        Persistent = true;
        RandomizedDelaySec = "10m";
        AccuracySec = "1m";
        Unit = "eta-backup.service";
      };
    };
  };
}
