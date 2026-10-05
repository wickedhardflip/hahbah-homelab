import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeBuilder:
    """Stands in for SnapshotBuilder so tests never touch /proc or the network."""

    def __init__(self, snapshot=None):
        self.snapshot = snapshot or {"schema": "homelab.snapshot/v1", "nodes": [], "links": [], "alerts": []}
        self.calls = 0

    def build(self):
        self.calls += 1
        return self.snapshot


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data", host_proc=tmp_path / "proc", host_sys=tmp_path / "sys",
        disks=(("/", str(tmp_path)),), nic="enp2s0", eero_igd_url="http://127.0.0.1:9/igd.xml",
        deploy_log=tmp_path / "deploy.log", apps_file=tmp_path / "apps.yaml",
        cookie_name="hl_session", cookie_domain=None, cookie_secure=False, session_days=30, collect_interval=30,
        base_domain="hahbah.com", home_url="https://home.hahbah.com",
    )


@pytest.fixture
def builder():
    return FakeBuilder()


@pytest.fixture
def app(settings, builder):
    return create_app(settings, builder=builder, collect=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def make_user(app, username="alex", password="correct horse battery"):
    from app.auth import hash_password
    from app.models import User
    with app.state.SessionLocal() as db:
        db.add(User(username=username, password_hash=hash_password(password)))
        db.commit()


def sign_in(client, username="alex", password="correct horse battery"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
