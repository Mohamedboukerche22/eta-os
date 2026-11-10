# Placeholder for the ISO target closure. The installer replaces this file with
# the output of nixos-generate-config for the actual machine.
{ lib, ... }:
{
  fileSystems."/" = {
    device = "/dev/disk/by-label/ETAROOT";
    fsType = "ext4";
  };
  swapDevices = [ ];
}
