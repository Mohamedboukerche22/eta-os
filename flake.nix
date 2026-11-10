{
  description = "Eta OS - a minimal, offline-first NixOS server distribution";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  };

  outputs = { self, nixpkgs, ... }:
    let
      system = "x86_64-linux";
      lib = nixpkgs.lib;
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = false;
      };
      etaPkg = pkgs.callPackage ./package.nix { };
      etaSource = lib.cleanSourceWith {
        src = ./.;
        filter = path: type:
          let
            base = baseNameOf path;
          in
          !(builtins.elem base [
            ".venv"
            ".git"
            "result"
            "__pycache__"
            ".pytest_cache"
          ]) && !(lib.hasPrefix "result-" base) && !(lib.hasSuffix ".pyc" base);
      };
      mkEta = extraModules: lib.nixosSystem {
        inherit system;
        specialArgs = { inherit etaPkg etaSource; };
        modules = [
          ./configuration.nix
          ./profiles/server.nix
        ] ++ extraModules;
      };
      target = (mkEta [ ]).config.system.build.toplevel;
      installer = lib.nixosSystem {
        inherit system;
        specialArgs = {
          inherit etaPkg etaSource targetSystem;
          inherit (nixpkgs) outPath;
        };
        modules = [
          ./modules/base.nix
          ./modules/installer.nix
        ];
      };
      targetSystem = target;
    in
    {
      nixosConfigurations = {
        eta = mkEta [ ];
        eta-installer = installer;
      };

      packages.${system} = {
        default = installer.config.system.build.isoImage;
        iso = installer.config.system.build.isoImage;
        eta = etaPkg;
      };

      checks.${system} = {
        eta-python = etaPkg;
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          python3
          python3Packages.pytest
          python3Packages.rich
          restic
          sqlite
          jq
          shellcheck
        ];
        shellHook = ''
          export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
          echo "Eta OS development shell. Created by mohamedboukerch55"
        '';
      };
    };
}
