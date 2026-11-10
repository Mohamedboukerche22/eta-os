# Authentication, privilege separation and the permission audit.
{ config, lib, pkgs, etaPkg, ... }:
let
  cfg = config.eta;
in
{
  config = lib.mkIf cfg.enable {
    security.sudo = {
      enable = true;
      wheelNeedsPassword = true;
      extraRules = [
        {
          users = [ "%eta-admin" ];
          commands = [
            {
              command = "${etaPkg}/bin/eta-priv";
              options = [ "NOPASSWD" ];
            }
          ];
        }
      ];
    };

    # Interactive boot and maintenance authentication uses the system PAM
    # account database. Eta OS never stores or hashes passwords itself.
    security.pam.services.eta-admin = { };

    # The master account is the only member of eta-admin, and eta-admin is the
    # only group allowed to invoke eta-priv through sudo.
    users.users.${cfg.masterUser}.extraGroups = [ "eta-admin" ];

    systemd.services.eta-security-check = {
      description = "Eta OS permission and account audit";
      wantedBy = [ "eta.target" ];
      before = [ "eta.target" ];
      after = [ "local-fs.target" "systemd-sysusers.service" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${etaPkg}/bin/eta-security-check";
        TimeoutStartSec = 30;
        Restart = "on-failure";
        RestartSec = 5;
        StartLimitBurst = 3;
        StartLimitIntervalSec = 300;
      };
    };
  };
}
