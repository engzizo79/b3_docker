"""Backend configuration from environment variables."""

import os
from pathlib import Path


class Settings:
    """Runtime settings. The node RPC is loopback-only inside the container."""

    def __init__(self) -> None:
        self.rpc_host: str = os.environ.get("B3_RPC_HOST", "127.0.0.1")
        self.rpc_port: int = int(os.environ.get("B3_RPC_PORT", "32647"))
        self.rpc_user: str = os.environ.get("B3_RPC_USER", "")
        self.rpc_password: str = os.environ.get("B3_RPC_PASSWORD", "")
        # Data dir is the single source of truth: EVERY state path derives
        # from it so a custom B3_DATA_DIR can never split state across two
        # directories (which would put the DB/audit log on the image layer
        # while the daemon writes to the persistent volume).
        self.b3_data_dir: str = os.environ.get("B3_DATA_DIR", "/data")
        # SQLite DB (user, TOTP, audit log) — lives on the persistent data volume.
        self.db_path: str = os.environ.get("B3_DB_PATH", str(Path(self.b3_data_dir) / "b3hive.db"))
        self.ui_password: str = os.environ.get("UI_PASSWORD", "")
        self.session_secret: str = os.environ.get("SESSION_SECRET", "")
        self.totp_key: str = os.environ.get("TOTP_ENCRYPTION_KEY", "")
        # TOTP 2FA: required for remote access, bypassed on localhost when true.
        self.localhost_skip_2fa: bool = os.environ.get(
            "LOCALHOST_SKIP_2FA", "true").strip().lower() != "false"
        self.wallet_unlock_timeout: int = int(os.environ.get("WALLET_UNLOCK_TIMEOUT", "60"))
        self.auth_required: bool = os.environ.get("AUTH_REQUIRED", "1") == "1"
        self.stall_alert_minutes: int = int(os.environ.get("STALL_ALERT_MINUTES", "10"))
        self.stall_level: str = os.environ.get("STALL_LEVEL", "alert")
        self.explorer_url: str = os.environ.get("EXPLORER_URL", "https://explorer.b3hive.io")
        self.webhook_url: str = os.environ.get("WEBHOOK_URL", "")
        self.monitor_interval: int = int(os.environ.get("MONITOR_INTERVAL", "60"))
        self.recovery_cmd_file: str = os.environ.get("RECOVERY_CMD_FILE", str(Path(self.b3_data_dir) / "recovery.cmd"))
        # Cookie Secure flag: auto (default, HTTPS-aware via X-Forwarded-Proto), true, false
        self.cookie_secure: str = os.environ.get("COOKIE_SECURE", "auto")

        # --- v0.6.0 deployment modes + daemon management ---
        # "managed": the container downloads/supervises b3coind from official
        # GitHub releases (default). "external": UI-only mode, the daemon runs
        # elsewhere and the user configures the RPC connection.
        self.daemon_mode: str = os.environ.get("B3_DAEMON_MODE", "managed").strip().lower()
        # External-node RPC connection (UI-only mode; managed keeps loopback).
        self.ext_rpc_host: str = os.environ.get("EXT_RPC_HOST", "")
        self.ext_rpc_port: int = int(os.environ.get("EXT_RPC_PORT", "0"))
        self.ext_rpc_user: str = os.environ.get("EXT_RPC_USER", "")
        self.ext_rpc_password: str = os.environ.get("EXT_RPC_PASSWORD", "")
        # Minimum daemon version the UI supports; the wizard/upgrade flow
        # refuses anything older. Parsed as a dotted triple.
        self.min_daemon_version: str = os.environ.get("MIN_DAEMON_VERSION", "1.1.4")
        # Subfolder layout (v0.6.0): binaries under daemon/, chain+wallet data
        # under node/. Old flat layouts are migrated by the entrypoint.
        self.daemon_dir: str = str(Path(self.b3_data_dir) / "daemon")
        self.node_datadir: str = str(Path(self.b3_data_dir) / "node")
        self.daemon_version_file: str = str(Path(self.daemon_dir) / ".installed_version")
        # Release check result cache (monitor writes; UI reads).
        self.release_check_file: str = str(Path(self.b3_data_dir) / "release_check.json")
        # Transport security policy for remote plain-HTTP sessions:
        # "warn" (default) = persistent banner, guide card, no blocking
        # "block" = refuse all non-HTTPS remote sessions (strictest)
        self.require_secure_transport: str = os.environ.get(
	"REQUIRE_SECURE_TRANSPORT", "warn").strip().lower()
        # Enable ECDH envelope encryption for login password + wallet
        # passphrase (WebCrypto-compatible). Even over plain HTTP the
        # cleartext secret never crosses the wire. Default on.
        self.envelope_encryption: bool = os.environ.get(
	"ENVELOPE_ENCRYPTION", "true").strip().lower() != "false"
        # Setup wizard / bootstrap
        # (b3_data_dir is read at the top of __init__ — every state path
        # derives from it; see the comment there.)
        # Set by the entrypoint while the daemon is deferred (first UI run, wizard pending).
        self.daemon_deferred_file: str = os.environ.get("B3_DEFERRED_FILE", str(Path(self.b3_data_dir) / ".daemon_deferred"))
        self.bootstrap_cmd_file: str = os.environ.get("BOOTSTRAP_CMD_FILE", str(Path(self.b3_data_dir) / "bootstrap.cmd"))
        self.bootstrap_progress_file: str = os.environ.get("BOOTSTRAP_PROGRESS_FILE", str(Path(self.b3_data_dir) / "bootstrap.progress.json"))
        # Last known explorer tip height (written by the monitor; read by the
        # chain summary for an honest sync-progress number).
        self.explorer_tip_file: str = os.environ.get("EXPLORER_TIP_FILE", str(Path(self.b3_data_dir) / "explorer_tip_height"))
        # Setup wizard: user chose sync-from-scratch -> start the node.
        self.start_node_cmd_file: str = os.environ.get("START_NODE_CMD_FILE", str(Path(self.b3_data_dir) / "start-node.cmd"))
        self.wizard_marker_file: str = os.environ.get("WIZARD_MARKER_FILE", str(Path(self.b3_data_dir) / ".wizard_complete"))
        self.bootstrap_manifest_url: str = os.environ.get(
            "BOOTSTRAP_MANIFEST_URL", "https://explorer.b3hive.io/bootstraps/manifest.json")
        # Vault key for the unattended-staking passphrase store (S5).
        # Operator-managed env var is the recommended source; when empty a
        # generated key file <data>/.vault.key (0400) is used.
        self.wallet_vault_key: str = os.environ.get("WALLET_VAULT_KEY", "")
        # Safety: block wallet actions if /data is not a persistent mount.
        # Set to true ONLY for testing — never in production with real funds.
        self.allow_ephemeral_data: bool = os.environ.get(
            "ALLOW_EPHEMERAL_DATA", "false").strip().lower() == "true"
        # UI/backend version — injected at Docker build time from `git describe`
        # (Dockerfile ARG B3_APP_VERSION). "dev" outside a build.
        self.app_version: str = os.environ.get("B3_APP_VERSION", "dev")
        # Expert console: operator-declared extra methods (comma-separated).
        # Still enforced by the RPC allowlist choke point underneath.
        self.extra_console_methods: str = os.environ.get("EXTRA_CONSOLE_METHODS", "")
        # Console IP gate: full console access only from these networks
        # (comma-separated CIDRs or single IPs). Default: local machine
        # only (loopback + Docker bridge nets). "*" disables the gate.
        self.console_networks: str = os.environ.get("CONSOLE_NETWORKS", "127.0.0.1/8,::1/128,172.16.0.0/12")
        # Daemon stdout log (entrypoint redirects -printtoconsole here; a tail
        # mirror keeps `docker logs` working). Backend tails this for the UI.
        self.daemon_log_file: str = os.environ.get(
            "B3_DAEMON_LOG", str(Path(self.b3_data_dir) / "daemon.log"))


settings = Settings()
