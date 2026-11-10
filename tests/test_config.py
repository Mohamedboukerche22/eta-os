"""Tests for eta.config module."""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

import pytest

from eta.config import (
    Settings,
    DEFAULT_SETTINGS,
    load_settings,
    ConfigError,
    service_enabled,
)


class TestSettings:
    def test_settings_defaults(self):
        settings = Settings(DEFAULT_SETTINGS)
        assert settings.get("schema") == 1
        assert settings.get("version") == "1.0.0"
        assert settings.get("hostname") == "eta"
        assert settings.get("master_user") == "eta-master"

    def test_settings_get_nested(self):
        settings = Settings(DEFAULT_SETTINGS)
        assert settings.get("database.engine") == "sqlite"
        assert settings.get("database.sqlite_path") == "/var/lib/eta/eta.db"
        assert settings.get("backend.port") == 8080

    def test_settings_get_default(self):
        settings = Settings(DEFAULT_SETTINGS)
        assert settings.get("nonexistent", "default") == "default"
        assert settings.get("database.nonexistent", "default") == "default"

    def test_settings_section(self):
        settings = Settings(DEFAULT_SETTINGS)
        db_section = settings.section("database")
        assert db_section["engine"] == "sqlite"
        assert "sqlite_path" in db_section

    def test_settings_enabled(self):
        settings = Settings(DEFAULT_SETTINGS)
        assert settings.enabled("security") is True
        assert settings.enabled("database") is True
        assert settings.enabled("ssh") is False
        assert settings.enabled("nonexistent") is False

    def test_settings_properties(self):
        settings = Settings(DEFAULT_SETTINGS)
        assert settings.log_dir == Path("/var/log/eta")
        assert settings.state_dir == Path("/var/lib/eta")
        assert settings.run_dir == Path("/run/eta")
        assert settings.master_user == "eta-master"
        assert settings.db_engine == "sqlite"
        assert settings.sqlite_path == Path("/var/lib/eta/eta.db")
        assert settings.backend_bind == "127.0.0.1"
        assert settings.backend_port == 8080
        assert settings.backend_url == "http://127.0.0.1:8080"

    def test_settings_as_dict(self):
        settings = Settings(DEFAULT_SETTINGS)
        data = settings.as_dict()
        assert data["schema"] == 1
        assert data["version"] == "1.0.0"
        # Should be a deep copy
        data["schema"] = 999
        assert settings.get("schema") == 1


