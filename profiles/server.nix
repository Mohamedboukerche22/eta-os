# Server profile: terminal only, boot display enabled, no desktop.
{ lib, ... }:
{
  eta.boot.display.enable = lib.mkDefault true;
  eta.boot.serialConsole = lib.mkDefault true;
  eta.services.backend.enable = lib.mkDefault true;
  eta.services.backup.enable = lib.mkDefault true;
  eta.services.monitor.enable = lib.mkDefault true;
  eta.services.database.enable = lib.mkDefault true;
  eta.services.security.enable = lib.mkDefault true;
  eta.services.ssh.enable = lib.mkDefault false;

  # A server appliance has no use for a desktop session.
  services.xserver.enable = false;
  services.printing.enable = false;
  services.avahi.enable = false;
  hardware.bluetooth.enable = false;
}
