"""Standalone GDrive upload with dedicated proxy (.gupp only, no gdrive Worker dependency)."""

import asyncio
import math
import os
import pickle  # nosec
import time
from datetime import datetime
from mimetypes import guess_type
from typing import Optional
from urllib.parse import urlparse

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from httplib2 import Http, ProxyInfo, socks
from oauth2client.client import OAuth2Credentials

from userge import Message, config, get_collection, pool, userge
from userge.plugins.misc.download import tg_download, url_download
from userge.utils import humanbytes, is_url, time_formatter
from userge.utils.exceptions import ProcessCanceled

_SAVED_SETTINGS = get_collection("CONFIGS")
_GDRIVE_DOC_ID = "GDRIVE"
G_DRIVE_FILE_LINK = "📄 <a href='https://drive.google.com/open?id={}'>{}</a> __({})__"


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def _get_proxy_url() -> Optional[str]:
    # fallback default if env vars are missing
    return (
        os.getenv("GDRIVE_UPLOAD_PROXY")
        or "socks5://127.0.0.1:1088"
    )

def _build_proxy_http(proxy_url: Optional[str], timeout: int = 120) -> Http:
    if not proxy_url:
        return Http(timeout=timeout)

    u = urlparse(proxy_url)
    scheme = (u.scheme or "").lower()
    if scheme.startswith("socks5"):
        ptype = socks.PROXY_TYPE_SOCKS5
    elif scheme.startswith("socks4"):
        ptype = socks.PROXY_TYPE_SOCKS4
    else:
        ptype = socks.PROXY_TYPE_HTTP

    pinfo = ProxyInfo(
        proxy_type=ptype,
        proxy_host=u.hostname,
        proxy_port=u.port,
        proxy_user=u.username,
        proxy_pass=u.password,
    )
    return Http(proxy_info=pinfo, timeout=timeout)


async def _load_creds_from_userge_db() -> Optional[OAuth2Credentials]:
    doc = await _SAVED_SETTINGS.find_one({"_id": _GDRIVE_DOC_ID}, {"creds": 1})
    if not doc or "creds" not in doc:
        return None
    try:
        creds = pickle.loads(doc["creds"])  # nosec
        if isinstance(creds, OAuth2Credentials):
            return creds
    except Exception:
        return None
    return None


def _build_service_with_creds_and_proxy(creds: OAuth2Credentials, proxy_url: Optional[str]):
    # IMPORTANT:
    # creds are already valid from gdrive plugin runtime/DB.
    # We do NOT force refresh through proxy to avoid bad proxy OAuth redirects.
    authed_http = creds.authorize(_build_proxy_http(proxy_url))
    return build("drive", "v3", http=authed_http, cache_discovery=False)


def _set_public_permission(service, file_id: str) -> None:
    service.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"},
        supportsTeamDrives=True
    ).execute()


def _upload_file_with_progress(
    service,
    file_path: str,
    parent_id: str,
    progress_cb,
    is_canceled_cb
) -> str:
    if is_canceled_cb():
        raise ProcessCanceled

    mime_type = guess_type(file_path)[0] or "application/octet-stream"
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)

    body = {
        "name": file_name,
        "mimeType": mime_type,
        "description": "Uploaded using Userge .gupp standalone",
    }
    if parent_id:
        body["parents"] = [parent_id]

    chunk_mb = int(os.getenv("GUP_CHUNK_MB", "50"))  # match Userge default style
    media = MediaFileUpload(
        file_path,
        mimetype=mime_type,
        chunksize=max(1, chunk_mb) * 1024 * 1024,
        resumable=True
    )

    req = service.files().create(body=body, media_body=media, supportsTeamDrives=True)

    c_time = time.time()
    response = None
    while response is None:
        if is_canceled_cb():
            raise ProcessCanceled
        status, response = req.next_chunk(num_retries=5)
        if status:
            total = status.total_size or file_size
            uploaded = status.resumable_progress
            diff = max(time.time() - c_time, 0.001)
            speed = round(uploaded / diff, 2)
            percentage = (uploaded / total) * 100 if total else 0
            eta = round((total - uploaded) / speed) if speed and total > uploaded else 0
            progress_cb(file_name, total, uploaded, percentage, speed, eta)

    return response.get("id")


