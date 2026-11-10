# SQLite by default; optional low-memory MariaDB.
{ config, lib, pkgs, etaPkg, ... }:
let
  cfg = config.eta;
  isMaria = cfg.database.engine == "mariadb";
in
{
  config = lib.mkIf cfg.enable {
    eta.services.mariadb.enable = lib.mkDefault isMaria;

    services.mysql = lib.mkIf isMaria {
      enable = true;
      package = pkgs.mariadb;
      dataDir = "/var/lib/mysql";
      settings = {
        mysqld = {
          bind-address = "127.0.0.1";
          port = 3306;
          socket = cfg.database.mariadbSocket;
          innodb_buffer_pool_size = "64M";
          innodb_log_buffer_size = "8M";
          performance_schema = false;
          max_connections = 20;
          skip-name-resolve = true;
        };
      };
    };

    systemd.services.mysql = lib.mkIf isMaria {
      serviceConfig.MemoryMax = "320M";
      serviceConfig.Nice = 5;
    };

    systemd.services.eta-db-init = lib.mkIf cfg.services.database.enable {
      description = "Initialize the Eta OS database";
      wantedBy = [ "eta.target" ];
      before = [ "eta.target" "eta-backend.service" "eta-monitor.service" ];
      after = [ "local-fs.target" ] ++ lib.optional isMaria "mysql.service";
      requires = lib.optional isMaria "mysql.service";
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${etaPkg}/bin/eta-db-init";
        TimeoutStartSec = 120;
        Restart = "on-failure";
        RestartSec = 5;
        StartLimitBurst = 3;
        StartLimitIntervalSec = 300;
      };
    };
  };
}
