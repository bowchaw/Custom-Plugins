"""gdrive upload with dedicated proxy"""

import os
from contextlib import contextmanager
from typing import Dict, Optional

import requests

from userge import Message, userge
from userge.plugins.misc.gdrive.__main__ import Worker

_PROXY_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


@contextmanager
def _temporary_proxy(proxy_url: str):
    """
    Temporarily route network via proxy for this command execution.
    Applies to env-based consumers and requests' explicit proxy resolution.
    """
    previous_env: Dict[str, Optional[str]] = {k: os.environ.get(k) for k in _PROXY_ENV_KEYS}
    old_merge = requests.sessions.Session.merge_environment_settings

    def _patched_merge_environment_settings(self, url, proxies, stream, verify, cert):
        # enforce proxy even if caller passes no proxies
        if proxies is None:
            proxies = {}
        proxies = dict(proxies)  # copy
        proxies.setdefault("http", proxy_url)
        proxies.setdefault("https", proxy_url)
        return old_merge(self, url, proxies, stream, verify, cert)

    try:
        # 1) env based routing
        for key in _PROXY_ENV_KEYS:
            os.environ[key] = proxy_url

        # 2) requests fallback/enforcement
        requests.sessions.Session.merge_environment_settings = _patched_merge_environment_settings
        yield
    finally:
        # restore requests behavior
        requests.sessions.Session.merge_environment_settings = old_merge

        # restore env
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@userge.on_cmd("gupp", about={
    "header": "Upload to GDrive using proxy",
    "usage": "{tr}gupp <file_path_or_url>",
    "description": "Uses GUP_PROXY or GDRIVE_UPLOAD_PROXY only for this upload command"
})
async def gupp_(message: Message):
    """upload to gdrive with proxy"""
    proxy_url = os.environ.get("GUP_PROXY") or os.environ.get("GDRIVE_UPLOAD_PROXY")
    if not proxy_url:
        await message.err("Set `GUP_PROXY` or `GDRIVE_UPLOAD_PROXY` to use `.gupp`.")
        return

    # keep whole flow inside proxy context (url/tg download + gdrive upload thread)
    with _temporary_proxy(proxy_url):
        await Worker(message).upload()
