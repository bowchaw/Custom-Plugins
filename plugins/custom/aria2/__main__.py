import time
import requests
import asyncio
from userge import userge, Message

RPC_URL = "http://127.0.0.1:6800/jsonrpc"

def format_bytes(size):
    size = int(size)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0

def aria2_rpc(method, params=None):
    payload = {
        "jsonrpc": "2.0",
        "id": "userge_aria",
        "method": method,
        "params": params or []
    }
    try:
        response = requests.post(RPC_URL, json=payload, timeout=5, proxies={"http": None, "https": None})
        return response.json().get("result")
    except Exception:
        return None

@userge.on_cmd("ddl", about={"header": "Download file using aria2 RPC"})
async def direct_aria_download(message: Message):
    url = message.input_str
    if not url:
        await message.edit("`Please provide a URL to download!`")
        return

    await message.edit("`Adding download task to Aria2...`")
    
    # Using 14 connections as requested
    options = {
        "max-connection-per-server": "14",
        "split": "14"
    }
    
    res = aria2_rpc("aria2.addUri", [[url], options])
    if not res:
        await message.edit("`Failed to connect to Aria2 daemon or invalid response.`")
        return
    
    gid = res
    start_time = time.time()
    last_msg = ""

    while True:
        status_res = aria2_rpc("aria2.tellStatus", [gid])
        if not status_res:
            await message.edit("`Error fetching download status.`")
            break

        status = status_res.get("status")
        
        if status == "complete":
            file_name = status_res.get("files", [{}])[0].get("path", "Unknown").split('/')[-1]
            speed = status_res.get("downloadSpeed", 1)
            elapsed_time = round(time.time() - start_time, 2)
            
            await message.edit(
                f"**Download Completed Successfully!**\n\n"
                f"📁 **File:** `{file_name}`\n"
                f"⏱ **Time Taken:** `{elapsed_time}s`\n"
                f"⚡ **Avg Speed:** `{format_bytes(speed)}/s`"
            )
            break

        elif status == "error":
            err_msg = status_res.get("errorMessage", "Unknown error")
            await message.edit(f"`Download failed:` {err_msg}")
            break

        elif status == "active":
            completed = int(status_res.get("completedLength", 0))
            total = int(status_res.get("totalLength", 1))
            speed = int(status_res.get("downloadSpeed", 0))
            
            # Fetch filename dynamically
            files_info = status_res.get("files", [{}])[0]
            if files_info.get("path"):
                active_file_name = files_info["path"].split('/')[-1]
            elif files_info.get("uris"):
                active_file_name = files_info["uris"][0].get("uri", "Unknown").split('/')[-1]
            else:
                active_file_name = "Allocating..."

            percentage = (completed / total) * 100 if total > 0 else 0
            eta = (total - completed) / speed if speed > 0 else 0
            
            progress_str = (
                f"📥 **Downloading...**\n"
                f"📁 **File:** `{active_file_name}`\n"
                f"📊 **Progress:** `{percentage:.2f}%` ({format_bytes(completed)} / {format_bytes(total)})\n"
                f"🚀 **Speed:** `{format_bytes(speed)}/s`\n"
                f"⏳ **ETA:** `{int(eta)}s`"
            )
            
            if progress_str != last_msg:
                try:
                    await message.edit(progress_str)
                    last_msg = progress_str
                except Exception:
                    pass
        
        # Increased to 10 seconds to prevent Telegram flood waits
        await asyncio.sleep(10)
