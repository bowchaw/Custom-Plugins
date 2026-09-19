""" Google Drive Uploader (Isolated Proxy Version) """

import asyncio
import math
import os
import pickle
import time
from datetime import datetime
from mimetypes import guess_type
from urllib.parse import urlparse, quote

import httplib2
import socks
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from userge import userge, Message, config, get_collection, pool
from userge.plugins.misc.download import url_download, tg_download
from userge.utils import humanbytes, time_formatter, is_url
from userge.utils.exceptions import ProcessCanceled

try:
    from userge.plugins.misc import gdrive
except ImportError:
    gdrive = None

_LOG = userge.getLogger(__name__)
_SAVED_SETTINGS = get_collection("CONFIGS")

_CREDS = None
G_DRIVE_DIR_MIME_TYPE = "application/vnd.google-apps.folder"
G_DRIVE_FILE_LINK = "📄 <a href='https://drive.google.com/open?id={}'>{}</a> __({})__"
G_DRIVE_FOLDER_LINK = "📁 <a href='https://drive.google.com/drive/folders/{}'>{}</a> __(folder)__"


@userge.on_start
async def _init() -> None:
    global _CREDS
    result = await _SAVED_SETTINGS.find_one({'_id': 'GDRIVE'}, {'creds': 1})
    _CREDS = pickle.loads(result['creds']) if result else None


def get_parent_id():
    try:
        import sys
        gdrive_main = sys.modules.get('userge.plugins.misc.gdrive.__main__')
        if gdrive_main and gdrive_main._PARENT_ID:
            return gdrive_main._PARENT_ID
    except Exception:
        pass
    return gdrive.G_DRIVE_PARENT_ID if gdrive else ""