class TestSettingsValidation:
    def test_valid_settings_pass(self):
        # Should not raise
        Settings(DEFAULT_SETTINGS)

    def test_invalid_absolute_path(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["log_dir"] = "relative/path"
        with pytest.raises(ConfigError, match="log_dir must be an absolute path"):
            Settings(data)

    def test_invalid_master_user(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["master_user"] = "Invalid-User"
        with pytest.raises(ConfigError, match="master_user is not a valid Unix user name"):
            Settings(data)

    def test_invalid_database_engine(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["database"]["engine"] = "postgres"
        with pytest.raises(ConfigError, match="database.engine must be 'sqlite' or 'mariadb'"):
            Settings(data)

    def test_invalid_backend_port(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["backend"]["port"] = 99999
        with pytest.raises(ConfigError, match="backend.port must be between 1 and 65535"):
            Settings(data)

        data["backend"]["port"] = 0
        with pytest.raises(ConfigError, match="backend.port must be between 1 and 65535"):
            Settings(data)

    def test_invalid_backend_bind(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["backend"]["bind"] = "not-an-ip"
        with pytest.raises(ConfigError, match="backend.bind must be an IPv4 or IPv6 address"):
            Settings(data)

    def test_invalid_monitor_interval(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["monitor"]["interval"] = 0
        with pytest.raises(ConfigError, match="monitor.interval must be positive"):
            Settings(data)

    def test_invalid_keep_values(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["backup"]["keep_daily"] = -1
        with pytest.raises(ConfigError, match="backup.keep_daily must be >= 0"):
            Settings(data)

    def test_invalid_verify_subset(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["backup"]["verify_subset"] = "invalid"
        with pytest.raises(ConfigError, match="backup.verify_subset must look like"):
            Settings(data)

    def test_invalid_services(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["services"] = "not-a-dict"
        with pytest.raises(ConfigError, match="services must be an object"):
            Settings(data)

        data["services"] = {"Invalid-Service": True}
        with pytest.raises(ConfigError, match="invalid service key"):
            Settings(data)

        data["services"] = {"security": "not-a-bool"}
        with pytest.raises(ConfigError, match="services.security must be a boolean"):
            Settings(data)


class TestLoadSettings:
    def test_load_settings_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            settings = load_settings(config_dir=config_dir, strict=False)
            assert settings.get("schema") == 1
            assert settings.get("version") == "1.0.0"

    def test_load_settings_with_defaults_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            defaults = {
                "hostname": "custom-host",
                "backend": {"port": 9090},
            }
            (config_dir / "defaults.json").write_text(json.dumps(defaults))
            settings = load_settings(config_dir=config_dir, strict=False)
            assert settings.get("hostname") == "custom-host"
            assert settings.get("backend.port") == 9090

    def test_load_settings_with_settings_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            settings_json = {
                "master_user": "custom-user",
                "database": {"engine": "mariadb"},
            }
            (config_dir / "settings.json").write_text(json.dumps(settings_json))
            settings = load_settings(config_dir=config_dir, strict=False)
            assert settings.get("master_user") == "custom-user"
            assert settings.get("database.engine") == "mariadb"

    def test_load_settings_env_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            env = {
                "ETA_HOSTNAME": "env-host",
                "ETA_BACKEND_PORT": "8081",
                "ETA_DB_ENGINE": "mariadb",
            }
            settings = load_settings(config_dir=config_dir, env=env, strict=False)
            assert settings.get("hostname") == "env-host"
            assert settings.get("backend.port") == 8081
            assert settings.get("database.engine") == "mariadb"

    def test_load_settings_explicit_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            explicit_file = Path(tmpdir) / "explicit.json"
            explicit_file.write_text(json.dumps({"hostname": "explicit-host"}))
            env = {"ETA_SETTINGS": str(explicit_file)}
            settings = load_settings(config_dir=config_dir, env=env, strict=False)
            assert settings.get("hostname") == "explicit-host"

    def test_load_settings_priority_order(self):
        """Test that later layers override earlier ones."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "defaults.json").write_text(json.dumps({"hostname": "defaults"}))
            (config_dir / "settings.json").write_text(json.dumps({"hostname": "settings"}))
            explicit_file = Path(tmpdir) / "explicit.json"
            explicit_file.write_text(json.dumps({"hostname": "explicit"}))
            env = {
                "ETA_SETTINGS": str(explicit_file),
                "ETA_HOSTNAME": "env",
            }
            settings = load_settings(config_dir=config_dir, env=env, strict=False)
            # Env should win
            assert settings.get("hostname") == "env"

    def test_load_settings_invalid_strict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "settings.json").write_text(json.dumps({"backend": {"port": 99999}}))
            with pytest.raises(ConfigError):
                load_settings(config_dir=config_dir, strict=True)

    def test_load_settings_invalid_non_strict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "settings.json").write_text(json.dumps({"backend": {"port": 99999}}))
            # Should fall back to defaults
            settings = load_settings(config_dir=config_dir, strict=False)
            assert settings.get("backend.port") == 8080


class TestServiceEnabled:
    def test_service_enabled_sqlite(self):
        settings = Settings(DEFAULT_SETTINGS)
        assert service_enabled(settings, "database") is True
        assert service_enabled(settings, "mariadb") is False

    def test_service_enabled_mariadb(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["database"]["engine"] = "mariadb"
        data["services"]["mariadb"] = True
        settings = Settings(data)
        assert service_enabled(settings, "database") is True
        assert service_enabled(settings, "mariadb") is True

    def test_service_enabled_mariadb_disabled(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["database"]["engine"] = "mariadb"
        data["services"]["mariadb"] = False
        settings = Settings(data)
        assert service_enabled(settings, "mariadb") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])