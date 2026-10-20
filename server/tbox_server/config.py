"""Server configuration. Everything is overridable with TBOX_* environment variables."""
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(v, default):
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"          # bind to localhost by default; expose via a TLS reverse proxy
    port: int = 8080
    secret: str = ""
    admin_user: str = "admin"
    admin_password: str = ""
    seed_demo: bool = True
    run_simulator: bool = True
    token_ttl_s: int = 8 * 3600
    max_body: int = 10 * 1024 * 1024
    login_max_failures: int = 5
    login_window_s: int = 300
    trust_proxy: bool = False         # honour X-Forwarded-For (only behind your own proxy)
    static_dir: Path = field(default_factory=lambda: Path(__file__).parent / "static")
    migrations_dir: Path = field(default_factory=lambda: Path(__file__).parent / "migrations")
    profiles_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2] / "profiles")

    @property
    def db_path(self):
        return self.data_dir / "tbox.db"

    @property
    def backup_dir(self):
        return self.data_dir / "backups"

    @property
    def log_dir(self):
        return self.data_dir / "logs"

    @classmethod
    def from_env(cls, **overrides):
        e = os.environ
        c = cls()
        c.data_dir = Path(e.get("TBOX_DATA_DIR", c.data_dir))
        c.host = e.get("TBOX_HOST", c.host)
        c.port = int(e.get("TBOX_PORT") or e.get("PORT") or c.port)  # PORT is set by Render/Heroku-style hosts
        c.secret = e.get("TBOX_SECRET", "")
        c.admin_user = e.get("TBOX_ADMIN_USER", c.admin_user)
        c.admin_password = e.get("TBOX_ADMIN_PASSWORD", "")
        c.seed_demo = _bool(e.get("TBOX_SEED_DEMO"), c.seed_demo)
        c.run_simulator = _bool(e.get("TBOX_SIMULATOR"), c.run_simulator)
        c.trust_proxy = _bool(e.get("TBOX_TRUST_PROXY"), c.trust_proxy)
        c.token_ttl_s = int(e.get("TBOX_TOKEN_TTL_S", c.token_ttl_s))
        if e.get("TBOX_PROFILES_DIR"):
            c.profiles_dir = Path(e["TBOX_PROFILES_DIR"])
        for k, v in overrides.items():
            setattr(c, k, v)
        return c

    def ensure_dirs(self):
        for d in (self.data_dir, self.backup_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)
        if not self.secret:
            keyfile = self.data_dir / "secret.key"
            if keyfile.exists():
                self.secret = keyfile.read_text().strip()
            else:
                self.secret = secrets.token_urlsafe(48)
                keyfile.write_text(self.secret)
                try:
                    os.chmod(keyfile, 0o600)
                except OSError:
                    pass