class _GDriveProxy:
    def __init__(self) -> None:
        self._parent_id = get_parent_id()
        self._completed = 0
        self._list = 1
        self._progress = None
        self._output = None
        self._is_canceled = False
        self._is_finished = False

    def _cancel(self) -> None:
        self._is_canceled = True

    def _finish(self) -> None:
        self._is_finished = True

    @property
    def _service(self) -> object:
        proxy_url = os.environ.get("GUP_PROXY", "")
        
        if proxy_url:
            parsed = urlparse(proxy_url)
            proxy_type = socks.PROXY_TYPE_HTTP
            if parsed.scheme.startswith("socks5"):
                proxy_type = socks.PROXY_TYPE_SOCKS5
            elif parsed.scheme.startswith("socks4"):
                proxy_type = socks.PROXY_TYPE_SOCKS4
                
            proxy_info = httplib2.ProxyInfo(
                proxy_type=proxy_type,
                proxy_host=parsed.hostname,
                proxy_port=parsed.port,
                proxy_user=parsed.username,
                proxy_pass=parsed.password
            )
            # Increased timeout to 1800s (30 mins) to prevent proxy dropping large files
            http = httplib2.Http(proxy_info=proxy_info, timeout=1800)
        else:
            http = httplib2.Http(timeout=1800)

        if _CREDS:
            if _CREDS.access_token_expired:
                _CREDS.refresh(httplib2.Http())
            http = _CREDS.authorize(http)
            
        return build("drive", "v3", http=http, cache_discovery=False)

    def _set_permission(self, file_id: str) -> None:
        permissions = {'role': 'reader', 'type': 'anyone'}
        self._service.permissions().create(fileId=file_id, body=permissions,
                                           supportsTeamDrives=True).execute()

    def _get_file_path(self, file_id: str, file_name: str) -> str:
        tmp_path = [file_name]
        while True:
            response = self._service.files().get(
                fileId=file_id, fields='parents', supportsTeamDrives=True).execute()
            if not response:
                break
            file_id = response['parents'][0]
            response = self._service.files().get(
                fileId=file_id, fields='name', supportsTeamDrives=True).execute()
            tmp_path.append(response['name'])
        return '/'.join(reversed(tmp_path[:-1]))

    def _get_output(self, file_id: str) -> str:
        file_ = self._service.files().get(
            fileId=file_id, fields="id, name, size, mimeType", supportsTeamDrives=True).execute()
        file_id = file_.get('id')
        file_name = file_.get('name')
        file_size = humanbytes(int(file_.get('size', 0)))
        mime_type = file_.get('mimeType')
        
        if mime_type == G_DRIVE_DIR_MIME_TYPE:
            out = G_DRIVE_FOLDER_LINK.format(file_id, file_name)
        else:
            out = G_DRIVE_FILE_LINK.format(file_id, file_name, file_size)
            
        if gdrive and gdrive.G_DRIVE_INDEX_LINK:
            link = os.path.join(
                gdrive.G_DRIVE_INDEX_LINK.rstrip('/'),
                quote(self._get_file_path(file_id, file_name)))
            if mime_type == G_DRIVE_DIR_MIME_TYPE:
                link += '/'
            out += f"\n👥 __[Shareable Link]({link})__"
        return out

    def _upload_file(self, file_path: str, parent_id: str) -> str:
        if self._is_canceled:
            raise ProcessCanceled
        mime_type = guess_type(file_path)[0] or "text/plain"
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        body = {"name": file_name, "mimeType": mime_type, "description": "Uploaded via Proxy using Userge"}
        if parent_id:
            body["parents"] = [parent_id]
            
        if file_size == 0:
            media_body = MediaFileUpload(file_path, mimetype=mime_type)
            u_file_obj = self._service.files().create(body=body, media_body=media_body,
                                                      supportsTeamDrives=True).execute()
            file_id = u_file_obj.get("id")
        else:
            # Reduced chunksize to 10MB so slow proxies don't time out the connection
            media_body = MediaFileUpload(file_path, mimetype=mime_type,
                                         chunksize=10*1024*1024, resumable=True)
            u_file_obj = self._service.files().create(body=body, media_body=media_body,
                                                      supportsTeamDrives=True)
            c_time = time.time()
            response = None
            while response is None:
                # Increased retries to 10 for better stability
                status, response = u_file_obj.next_chunk(num_retries=10)
                if self._is_canceled:
                    raise ProcessCanceled
                if status:
                    f_size = status.total_size
                    diff = time.time() - c_time
                    uploaded = status.resumable_progress
                    percentage = uploaded / f_size * 100
                    speed = round(uploaded / diff, 2)
                    eta = round((f_size - uploaded) / max(speed, 1))
                    tmp = \
                        "__Uploading to GDrive (Via Proxy)...__\n" + \
                        "```\n[{}{}]({}%)```\n" + \
                        "**File Name** : `{}`\n" + \
                        "**File Size** : `{}`\n" + \
                        "**Uploaded** : `{}`\n" + \
                        "**Completed** : `{}/{}`\n" + \
                        "**Speed** : `{}/s`\n" + \
                        "**ETA** : `{}`"
                    self._progress = tmp.format(
                        "".join((config.FINISHED_PROGRESS_STR
                                 for _ in range(math.floor(percentage / 5)))),
                        "".join((config.UNFINISHED_PROGRESS_STR
                                 for _ in range(20 - math.floor(percentage / 5)))),
                        round(percentage, 2),
                        file_name,
                        humanbytes(f_size),
                        humanbytes(uploaded),
                        self._completed,
                        self._list,
                        humanbytes(speed),
                        time_formatter(eta))
            file_id = response.get("id")
            
        if gdrive and not gdrive.G_DRIVE_IS_TD:
            self._set_permission(file_id)
        self._completed += 1
        return file_id

    def _create_drive_dir(self, dir_name: str, parent_id: str) -> str:
        if self._is_canceled:
            raise ProcessCanceled
        body = {"name": dir_name, "mimeType": G_DRIVE_DIR_MIME_TYPE}
        if parent_id:
            body["parents"] = [parent_id]
        file_ = self._service.files().create(body=body, supportsTeamDrives=True).execute()
        file_id = file_.get("id")
        if gdrive and not gdrive.G_DRIVE_IS_TD:
            self._set_permission(file_id)
        self._completed += 1
        return file_id

    def _upload_dir(self, input_directory: str, parent_id: str) -> str:
        if self._is_canceled:
            raise ProcessCanceled
        list_dirs = os.listdir(input_directory)
        if len(list_dirs) == 0:
            return parent_id
        self._list += len(list_dirs)
        new_id = None
        for item in list_dirs:
            current_file_name = os.path.join(input_directory, item)
            if os.path.isdir(current_file_name):
                current_dir_id = self._create_drive_dir(item, parent_id)
                new_id = self._upload_dir(current_file_name, current_dir_id)
            else:
                self._upload_file(current_file_name, parent_id)
                new_id = parent_id
        return new_id

    def _upload(self, file_name: str) -> None:
        try:
            if os.path.isfile(file_name):
                file_id = self._upload_file(file_name, self._parent_id)
            else:
                folder_name = os.path.basename(os.path.abspath(file_name))
                file_id = self._create_drive_dir(folder_name, self._parent_id)
                self._upload_dir(file_name, file_id)
            self._output = self._get_output(file_id)
        except HttpError as h_e:
            _LOG.exception(h_e)
            self._output = h_e
        except ProcessCanceled:
            self._output = "`Process Canceled!`"
        except Exception as e:
            _LOG.exception(e)
            self._output = f"`Upload Error:` {str(e)}"
        finally:
            self._finish()


