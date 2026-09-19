import os
import sys
import time
import requests
import subprocess
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
        "id": "userge_godl",
        "method": method,
        "params": params or []
    }
    try:
        response = requests.post(RPC_URL, json=payload, timeout=5, proxies={"http": None, "https": None})
        return response.json().get("result")
    except Exception:
        return None

@userge.on_cmd("godl", about={"header": "Download GoFile links via Aria2 RPC"})
async def godl_command(message: Message):
    args = message.input_str.strip().split()
    if not args:
        await message.edit("`Usage: .godl <url_or_id> [index]`")
        return
        
    target = args[0]
    index = int(args[1]) if len(args) > 1 else None
    folder_id = target.split("/")[-1]
    
    account_token = os.environ.get("GOFILE_TOKEN")
    if not account_token:
        await message.edit("`GOFILE_TOKEN environment variable is not set!`")
        return

    await message.edit("`Bypassing GoFile Token...`")

    user_agent = "Mozilla/5.0 (Linux; Android 16; 24069PC21I) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.7827.91 Mobile Safari/537.36"
    language = "en-US" 
    
    # Isolate session from global proxies
    session = requests.Session()
    session.proxies = {"http": None, "https": None}
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Origin": "https://gofile.io",
        "Referer": "https://gofile.io/"
    })
        
    # Fetch Obfuscated JS & Generate Website Token via qjs
    try:
        js_code = session.get("https://gofile.io/js/wt.obf.js").text
        js_wrapper = f"""
        const navigator = {{ userAgent: "{user_agent}", language: "{language}" }};
        const window = {{}};
        const appdata = {{ wt: "4fd6sg89d7s6" }};
        {js_code}
        console.log(generateWT("{account_token}"));
        """
        
        with open("decode.js", "w") as f:
            f.write(js_wrapper)

        result = subprocess.run(["qjs", "decode.js"], capture_output=True, text=True)
        if os.path.exists("decode.js"):
            os.remove("decode.js")
            
        website_token = result.stdout.strip()
    except Exception as e:
        await message.edit(f"`Error resolving GoFile token:` {str(e)}")
        return

    session.headers.update({
        "Authorization": f"Bearer {account_token}",
        "X-Website-Token": website_token,
        "X-BL": language
    })
    
    # Fetch API JSON
    api_url = f"https://api.gofile.io/contents/{folder_id}"
    content_data = session.get(api_url).json()

    if content_data.get("status") != "ok":
        await message.edit("`Failed to fetch GoFile content metadata.`")
        return

    data = content_data.get("data", {})
    item_type = data.get("type")
    
    files_to_download = []
    
    if item_type == "file":
        files_to_download.append((data.get("name"), data.get("link")))
    elif item_type == "folder":
        children = data.get("children", {})
        for child_id, child_info in children.items():
            files_to_download.append((child_info.get("name"), child_info.get("link")))
            
    if index is not None:
        idx = int(index) - 1
        if 0 <= idx < len(files_to_download):
            files_to_download = [files_to_download[idx]]
        else:
            await message.edit("`Index out of range.`")
            return

    if not files_to_download:
        await message.edit("`No files found to download.`")
        return

    # Process Downloads Iteratively
    for name, link in files_to_download:
        if not link:
            continue
            
        await message.edit(f"`Adding {name} to Aria2...`")
        
        # Send to Aria2 with account cookie
        options = {
            "header": [f"Cookie: accountToken={account_token}"],
            "out": name,
            "max-connection-per-server": "16",
            "split": "16"
        }
        
        res = aria2_rpc("aria2.addUri", [[link], options])
        if not res:
            await message.edit("`Failed to connect to Aria2 daemon.`")
            continue
            
        gid = res
        start_time = time.time()
        last_msg = ""

        while True:
            status_res = aria2_rpc("aria2.tellStatus", [gid])
            if not status_res:
                break
                
            status = status_res.get("status")
            
            if status == "complete":
                elapsed = round(time.time() - start_time, 2)
                await message.edit(f"**GoFile Download Complete!**\n📁 `{name}`\n⏱ `{elapsed}s`")
                break
            elif status == "error":
                await message.edit(f"`Download failed for {name}`")
                break
            elif status == "active":
                completed = int(status_res.get("completedLength", 0))
                total = int(status_res.get("totalLength", 1))
                speed = int(status_res.get("downloadSpeed", 0))
                
                percentage = (completed / total) * 100 if total > 0 else 0
                eta = (total - completed) / speed if speed > 0 else 0
                
                progress_str = (
                    f"📥 **Downloading GoFile...**\n"
                    f"📁 **File:** `{name}`\n"
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
            
            await asyncio.sleep(3)
