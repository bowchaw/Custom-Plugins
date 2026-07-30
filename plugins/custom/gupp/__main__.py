"""gdrive upload with dedicated proxy"""

import os
from contextlib import contextmanager

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
    previous = {key: os.environ.get(key) for key in _PROXY_ENV_KEYS}
    try:
        for key in _PROXY_ENV_KEYS:
            os.environ[key] = proxy_url
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@userge.on_cmd("gupp", about={
    'header': "Upload to GDrive using proxy",
    'usage': "{tr}gupp <file_path_or_url>",
    'description': "Uses GUP_PROXY or GDRIVE_UPLOAD_PROXY only for this upload command"
})
async def gupp_(message: Message):
    """upload to gdrive with proxy"""
    proxy_url = os.environ.get("GUP_PROXY") or os.environ.get("GDRIVE_UPLOAD_PROXY")
    if not proxy_url:
        await message.err("Set `GUP_PROXY` or `GDRIVE_UPLOAD_PROXY` to use `.gupp`.")
        return
    with _temporary_proxy(proxy_url):
        await Worker(message).upload()