class ProxyWorker(_GDriveProxy):
    def __init__(self, message: Message) -> None:
        self._message = message
        super().__init__()

    async def upload(self) -> None:
        if not _CREDS:
            await self._message.edit("`GDrive not setup! Run .gsetup first.`", del_in=5)
            return

        replied = self._message.reply_to_message
        is_input_url = is_url(self._message.input_str)
        dl_loc = ""
        
        if replied and replied.media:
            try:
                dl_loc, _ = await tg_download(self._message, replied)
            except ProcessCanceled:
                await self._message.canceled()
                return
            except Exception as e_e:
                await self._message.err(str(e_e))
                return
        elif is_input_url:
            try:
                dl_loc, _ = await url_download(self._message, self._message.input_str)
            except ProcessCanceled:
                await self._message.canceled()
                return
            except Exception as e_e:
                await self._message.err(str(e_e))
                return
                
        file_path = dl_loc if dl_loc else self._message.input_str
        if not os.path.exists(file_path):
            await self._message.err("invalid file path provided?")
            return
            
        if "|" in file_path:
            file_path, file_name = file_path.split("|")
            new_path = os.path.join(os.path.dirname(file_path.strip()), file_name.strip())
            os.rename(file_path.strip(), new_path)
            file_path = new_path
            
        await self._message.try_to_edit("`Loading GDrive Upload (Proxied)...`")
        
        pool.submit_thread(self._upload, file_path)
        start_t = datetime.now()
        
        with self._message.cancel_callback(self._cancel):
            while not self._is_finished:
                if self._progress is not None:
                    await self._message.edit(self._progress)
                # Ensure it doesn't flood edits, default is usually 3-5 seconds in Userge
                await asyncio.sleep(config.Dynamic.EDIT_SLEEP_TIMEOUT)
                
        if dl_loc and os.path.exists(dl_loc):
            os.remove(dl_loc)
            
        end_t = datetime.now()
        m_s = (end_t - start_t).seconds
        
        if isinstance(self._output, HttpError):
            out = f"**ERROR** : `{self._output._get_reason()}`" 
        elif self._output is not None and not self._is_canceled:
            if str(self._output).startswith("`Upload Error:`"):
                out = self._output
            else:
                out = f"**Uploaded Successfully (Via Proxy)** __in {m_s} seconds__\n\n{self._output}"
        elif self._output is not None and self._is_canceled:
            out = self._output
        else:
            out = "`failed to upload.. check logs?`"
            
        await self._message.edit(out, disable_web_page_preview=True, log=__name__)


@userge.on_cmd("gupp", about={
    'header': "Upload files to GDrive using an HTTP/SOCKS Proxy",
    'description': "Requires setting the 'GUP_PROXY' environment variable.\n"
                   "Example: GUP_PROXY=http://1.2.3.4:8080",
    'usage': "{tr}gupp [file path | link | reply to file] | [new name]"}, check_downpath=True)
async def gupp_proxy_upload(message: Message):
    """ upload to gdrive securely via proxy """
    await ProxyWorker(message).upload()
