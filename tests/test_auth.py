import time

import pytest
import yaml

from app import auth, main
from tests.test_api import client  # noqa: F401

PASSWORD = "correct horse battery"


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    auth._failures.clear()


def test_hash_roundtrip_and_format():
    stored = auth.hash_password("pw-123456789012")
    assert stored.startswith("scrypt:") and "$" not in stored    # safe in a docker env_file
    assert auth.verify_password("pw-123456789012", stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.verify_password("x", "garbage") and not auth.verify_password("x", "md5:1:2:3:4:5")


def test_open_when_no_password_configured(client):
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/auth/status").json() == {"enabled": False, "logged_in": False}


def test_everything_needs_a_session_once_enabled(client, secured):
    assert client.get("/api/overview").status_code == 401
    assert client.post("/api/run").status_code == 401
    assert client.post("/api/actions/x/approve").status_code == 401
    assert client.get("/static/app.js").status_code == 401
    assert client.get("/").text.count("login-form") == 1           # sign-in page at the same URL
    assert client.get("/healthz").status_code == 200               # Docker healthcheck
    assert client.get("/static/style.css").status_code == 200


def test_login_logout_flow(client, secured):
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/auth/status").json() == {"enabled": True, "logged_in": True}
    client.post("/api/auth/logout")
    assert client.get("/api/overview").status_code == 401


def test_secure_cookie_behind_https_proxy(client, secured):
    r = client.post("/api/auth/login", json={"password": PASSWORD}, headers={"x-forwarded-proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_forged_tampered_and_expired_sessions_are_rejected(client, secured):
    good = auth.make_session()
    assert auth.valid_session(good)
    expires, nonce, sig = good.split(".")
    assert not auth.valid_session(f"{int(expires) + 999}.{nonce}.{sig}")
    assert not auth.valid_session(f"{expires}.{nonce}.AAAA") and not auth.valid_session("junk") and not auth.valid_session(None)
    assert not auth.valid_session(auth.make_session(now=time.time() - auth.SESSION_TTL - 5))
    client.cookies.set(auth.COOKIE, "1.2.3")
    assert client.get("/api/overview").status_code == 401


def test_session_signed_with_another_secret_is_rejected(secured, monkeypatch):
    token = auth.make_session()
    monkeypatch.setenv("SESSION_SECRET", "different")
    assert not auth.valid_session(token)


def test_failed_logins_are_throttled(client, secured):
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/api/auth/login", json={"password": "bad"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429   # even the right one


# --- migrating config.yaml destinations into the app -----------------------------------------------

@pytest.fixture
def migratable(client, monkeypatch, cfg, tmp_path):
    raw = cfg.model_dump()
    raw["honeymoon"] = [{"id": "tokyo", "name": "Tokyo", "country": "Japan", "airports": ["HND"],
                         "fly_home_from": "SGN", "tip": "Nonstop home."}]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    monkeypatch.setattr(main, "CONFIG_PATH", path)
    monkeypatch.delenv("GUI_DESTINATIONS_PATH", raising=False)
    return client, path


def test_migration_moves_config_destinations_with_all_fields(migratable):
    client, config_path = migratable
    before = config_path.read_text()
    assert client.get("/api/migration").json() == {"migrated": False, "pending": 1}

    assert client.post("/api/migrate-config").json()["migrated"] == 1
    assert config_path.read_text() == before                         # config.yaml untouched
    tokyo = main.cfg.honeymoon[0]
    assert (tokyo.id, tokyo.fly_home_from, tokyo.tip) == ("tokyo", "SGN", "Nonstop home.")
    assert client.get("/api/migration").json() == {"migrated": True, "pending": 0}
    assert client.post("/api/migrate-config").json()["already"] is True    # idempotent, no duplicates
    assert [d.id for d in main.cfg.honeymoon] == ["tokyo"]


def test_migrated_destinations_can_be_edited_and_removed_and_stay_removed(migratable):
    client, config_path = migratable
    client.post("/api/migrate-config")
    changed = main._update_destination("tokyo", {"extended_stay": True, "airports": ["NRT"]})
    assert changed["destination"]["fly_home_from"] == "SGN"          # untouched fields survive the edit
    assert "start_dates" not in changed["destination"]               # still uses the global dates
    main._remove_destination("tokyo")
    assert main.cfg.honeymoon == []
    main.cfg = main.load_config(config_path)                         # a reload / restart must not resurrect it
    assert main.cfg.honeymoon == []


def test_migration_keeps_existing_app_destinations(migratable):
    client, _ = migratable
    client.post("/api/destinations", json={"name": "Seoul", "country": "South Korea", "airports": ["ICN"],
                                           "start_date": "2027-03-15", "end_date": "2027-03-19"})
    assert [d.id for d in main.cfg.honeymoon] == ["tokyo", "seoul"]
    client.post("/api/migrate-config")
    assert [d.id for d in main.cfg.honeymoon] == ["tokyo", "seoul"]
    # a legacy list file still loads (no migration flag) and the flag round-trips through later writes
    assert client.post("/api/destinations", json={"name": "Osaka", "country": "Japan", "airports": ["KIX"],
                                                  "start_date": "2027-03-15", "end_date": "2027-03-19"}).status_code == 201
    assert client.get("/api/migration").json()["migrated"] is True
