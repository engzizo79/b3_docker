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
        # SQLite DB (user, TOTP, audit log) — lives on the persistent /data volume.
        self.db_path: str = os.environ.get("B3_DB_PATH", "/data/b3hive.db")
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
        self.recovery_cmd_file: str = os.environ.get("RECOVERY_CMD_FILE", "/data/recovery.cmd")
        # Cookie Secure flag: auto (default, HTTPS-aware via X-Forwarded-Proto), true, false
        self.cookie_secure: str = os.environ.get("COOKIE_SECURE", "auto")
        # Setup wizard / bootstrap
        self.b3_data_dir: str = os.environ.get("B3_DATA_DIR", "/data")
        # Set by the entrypoint while the daemon is deferred (fresh chain, first UI run).
        self.daemon_deferred_file: str = os.environ.get("B3_DEFERRED_FILE", str(Path(self.b3_data_dir) / ".daemon_deferred"))
        self.bootstrap_cmd_file: str = os.environ.get("BOOTSTRAP_CMD_FILE", "/data/bootstrap.cmd")
        self.bootstrap_progress_file: str = os.environ.get("BOOTSTRAP_PROGRESS_FILE", "/data/bootstrap.progress.json")
        # Last known explorer tip height (written by the monitor; read by the
        # chain summary for an honest sync-progress number).
        self.explorer_tip_file: str = os.environ.get("EXPLORER_TIP_FILE", "/data/explorer_tip_height")
        # Setup wizard: user chose sync-from-scratch -> start the node.
        self.start_node_cmd_file: str = os.environ.get("START_NODE_CMD_FILE", "/data/start-node.cmd")
        self.wizard_marker_file: str = os.environ.get("WIZARD_MARKER_FILE", "/data/.wizard_complete")
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


settings = Settings()
