# Offline-friendly networking. DHCP on the LAN, no cloud dependencies.
{ config, lib, ... }:
let
  cfg = config.eta;
  optional = lib.optional;
in
{
  config = lib.mkIf cfg.enable {
    networking.hostName = lib.mkDefault "eta";
    networking.domain = lib.mkDefault "eta.local";
    networking.useDHCP = lib.mkDefault true;
    networking.networkmanager.enable = false;
    networking.wireless.enable = false;
    networking.timeServers = [ ];

    networking.firewall.enable = true;
    networking.firewall.allowPing = true;
    networking.firewall.allowedTCPPorts =
      optional cfg.services.ssh.enable 22
      ++ optional cfg.backend.exposeToLan cfg.backend.port;
    networking.firewall.allowedUDPPorts = [ ];

    # Never accept a default route from an untrusted interface without thought.
    networking.firewall.checkReversePath = "loose";

    services.openssh = lib.mkIf cfg.services.ssh.enable {
      enable = true;
      openFirewall = false;
      settings = {
        PasswordAuthentication = false;
        KbdInteractiveAuthentication = false;
        PermitRootLogin = "no";
        AllowUsers = [ cfg.masterUser ];
        MaxAuthTries = 3;
        LogLevel = "VERBOSE";
        X11Forwarding = false;
        AllowAgentForwarding = false;
        AllowTcpForwarding = false;
      };
    };
  };
}
