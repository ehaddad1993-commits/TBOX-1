"""Gateway configuration (JSON file, overridable with TBOX_GATEWAY_* environment variables)."""
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GatewayConfig:
    server_url: str = "http://127.0.0.1:8080"
    token: str = ""
    state_dir: Path = Path("gateway_data")
    heartbeat_s: float = 10.0
    config_refresh_s: float = 15.0
    max_batch: int = 500
    max_buffer_rows: int = 2_000_000
    verify_tls: bool = True
    ca_file: str = ""
    log_level: str = "INFO"
    request_timeout_s: float = 15.0
    allow_writes: bool = False   # local safety switch: the plant side must opt in to remote writes

    @classmethod
    def load(cls, path=None):
        c = cls()
        if path:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(c, k):
                    setattr(c, k, Path(v) if k == "state_dir" else v)
        e = os.environ
        c.server_url = e.get("TBOX_GATEWAY_SERVER_URL", c.server_url).rstrip("/")
        c.token = e.get("TBOX_GATEWAY_TOKEN", c.token)
        if e.get("TBOX_GATEWAY_ALLOW_WRITES"):
            c.allow_writes = e["TBOX_GATEWAY_ALLOW_WRITES"].strip().lower() in ("1", "true", "yes", "on")
        if e.get("TBOX_GATEWAY_STATE_DIR"):
            c.state_dir = Path(e["TBOX_GATEWAY_STATE_DIR"])
        if not c.token:
            raise SystemExit("No gateway token configured (token in the JSON file or TBOX_GATEWAY_TOKEN).")
        if c.server_url.startswith("http://") and not any(h in c.server_url for h in ("127.0.0.1", "localhost")):
            import logging
            logging.getLogger("tbox.gateway").warning(
                "server_url uses plain HTTP to a remote host. Use HTTPS so the token and data are encrypted in transit.")
        c.state_dir.mkdir(parents=True, exist_ok=True)
        return c
