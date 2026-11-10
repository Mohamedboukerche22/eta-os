# Eta OS live installer ISO.
#
# Minimal, offline, no network services. Log in as root on tty1 and run
# eta-install (or `eta/install.py`) to install the server. The ISO contains
# everything needed for a fully offline installation:
#
#   /etc/eta/source         the complete Eta OS flake source tree
#   /etc/eta/target-system  the prebuilt target system closure (fallback)
#
# Created by mohamedboukerch55
{ config, lib, pkgs, etaPkg, etaSource, targetSystem, outPath, ... }:
let
  cfg = config.eta;
in
{
  imports = [
    # Provides config.system.build.isoImage and the isolinux/systemd-boot
    # boot configuration for installation media.
    "${outPath}/nixos/modules/installer/cd-dvd/installation-cd-minimal.nix"
  ];

  # Live ISO: disable the persistent settings-apply oneshot from base.nix.
  eta.installerMode = true;
  eta.boot.display.enable = false;

  isoImage = {
    isoName = lib.mkDefault "eta-os-${cfg.version}-x86_64-linux.iso";
    isoBaseName = lib.mkDefault "eta-os";
    volumeID = "ETA_OS_1";
    squashfsCompression = "zstd";
    makeEfiBootable = true;
    makeUsbBootable = true;
    # The ISO must boot on low-memory machines; keep initrd small.
    includeSystemBuildDependencies = false;
  };

  # Never phone home. The offline installer forces empty substituters too,
  # but this makes the live environment equally quiet.
  nix.settings = {
    substituters = [ ];
    fallback = false;
  };

  # The complete source tree for declarative offline rebuilds/installs.
  environment.etc."eta/source".source = etaSource;

  # Baked target closure used as a fallback when the offline flake build
  # fails. Referencing it here copies it into the ISO's Nix store.
  environment.etc."eta/target-system".text = "${targetSystem}\n";

  environment.systemPackages = with pkgs; [
    etaPkg
    curl
    less
    nano
  ];

  # Console autologin as root keeps the live environment ergonomic; the
  # account itself stays locked (hashedPassword "!" from base.nix), so no
  # password or SSH login is possible on the live image.
  services.getty.autologinUser = lib.mkForce "root";

  # Nothing in the live environment should reach the network or persist.
  systemd.services.eta-settings-apply.enable = lib.mkForce false;

  networking.networkmanager.enable = lib.mkForce false;
  networking.wireless.enable = lib.mkForce false;
  networking.wireless.iwd.enable = lib.mkForce false;

  # The installer writes to the target disk, so the ISO itself needs the
  # partitioning, filesystem and install tooling (all in base.nix already):
  # parted, dosfstools, e2fsprogs, gptfdisk, util-linux (mkswap, blkid),
  # nixos-rebuild (nixos-generate-config, nixos-install, nixos-enter) and shadow.
}
