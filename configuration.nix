# Eta OS system configuration.
# Created by mohamedboukerch55
{ ... }:
{
  imports = [
    ./hardware-configuration.nix
    ./eta-local.nix

    ./modules/base.nix
    ./modules/boot.nix
    ./modules/networking.nix
    ./modules/security.nix
    ./modules/logging.nix
    ./modules/database.nix
    ./modules/backend.nix
    ./modules/backup.nix
    ./modules/monitoring.nix
    ./modules/admin.nix
  ];
}
