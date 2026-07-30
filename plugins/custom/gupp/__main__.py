"""Standalone GDrive upload with dedicated proxy (.gupp only, no gdrive plugin dependency)."""

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

# =========================
# Config env vars required:
# =========================
# G_DRIVE_CLIENT_ID
# G_DRIVE_CLIENT_SECRET
# G_DRIVE_REFRESH_TOKEN
#
# Optional:
# G_DRIVE_PARENT_ID
# G_DRIVE_IS_TD=true/false
# GUP_PROXY or GDRIVE_UPLOAD_PROXY   (required for .gupp behavior)
# GUP_CHUNK_MB (default 64)
#
# NOTE:
# This plugin intentionally supports ONLY upload command `.gupp`.
# It does not depend on UsergeTeam gdrive plugin internals.

OAUTH_SCOPE = "https://www.googleapis.com/auth/drive"
TOKEN_URI = "https://oauth2.googleapis.com/token"

_GDRIVE_COLLECTION = get_collection("CONFIGS")
_DOC_ID = "GUPP_STANDALONE_CREDS"


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def _get_proxy_url() -> Optional[str]:
    return os.getenv("GUP_PROXY") or os.getenv("GDRIVE_UPLOAD_PROXY")


def _build_proxy_http(proxy_url: Optional[str], timeout: int = 120) -> Http:
    if not proxy_url:
        return Http(timeout=timeout)

    p = urlparse(proxy_url)
    scheme = (p.scheme or "").lower()

    if scheme.startswith("socks5"):
        proxy_type = socks.PROXY_TYPE_SOCKS5
    elif scheme.startswith("socks4"):
        proxy_type = socks.PROXY_TYPE_SOCKS4
    else:
        proxy_type = socks.PROXY_TYPE_HTTP

    pinfo = ProxyInfo(
        proxy_type=proxy_type,
        proxy_host=p.hostname,
        proxy_port=p.port,
        proxy_user=p.username,
        proxy_pass=p.password,
    )
    return Http(proxy_info=pinfo, timeout=timeout)


async def _load_cached_creds() -> Optional[OAuth2Credentials]:
    doc = await _GDRIVE_COLLECTION.find_one({"_id": _DOC_ID}, {"creds": 1})
    if not doc or "creds" not in doc:
        return None
    try:
        return pickle.loads(doc["creds"])  # nosec
    except Exception:
        return None


async def _save_cached_creds(creds: OAuth2Credentials) -> None:
    await _GDRIVE_COLLECTION.update_one(
        {"_id": _DOC_ID},
        {"$set": {"creds": pickle.dumps(creds)}},  # nosec
        upsert=True,
    )


def _creds_from_env() -> OAuth2Credentials:
    client_id = os.getenv("G_DRIVE_CLIENT_ID")
    client_secret = os.getenv("G_DRIVE_CLIENT_SECRET")
    refresh_token = os.getenv("G_DRIVE_REFRESH_TOKEN")

    if not client_id or not client_secret or not refresh_token:
        raise ValueError(
            "Missing env vars. Required: G_DRIVE_CLIENT_ID, "
            "G_DRIVE_CLIENT_SECRET, G_DRIVE_REFRESH_TOKEN"
        )

    return OAuth2Credentials(
        access_token=None,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        token_expiry=None,
        token_uri=TOKEN_URI,
        user_agent="Userge-GUPP-Standalone",
        revoke_uri=None,
        id_token=None,
        token_response=None,
        scopes=[OAUTH_SCOPE],
        token_info_uri=None,
    )


async def _get_creds(proxy_url: Optional[str]) -> OAuth2Credentials:
    creds = await _load_cached_creds()
    if creds is None:
        creds = _creds_from_env()

    # Refresh token via proxy-aware Http
    http = _build_proxy_http(proxy_url)
    if (not creds.access_token) or creds.access_token_expired:
        await pool.run_in_thread(creds.refresh)(http)
        await _save_cached_creds(creds)

    return creds


def _drive_service(creds: OAuth2Credentials, proxy_url: Optional[str]):
    http = _build_proxy_http(proxy_url)
    authed_http = creds.authorize(http)
    return build("drive", "v3", http=authed_http, cache_discovery=False)


def _set_public_permission(service, file_id: str) -> None:
    service.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"},
        supportsTeamDrives=True,
    ).execute()


def _get_file_link(file_id: str, file_name: str, file_size: int) -> str:
    return (
        f"📄 <a href='https://drive.google.com/open?id={file_id}'>{file_name}</a> "
        f"__({humanbytes(file_size)})__"
    )


