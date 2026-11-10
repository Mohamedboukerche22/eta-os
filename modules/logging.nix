# Bounded persistent journal plus Eta OS JSONL rotation.
{ config, lib, pkgs, etaPkg, ... }:
let
  cfg = config.eta;
in
{
  config = lib.mkIf cfg.enable {
    services.journald.extraConfig = ''
      Storage=persistent
      Compress=yes
      SystemMaxUse=64M
      SystemMaxFileSize=8M
      RuntimeMaxUse=16M
      RuntimeMaxFileSize=4M
      MaxRetentionSec=7d
      ForwardToSyslog=no
      RateLimitIntervalSec=30s
      RateLimitBurst=1000
    '';

    systemd.services.eta-log-rotate = {
      description = "Rotate Eta OS JSONL logs by size";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${etaPkg}/bin/eta-log-rotate";
        Nice = 19;
        IOSchedulingClass = "idle";
        TimeoutStartSec = 60;
      };
    };

    systemd.timers.eta-log-rotate = {
      description = "Periodic Eta OS log rotation";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "*:0/15";
        Persistent = false;
        AccuracySec = "1min";
        Unit = "eta-log-rotate.service";
      };
    };
  };
}
