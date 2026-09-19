# plugins/custom/gupp/__main__.py

import os
import asyncio
from datetime import datetime
import httplib2

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from userge import userge, Message, config, pool
from userge.plugins.misc.download import url_download, tg_download
from userge.utils import is_url
from userge.utils.exceptions import ProcessCanceled

# We import the existing GDrive plugin logic to reuse its authentication, 
# variables, and UI formatting without duplicating 1,000 lines of code.
import userge.plugins.misc.gdrive.__main__ as gdrive_main
from userge.plugins.misc.gdrive.__main__ import Worker, creds_dec

# Define proxy settings. You can edit these directly here or 
# set them as environment variables on your Heroku/Linux server.
PROXY_HOST = os.environ.get("GUPP_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("GUPP_PROXY_PORT", "1088"))

class ProxyWorker(Worker):
    """ 
    A custom GDrive Worker that routes API traffic through a local proxy.
    This overrides the default Google API connection strictly for this object, 
    ensuring global settings remain untouched so Aria2 local RPC won't break.
    """
    def __init__(self, message: Message) -> None:
        super().__init__(message)
        
    @property
    def _service(self) -> object:
        try:
            import socks
            proxy_type = socks.PROXY_TYPE_HTTP
        except ImportError:
            # Fallback integer for HTTP Proxy in httplib2 if PySocks isn't loaded
            proxy_type = 3 
            
        proxy_info = httplib2.ProxyInfo(
            proxy_type=proxy_type, 
            proxy_host=PROXY_HOST, 
            proxy_port=PROXY_PORT
        )
        
        # Inject the proxy strictly into this HTTP instance
        http = httplib2.Http(proxy_info=proxy_info)
        
        return build(
            "drive", 
            "v3", 
            credentials=gdrive_main._CREDS, 
            http=http, 
            cache_discovery=False
        )

@userge.on_cmd("gupp", about={
    'header': "Upload files to GDrive using Proxy",
    'description': "Uploads files via a proxy tunnel for faster speeds. "
                   "Does not modify global proxy configs, preventing conflicts with Aria2.",
    'usage': "{tr}gupp [file / folder path | direct link | reply to telegram file] | [new name]",
    'examples': [
        "{tr}gupp test.bin : reply to tg file",
        "{tr}gupp downloads/100MB.bin | test.bin"
    ]}, check_downpath=True)
@creds_dec
async def gupp_(message: Message):
    """ upload to gdrive using local session proxy """
    # Instantiate our isolated proxy worker instead of the normal Worker
    worker = ProxyWorker(message)
    
    replied = message.reply_to_message
    is_input_url = is_url(message.input_str)
    dl_loc = ""
    
    if replied and replied.media:
        try:
            dl_loc, _ = await tg_download(message, replied)
        except ProcessCanceled:
            await message.canceled()
            return
        except Exception as e_e:
            await message.err(str(e_e))
            return
    elif is_input_url:
        try:
            dl_loc, _ = await url_download(message, message.input_str)
        except ProcessCanceled:
            await message.canceled()
            return
        except Exception as e_e:
            await message.err(str(e_e))
            return
            
    file_path = dl_loc if dl_loc else message.input_str
    if not os.path.exists(file_path):
        await message.err("invalid file path provided?")
        return
        
    if "|" in file_path:
        file_path, file_name = file_path.split("|")
        new_path = os.path.join(os.path.dirname(file_path.strip()), file_name.strip())
        os.rename(file_path.strip(), new_path)
        file_path = new_path
        
    await message.try_to_edit(f"`Loading GDrive Proxy Upload via {PROXY_HOST}:{PROXY_PORT}...`")
    
    # Delegate the upload task to the threaded ProxyWorker
    pool.submit_thread(worker._upload, file_path)
    start_t = datetime.now()
    
    with message.cancel_callback(worker._cancel):
        while not worker._is_finished:
            if worker._progress is not None:
                await message.edit(worker._progress)
            await asyncio.sleep(config.Dynamic.EDIT_SLEEP_TIMEOUT)
            
    if dl_loc and os.path.exists(dl_loc):
        os.remove(dl_loc)
        
    end_t = datetime.now()
    m_s = (end_t - start_t).seconds
    
    if isinstance(worker._output, HttpError):
        out = f"**ERROR** : `{worker._output._get_reason()}`"
    elif worker._output is not None and not worker._is_canceled:
        out = f"**Uploaded Successfully via Proxy** __in {m_s} seconds__\n\n{worker._output}"
    elif worker._output is not None and worker._is_canceled:
        out = worker._output
    else:
        out = "`failed to upload.. check logs?`"
        
    await message.edit(out, disable_web_page_preview=True, log=__name__)