def _resolve_upload_source(message: Message):
    """Resolve source from reply media, URL, or local path."""
    async def _inner():
        replied = message.reply_to_message
        is_input_url = is_url(message.input_str)
        dl_loc = ""

        if replied and replied.media:
            dl_loc, _ = await tg_download(message, replied)
            return dl_loc, True

        if is_input_url:
            dl_loc, _ = await url_download(message, message.input_str)
            return dl_loc, True

        return message.input_str, False

    return _inner()


def _upload_file_with_progress(
    service,
    file_path: str,
    parent_id: str,
    progress_cb,
    is_canceled_cb,
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

    chunk_mb = int(os.getenv("GUP_CHUNK_MB", "64"))
    chunk_size = max(1, chunk_mb) * 1024 * 1024

    media = MediaFileUpload(
        file_path,
        mimetype=mime_type,
        chunksize=chunk_size,
        resumable=True,
    )

    req = service.files().create(
        body=body,
        media_body=media,
        supportsTeamDrives=True,
    )

    started = time.time()
    response = None

    while response is None:
        if is_canceled_cb():
            raise ProcessCanceled

        status, response = req.next_chunk(num_retries=5)
        if status:
            total = status.total_size or file_size
            uploaded = status.resumable_progress
            elapsed = max(time.time() - started, 0.001)
            speed = uploaded / elapsed
            percent = (uploaded / total) * 100 if total else 0.0
            eta = int((total - uploaded) / speed) if speed > 0 and total > uploaded else 0
            progress_cb(file_name, total, uploaded, percent, speed, eta)

    return response.get("id")


@userge.on_cmd(
    "gupp",
    about={
        "header": "Upload to GDrive using dedicated proxy (standalone)",
        "usage": "{tr}gupp <file_path_or_url> OR reply to media",
        "description": (
            "Uses GUP_PROXY or GDRIVE_UPLOAD_PROXY for token refresh and upload traffic. "
            "Does not depend on gdrive plugin."
        ),
    },
    check_downpath=True,
)
async def gupp_(message: Message):
    proxy_url = _get_proxy_url()
    if not proxy_url:
        await message.err("Set `GUP_PROXY` or `GDRIVE_UPLOAD_PROXY` first.")
        return

    parent_id = os.getenv("G_DRIVE_PARENT_ID", "").strip()
    is_td = _env_bool("G_DRIVE_IS_TD", False)

    try:
        source_path, is_temp = await _resolve_upload_source(message)
    except ProcessCanceled:
        await message.canceled()
        return
    except Exception as e:
        await message.err(f"Download failed: {e}")
        return

    if not source_path or not os.path.exists(source_path):
        await message.err("Invalid file path/source.")
        return

    if os.path.isdir(source_path):
        await message.err("This standalone `.gupp` supports file upload only (not folders).")
        if is_temp and os.path.exists(source_path):
            try:
                os.remove(source_path)
            except Exception:
                pass
        return

    await message.edit("`Initializing standalone GDrive upload via proxy...`")

    progress_text = {"value": None}
    canceled = {"value": False}
    finished = {"value": False}
    output = {"value": None}
    start_t = datetime.now()

    def _cancel():
        canceled["value"] = True

    def _is_canceled():
        return canceled["value"]

    def _progress_cb(name, total, uploaded, percent, speed, eta):
        bar_done = math.floor(percent / 5)
        bar = (
            "".join(config.FINISHED_PROGRESS_STR for _ in range(bar_done))
            + "".join(config.UNFINISHED_PROGRESS_STR for _ in range(20 - bar_done))
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
            creds = asyncio.run(_get_creds(proxy_url))
            service = _drive_service(creds, proxy_url)

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
            output["value"] = _get_file_link(file_id, file_name, file_size)

        except ProcessCanceled:
            output["value"] = "`Process Canceled!`"
        except HttpError as he:
            output["value"] = f"**ERROR** : `{he._get_reason()}`"  # pylint: disable=protected-access
        except Exception as ex:
            output["value"] = f"**ERROR** : `{ex}`"
        finally:
            finished["value"] = True

    pool.submit_thread(_runner)

    with message.cancel_callback(_cancel):
        while not finished["value"]:
            if progress_text["value"] is not None:
                await message.edit(progress_text["value"])
            await asyncio.sleep(config.Dynamic.EDIT_SLEEP_TIMEOUT)

    # cleanup temp download
    if is_temp and os.path.exists(source_path):
        try:
            os.remove(source_path)
        except Exception:
            pass

    elapsed = (datetime.now() - start_t).seconds
    out = output["value"]

    if out is None:
        await message.edit("`failed to upload.. check logs?`")
    elif out == "`Process Canceled!`":
        await message.edit(out)
    elif out.startswith("**ERROR**"):
        await message.edit(out, disable_web_page_preview=True)
    else:
        await message.edit(
            f"**Uploaded Successfully** __in {elapsed} seconds__\n\n{out}",
            disable_web_page_preview=True,
            log=__name__,
        )
