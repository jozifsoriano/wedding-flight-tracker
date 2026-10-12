import logging

import httpx

from .config import Config

log = logging.getLogger(__name__)


def notify(cfg: Config, title: str, message: str, url: str = "") -> None:
    a = cfg.alerts
    try:
        if a.ntfy_url:
            headers = {"Title": title.encode("utf-8"), "Tags": "airplane"}
            if url:
                headers["Click"] = url
            httpx.post(a.ntfy_url, content=message.encode("utf-8"), headers=headers, timeout=15)
        if a.discord_webhook:
            httpx.post(a.discord_webhook, json={"content": f"**{title}**\n{message}\n{url}".strip()}, timeout=15)
    except Exception:
        log.exception("alert delivery failed")
