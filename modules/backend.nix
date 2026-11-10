# Unprivileged, dependency-free HTTP backend.
{ config, lib, pkgs, etaPkg, ... }:
let
  cfg = config.eta;
in
{
  config = lib.mkIf (cfg.enable && cfg.services.backend.enable) {
    systemd.services.eta-backend = {
      description = "Eta OS backend API";
      wantedBy = [ "eta.target" ];
      after = [ "local-fs.target" "network.target" ]
        ++ lib.optional cfg.services.database.enable "eta-db-init.service";
      requires = lib.optional cfg.services.database.enable "eta-db-init.service";
      environment = {
        PYTHONUNBUFFERED = "1";
        ETA_LOG_DIR = cfg.logDir;
        ETA_RUN_DIR = cfg.runDir;
      };
      serviceConfig = {
        Type = "notify";
        NotifyAccess = "main";
        WatchdogSec = 60;
        User = "eta-backend";
        Group = "eta-backend";
        SupplementaryGroups = [ "eta-log" ];
        WorkingDirectory = cfg.stateDir;
        ExecStart = "${etaPkg}/bin/eta-backend --bind ${cfg.backend.bind} --port ${toString cfg.backend.port}";
        Restart = "on-failure";
        RestartSec = 3;
        StartLimitBurst = 5;
        StartLimitIntervalSec = 300;
        TimeoutStartSec = 30;
        TimeoutStopSec = 20;
        KillSignal = "SIGTERM";
        MemoryMax = cfg.backend.memoryMax;
        TasksMax = 64;
        LimitNOFILE = 4096;
        UMask = "0007";
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.stateDir cfg.logDir ];
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        RestrictNamespaces = true;
        LockPersonality = true;
        CapabilityBoundingSet = "";
        AmbientCapabilities = "";
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
      };
    };
  };
}
