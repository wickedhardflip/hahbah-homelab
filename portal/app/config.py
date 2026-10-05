"""Portal settings, read once from environment variables."""
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host_proc: Path
    host_sys: Path
    disks: tuple            # ((label, path_inside_container), ...)
    nic: str
    eero_igd_url: str
    deploy_log: Path
    apps_file: Path
    cookie_name: str
    cookie_domain: str | None
    cookie_secure: bool
    session_days: int
    collect_interval: int
    base_domain: str
    home_url: str
    tautulli_url: str = ""
    home_tz: str = "America/New_York"
    tautulli_api_key: str = field(default="", repr=False)   # never in repr/logs
    media_dir: Path = Path("/app/media")              # the About page video (kept out of git)
    outbox_dir: Path | None = None                    # emails for the collector to send (None = no email)
    alert_to: str = ""                                # where alerts and the digest go (from secrets/alerts.env)
    mail_dir: Path | None = None                      # where recipients.json is written (None = no web-added addresses)
    sports_file: Path | None = None                   # where the team picks are written (the collector reads it)
    weather_dir: Path | None = None                   # where the weather location file is written (the collector reads it)
    collector_dir: Path = Path("/collector")          # the collector container's JSON files (read-only here)
    caddy_host: str = "caddy"
    host_gateway: str = "host.docker.internal"        # how the portal container reaches apps on the host
    ollama_url: str = ""
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)   # never in repr/logs
    oauth_state_secret: str = field(default="", repr=False)     # signs the 10-minute hl_oauth cookie

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


def load_settings() -> Settings:
    env = os.environ.get
    disks = tuple(tuple(p.split("=", 1)) for p in env("PORTAL_DISKS", "/=/app/data").split(",") if "=" in p)
    return Settings(
        data_dir=Path(env("PORTAL_DATA_DIR", "/app/data")),
        host_proc=Path(env("PORTAL_HOST_PROC", "/host/proc")),
        host_sys=Path(env("PORTAL_HOST_SYS", "/host/sys")),
        disks=disks,
        nic=env("PORTAL_NIC", "enp2s0"),
        eero_igd_url=env("PORTAL_EERO_IGD", "http://192.168.4.1:1900/igd.xml"),
        deploy_log=Path(env("PORTAL_DEPLOY_LOG", "/host/deploy.log")),
        apps_file=Path(env("PORTAL_APPS_FILE", "/app/apps.yaml")),
        cookie_name="hl_session",
        cookie_domain=env("PORTAL_COOKIE_DOMAIN") or None,
        cookie_secure=env("PORTAL_COOKIE_SECURE", "1") == "1",
        session_days=int(env("PORTAL_SESSION_DAYS", "30")),
        collect_interval=int(env("PORTAL_COLLECT_INTERVAL", "30")),
        base_domain=env("PORTAL_BASE_DOMAIN", "hahbah.com"),
        home_url=env("PORTAL_HOME_URL", "https://home.hahbah.com").rstrip("/"),
        tautulli_url=env("TAUTULLI_URL", "").rstrip("/"),
        home_tz=env("PORTAL_TZ", "America/New_York"),
        tautulli_api_key=env("TAUTULLI_API_KEY", ""),
        media_dir=Path(env("PORTAL_MEDIA_DIR", "/app/media")),
        outbox_dir=Path(env("PORTAL_OUTBOX")) if env("PORTAL_OUTBOX") else None,
        alert_to=env("ALERT_EMAIL_TO", "").strip(),
        mail_dir=Path(env("PORTAL_MAIL_DIR")) if env("PORTAL_MAIL_DIR") else None,
        sports_file=Path(env("PORTAL_SPORTS_FILE")) if env("PORTAL_SPORTS_FILE") else None,
        weather_dir=Path(env("PORTAL_WEATHER_DIR")) if env("PORTAL_WEATHER_DIR") else None,
        collector_dir=Path(env("PORTAL_COLLECTOR_DIR", "/collector")),
        caddy_host=env("PORTAL_CADDY_HOST", "caddy"),
        host_gateway=env("PORTAL_HOST_GATEWAY", "host.docker.internal"),
        ollama_url=env("OLLAMA_URL", ""),
        google_client_id=env("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=env("GOOGLE_CLIENT_SECRET", "").strip(),
        oauth_state_secret=env("PORTAL_STATE_SECRET", ""),
    )
