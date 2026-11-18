from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

DEFAULT_REDIRECTS = [
    "https://chatgpt.com/",
    "https://chat.openai.com/",
    "https://claude.ai/",
    "https://claude.com/",
    "http://localhost",
    "http://127.0.0.1",
]


@dataclass
class Settings:
    db_path: str = "data/commons.db"
    public_url: str = "http://localhost:8000"
    port: int = 8000
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    redirect_allowlist: list[str] = field(default_factory=lambda: list(DEFAULT_REDIRECTS))
    dispatch_interval: float = 2.0
    cookie_secure: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            db_path=os.getenv("COMMONS_DB", "data/commons.db"),
            public_url=os.getenv("COMMONS_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
            port=int(os.getenv("COMMONS_PORT", "8000")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET") or None,
            dispatch_interval=float(os.getenv("COMMONS_DISPATCH_INTERVAL", "2")),
        )
        extra = os.getenv("COMMONS_REDIRECT_ALLOWLIST")
        if extra:
            s.redirect_allowlist = [x.strip() for x in extra.split(",") if x.strip()]
        s.cookie_secure = s.public_url.startswith("https://")
        return s

    @property
    def allowed_hosts(self) -> list[str]:
        host = urlparse(self.public_url).netloc
        return sorted({host, f"127.0.0.1:{self.port}", f"localhost:{self.port}", "localhost", "testserver"})
