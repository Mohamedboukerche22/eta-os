# Terminal boot experience, platform target and boot loader.
{ config, lib, pkgs, etaPkg, ... }:
let
  cfg = config.eta;
  optional = lib.optional;
  wants =
    optional cfg.services.security.enable "eta-security-check.service"
    ++ optional cfg.services.database.enable "eta-db-init.service"
    ++ optional cfg.services.backend.enable "eta-backend.service"
    ++ optional cfg.services.monitor.enable "eta-monitor.service"
    ++ optional cfg.services.ssh.enable "sshd.service";
in
{
  config = lib.mkIf cfg.enable {
    boot.loader.systemd-boot.enable = lib.mkDefault true;
    boot.loader.efi.canTouchEfiVariables = lib.mkDefault true;
    boot.loader.timeout = lib.mkDefault 3;
    boot.consoleLogLevel = 3;
    boot.initrd.verbose = false;
    boot.kernelParams =
      [ "console=tty0" ]
      ++ lib.optionals cfg.boot.serialConsole [ "console=ttyS0,115200n8" ];

    services.getty.helpLine = ''

      Eta OS ${cfg.version} - Created by mohamedboukerch55
      Log in as ${cfg.masterUser} to administer this server.
    '';

    systemd.targets.eta = {
      description = "Eta OS platform target";
      wantedBy = [ "multi-user.target" ];
      wants = wants;
      after = [ "local-fs.target" "network.target" ];
    };

    systemd.services.eta-boot = lib.mkIf cfg.boot.display.enable {
      description = "Eta OS terminal boot display";
      wantedBy = [ "multi-user.target" ];
      before = [ "getty@tty1.service" ];
      after = [ "systemd-vconsole-setup.service" ];
      wants = [ "systemd-vconsole-setup.service" ];
      environment.ETA_BOOT_TIMEOUT = toString cfg.boot.timeoutSeconds;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        StandardInput = "tty";
        StandardOutput = "tty";
        StandardError = "tty";
        TTYPath = "/dev/tty1";
        TTYReset = true;
        TTYVHangup = false;
        TTYVTDisplacement = true;
        TimeoutStartSec = cfg.boot.timeoutSeconds + 120;
        Restart = "no";
      };
      unitConfig.ConditionPathExists = "/dev/tty1";
      script = ''
        exec ${etaPkg}/bin/eta-boot
      '';
    };

    systemd.services."getty@tty1".after = [ "eta-boot.service" ];

    systemd.services."serial-getty@ttyS0" = lib.mkIf cfg.boot.serialConsole {
      wantedBy = [ "getty.target" ];
    };
  };
}
