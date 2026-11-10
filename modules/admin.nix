# Master administration entry points. The heavy lifting lives in eta-admin.
{ config, lib, pkgs, etaPkg, ... }:
let
  cfg = config.eta;
in
{
  config = lib.mkIf cfg.enable {
    environment.etc."eta/admin.json".text = builtins.toJSON {
      schema = "eta.admin.v1";
      master_user = cfg.masterUser;
      admin_group = "eta-admin";
      privileged_helper = "${etaPkg}/bin/eta-priv";
      pam_service = cfg.auth.pamService;
      creator = "mohamedboukerch55";
    };

    environment.interactiveShellInit = ''
      if [ "$(id -un 2>/dev/null)" = "${cfg.masterUser}" ]; then
        printf '%s\n' "Eta OS master shell. 'eta-admin --help' for administration."
      fi
    '';

  };
}
