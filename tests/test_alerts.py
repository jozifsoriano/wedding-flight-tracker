from app import alerts
from tests.conftest import make_cfg


def capture_posts(monkeypatch, fail=False):
    posts = []

    def post(url, **kw):
        posts.append((url, kw))
        if fail:
            raise ConnectionError("down")

    monkeypatch.setattr(alerts.httpx, "post", post)
    return posts


def test_no_channels_configured_sends_nothing(monkeypatch, cfg):
    posts = capture_posts(monkeypatch)
    alerts.notify(cfg, "t", "m")
    assert posts == []


def test_ntfy_and_discord(monkeypatch):
    cfg = make_cfg(alerts={"ntfy_url": "https://ntfy.example.test/topic",
                           "discord_webhook": "https://discord.example.test/hook"})
    posts = capture_posts(monkeypatch)
    alerts.notify(cfg, "✈️ New low", "$2,000 for 2", "https://flights.example.test")

    (ntfy_url, ntfy), (discord_url, discord) = posts
    assert ntfy_url == "https://ntfy.example.test/topic"
    assert ntfy["content"] == "$2,000 for 2".encode()
    assert ntfy["headers"] == {"Title": "✈️ New low".encode(), "Tags": "airplane",
                               "Click": "https://flights.example.test"}
    assert discord_url == "https://discord.example.test/hook"
    assert discord["json"] == {"content": "**✈️ New low**\n$2,000 for 2\nhttps://flights.example.test"}


def test_ntfy_without_url_has_no_click_header(monkeypatch):
    cfg = make_cfg(alerts={"ntfy_url": "https://ntfy.example.test/topic"})
    posts = capture_posts(monkeypatch)
    alerts.notify(cfg, "t", "m")
    assert "Click" not in posts[0][1]["headers"]


def test_delivery_failure_is_swallowed(monkeypatch):
    cfg = make_cfg(alerts={"ntfy_url": "https://ntfy.example.test/topic"})
    capture_posts(monkeypatch, fail=True)
    alerts.notify(cfg, "t", "m")   # must not raise: a dead ntfy server shouldn't break a run