@userge.on_cmd("gupp", about={
    "header": "Upload to GDrive using dedicated proxy (standalone)",
    "usage": "{tr}gupp <file_path_or_url> OR reply to media",
    "description": "Uses proxy only for upload transport. Uses creds saved by official .gsetup/.gconf."
}, check_downpath=True)
async def gupp_(message: Message):
    proxy_url = _get_proxy_url()
    await message.edit(f"`Using proxy: {proxy_url}`")
    if not proxy_url:
        await message.err("Set `GUP_PROXY` or `GDRIVE_UPLOAD_PROXY` first.")
        await message.err("Set `GUP_PROXY` or `GDRIVE_UPLOAD_PROXY` first.")
        return

    creds = await _load_creds_from_userge_db()
    if not creds:
        await message.err("No GDrive creds in DB. Run official `.gsetup` + `.gconf` first.")
        return

    # If token expired, do NOT refresh via proxy here (causes your redirect error on bad proxy).
    if creds.access_token_expired:
        await message.err(
            "Stored token is expired. Run official `.gsetup`/`.gconf` (or `.gup` once) to refresh creds, then retry `.gupp`."
        )
        return

    parent_id = os.getenv("G_DRIVE_PARENT_ID", "").strip()
    is_td = _env_bool("G_DRIVE_IS_TD", False)

    # resolve input
    try:
        replied = message.reply_to_message
        is_input_url = is_url(message.input_str)
        dl_loc = ""

        if replied and replied.media:
            dl_loc, _ = await tg_download(message, replied)
            source_path = dl_loc
            is_temp = True
        elif is_input_url:
            dl_loc, _ = await url_download(message, message.input_str)
            source_path = dl_loc
            is_temp = True
        else:
            source_path = message.input_str
            is_temp = False
    except ProcessCanceled:
        await message.canceled()
        return
    except Exception as e:
        await message.err(f"Download failed: {e}")
        return

    if not source_path or not os.path.exists(source_path):
        await message.err("invalid file path provided?")
        return
    if os.path.isdir(source_path):
        await message.err("This `.gupp` supports file upload only, not folders.")
        return

    await message.edit("`Loading standalone GDrive Upload via proxy...`")

    progress_text = {"value": None}
    finished = {"value": False}
    canceled = {"value": False}
    output = {"value": None}
    start_t = datetime.now()

    def _cancel():
        canceled["value"] = True

    def _is_canceled():
        return canceled["value"]

    def _progress_cb(name, total, uploaded, percent, speed, eta):
        done = math.floor(percent / 5)
        bar = (
            "".join(config.FINISHED_PROGRESS_STR for _ in range(done)) +
            "".join(config.UNFINISHED_PROGRESS_STR for _ in range(20 - done))
        )
        progress_text["value"] = (
            "__Uploading to GDrive (Proxy)...__\n"
            f"```\n[{bar}]({round(percent, 2)}%)```\n"
            f"**File Name** : `{name}`\n"
            f"**File Size** : `{humanbytes(total)}`\n"
            f"**Uploaded** : `{humanbytes(uploaded)}`\n"
            f"**Speed** : `{humanbytes(speed)}/s`\n"
            f"**ETA** : `{time_formatter(eta)}`"
        )

    def _runner():
        try:
            service = _build_service_with_creds_and_proxy(creds, proxy_url)
            file_id = _upload_file_with_progress(
                service=service,
                file_path=source_path,
                parent_id=parent_id,
                progress_cb=_progress_cb,
                is_canceled_cb=_is_canceled,
            )
            if not is_td:
                _set_public_permission(service, file_id)
            file_name = os.path.basename(source_path)
            file_size = os.path.getsize(source_path)
            output["value"] = G_DRIVE_FILE_LINK.format(file_id, file_name, humanbytes(file_size))
        except ProcessCanceled:
            output["value"] = "`Process Canceled!`"
        except HttpError as h_e:
            output["value"] = f"**ERROR** : `{h_e._get_reason()}`"  # pylint: disable=protected-access
        except Exception as e:
            output["value"] = f"**ERROR** : `{e}`"
        finally:
            finished["value"] = True

    pool.submit_thread(_runner)

    with message.cancel_callback(_cancel):
        while not finished["value"]:
            if progress_text["value"] is not None:
                await message.edit(progress_text["value"])
            await asyncio.sleep(config.Dynamic.EDIT_SLEEP_TIMEOUT)

    if is_temp and os.path.exists(source_path):
        try:
            os.remove(source_path)
        except Exception:
            pass

    m_s = (datetime.now() - start_t).seconds
    if isinstance(output["value"], str) and output["value"].startswith("**ERROR**"):
        await message.edit(output["value"], disable_web_page_preview=True)
    elif output["value"] == "`Process Canceled!`":
        await message.edit(output["value"])
    elif output["value"]:
        await message.edit(
            f"**Uploaded Successfully** __in {m_s} seconds__\n\n{output['value']}",
            disable_web_page_preview=True,
            log=__name__,
        )
    else:
        await message.edit("`failed to upload.. check logs?`")
