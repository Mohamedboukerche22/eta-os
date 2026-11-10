{
  lib,
  python3,
  makeWrapper,
  systemd,
  restic,
  sqlite,
  mariadb,
  util-linux,
  procps,
  iproute2,
  nix,
  nixos-rebuild,
  nixos-install-tools,
  parted,
  dosfstools,
  e2fsprogs,
  gptfdisk,
  coreutils,
  findutils,
  gnugrep,
  gawk,
  bash,
  kmod,
  shadow,
  which,
}:

let
  pythonDeps = [ python3.pkgs.rich ]
    ++ lib.optional (python3.pkgs ? python-pam) python3.pkgs.python-pam
    ++ lib.optional (python3.pkgs ? mysql-connector) python3.pkgs.mysql-connector
    ++ lib.optional (python3.pkgs ? mysql-connector-python) python3.pkgs.mysql-connector-python;

  runtimePath = lib.makeBinPath [
    systemd
    restic
    sqlite
    mariadb.client
    util-linux
    procps
    iproute2
    nix
    nixos-rebuild
    nixos-install-tools
    parted
    dosfstools
    e2fsprogs
    gptfdisk
    coreutils
    findutils
    gnugrep
    gawk
    bash
    kmod
    shadow
    which
  ];

  binaries = [
    "eta-admin"
    "eta-boot"
    "eta-monitor"
    "eta-backup"
    "eta-health"
    "eta-maintenance"
    "eta-install"
    "eta-priv"
    "eta-backend"
    "eta-db-init"
    "eta-settings-apply"
    "eta-security-check"
    "eta-log-rotate"
  ];
in
python3.pkgs.buildPythonApplication {
  pname = "eta-os";
  version = "1.0.0";
  format = "pyproject";

  src = lib.cleanSourceWith {
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

  dependencies = pythonDeps;
  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ python3.pkgs.pytestCheckHook ];
  doCheck = true;
  disabledTestMarks = [ "integration" ];

  postFixup = ''
    for prog in ${lib.escapeShellArgs binaries}; do
      if [ -e "$out/bin/$prog" ]; then
        wrapProgram "$out/bin/$prog" --prefix PATH : "${runtimePath}"
      fi
    done
  '';

  meta = with lib; {
    description = "Eta OS management and monitoring layer";
    homepage = "https://example.invalid/eta-os";
    license = licenses.mit;
    maintainers = [ ];
    platforms = [ "x86_64-linux" ];
    mainProgram = "eta-admin";
  };
}
