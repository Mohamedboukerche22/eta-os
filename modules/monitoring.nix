# Health monitor and bounded recovery supervisor.
{ config, lib, pkgs, etaPkg, ... }:
let
  cfg = config.eta;
in
{
  config = lib.mkIf (cfg.enable && cfg.services.monitor.enable) {
    systemd.services.eta-monitor = {
      description = "Eta OS health monitor and recovery supervisor";
      wantedBy = [ "eta.target" ];
      after = [ "local-fs.target" "eta-db-init.service" "eta-backend.service" ];
      wants = [ "eta-backend.service" ];
      environment = {
        PYTHONUNBUFFERED = "1";
        ETA_LOG_DIR = cfg.logDir;
        ETA_RUN_DIR = cfg.runDir;
      };
      serviceConfig = {
        Type = "notify";
        NotifyAccess = "main";
        WatchdogSec = 60;
        ExecStart = "${etaPkg}/bin/eta-monitor";
        Restart = "on-failure";
        RestartSec = 5;
        StartLimitBurst = 5;
        StartLimitIntervalSec = 600;
        TimeoutStartSec = 30;
        TimeoutStopSec = 20;
        MemoryMax = cfg.monitoring.memoryMax;
        Nice = 5;
        IOSchedulingClass = "best-effort";
        IOSchedulingPriority = 6;
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        CapabilityBoundingSet = "";
        AmbientCapabilities = "";
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
        ReadWritePaths = [ cfg.stateDir cfg.logDir cfg.runDir ];
      };
    };
  };
}
