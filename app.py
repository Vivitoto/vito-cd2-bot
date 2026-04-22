import os
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
import requests
from flask import Flask, request
from wechatpy.enterprise.crypto import WeChatCrypto
import grpc
import clouddrive_pb2
import clouddrive_pb2_grpc

app = Flask(__name__)

# --- 1. 企微配置 ---
CORP_ID = os.getenv("CORP_ID")
APP_SECRET = os.getenv("APP_SECRET")
AGENT_ID = os.getenv("AGENT_ID")
APP_TOKEN = os.getenv("APP_TOKEN")
ENCODING_AES_KEY = os.getenv("ENCODING_AES_KEY")
WECHAT_PROXY = os.getenv("WECHAT_PROXY", "https://qyapi.weixin.qq.com").rstrip("/")

# --- 2. CD2 gRPC 配置 ---
CD2_HOST = os.getenv("CD2_HOST", "192.168.1.10:19798").replace("http://", "").replace("https://", "")
CD2_TOKEN = os.getenv("CD2_TOKEN")
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH")
ORGANIZE_BY_DATE = os.getenv("ORGANIZE_BY_DATE", "true").lower() in ("true", "1", "yes", "on")

# --- 3. Prowlarr 聚合搜索配置 ---
PROWLARR_URL = os.getenv("PROWLARR_URL", "http://192.168.1.10:9696").rstrip("/")
PROWLARR_API_KEY = os.getenv("PROWLARR_API_KEY")

crypto = WeChatCrypto(APP_TOKEN, ENCODING_AES_KEY, CORP_ID)

# --- 内存缓存区 ---
recent_msg_ids = []         # 防重复消息缓存
user_search_cache = {}      # 保存用户的搜索结果列表 { "user_id": [ {title, size, url}, ... ] }

def format_size(size_bytes):
    """将字节大小格式化为 MB 或 GB"""
    if not size_bytes: return "未知大小"
    size_mb = size_bytes / (1024 * 1024)
    if size_mb >= 1024:
        return f"{size_mb/1024:.2f} GB"
    return f"{size_mb:.2f} MB"

def send_wechat_reply(touser, content):
    try:
        token_url = f"{WECHAT_PROXY}/cgi-bin/gettoken?corpid={CORP_ID}&corpsecret={APP_SECRET}"
        token_res = requests.get(token_url, timeout=10).json()
        access_token = token_res.get("access_token")
        if not access_token: return
        
        send_url = f"{WECHAT_PROXY}/cgi-bin/message/send?access_token={access_token}"
        payload = {
            "touser": touser,
            "msgtype": "text",
            "agentid": AGENT_ID,
            "text": {"content": content}
        }
        requests.post(send_url, json=payload, timeout=10)
    except Exception as e:
        print(f"[*] 微信回复失败: {e}")

def get_search_results(keyword):
    """搜索并返回资源列表（包含标题、大小、链接等）"""
    if not PROWLARR_API_KEY: return []
        
    try:
        url = f"{PROWLARR_URL}/api/v1/search"
        headers = {"X-Api-Key": PROWLARR_API_KEY}
        params = {"query": keyword, "type": "search"}
        
        res = requests.get(url, headers=headers, params=params, timeout=20)
        res.raise_for_status()
        results = res.json()
        
        valid_results = []
        for item in results:
            info_hash = item.get("infoHash")
            magnet = item.get("magnetUrl")
            dl_url = item.get("downloadUrl")
            
            final_url = None
            if info_hash:
                final_url = f"magnet:?xt=urn:btih:{info_hash}"
            elif magnet and magnet.startswith("magnet:"):
                final_url = magnet
            elif dl_url:
                if PROWLARR_URL in dl_url and "apikey=" not in dl_url.lower():
                    sep = "&" if "?" in dl_url else "?"
                    final_url = f"{dl_url}{sep}apikey={PROWLARR_API_KEY}"
                else:
                    final_url = dl_url
            elif str(item.get("guid")).startswith("magnet:"):
                final_url = item.get("guid")
                
            if final_url:
                valid_results.append({
                    "title": item.get("title", "未知标题"),
                    "size": item.get("size", 0),
                    "seeders": item.get("seeders", 0),
                    "indexer": item.get("indexer", "未知"),
                    "url": final_url
                })
        
        # 按做种人数排序，最多返回 8 个结果防止微信消息超长
        valid_results.sort(key=lambda x: x["seeders"], reverse=True)
        return valid_results[:8]
    except Exception as e:
        print(f"[*] Prowlarr 搜索异常: {e}")
        return []

def _get_today_folder():
    """根据配置返回实际转存目录。若 ORGANIZE_BY_DATE 开启，则在 DOWNLOAD_PATH 下追加 YYYY-MM-DD 目录。"""
    base = DOWNLOAD_PATH or "/"
    if not ORGANIZE_BY_DATE:
        return base
    today = datetime.now().strftime("%Y-%m-%d")
    # 统一使用 / 作为路径分隔符，去掉尾部多余 /
    base = base.rstrip("/")
    return f"{base}/{today}"

def _cd2_create_folder(folder_path):
    """通过 CD2 gRPC 创建目录；若目录已存在则忽略错误。"""
    if not CD2_TOKEN:
        return False
    try:
        channel = grpc.insecure_channel(CD2_HOST)
        stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
        metadata = [("authorization", f"Bearer {CD2_TOKEN}")]
        req = clouddrive_pb2.CreateFolderRequest(path=folder_path)
        stub.CreateFolder(req, metadata=metadata, timeout=10)
        return True
    except grpc.RpcError as e:
        # 目录已存在时通常会返回 ALREADY_EXISTS，属于正常情况
        if e.code() == grpc.StatusCode.ALREADY_EXISTS:
            return True
        print(f"[*] CD2 创建目录异常: {e}")
        return False
    except Exception as e:
        print(f"[*] CD2 创建目录异常: {e}")
        return False

def cd2_offline_download(target_url):
    if not CD2_TOKEN: return False, "未配置 CD2_TOKEN"
    try:
        target_folder = _get_today_folder()
        
        # 若开启了按日期归档，先尝试创建日期目录
        if ORGANIZE_BY_DATE:
            created = _cd2_create_folder(target_folder)
            if not created:
                print(f"[*] 警告：创建日期目录 {target_folder} 失败，将尝试直接转存到该路径")
        
        channel = grpc.insecure_channel(CD2_HOST)
        stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
        metadata = [("authorization", f"Bearer {CD2_TOKEN}")]
        req = clouddrive_pb2.AddOfflineFileRequest(
            urls=target_url,
            toFolder=target_folder,
            checkFolderAfterSecs=0
        )
        res = stub.AddOfflineFiles(req, metadata=metadata, timeout=10)
        return (True, f"提交成功 → {target_folder}") if res.success else (False, f"被拒: {res.errorMessage}")
    except Exception as e:
        return False, f"系统异常: {str(e)}"

def process_message_async(from_user, content):
    # 【模式1】用户发送纯数字序号进行选择下载
    if content.isdigit():
        if from_user in user_search_cache:
            idx = int(content) - 1
            cached_results = user_search_cache[from_user]
            
            if 0 <= idx < len(cached_results):
                selected_item = cached_results[idx]
                target_url = selected_item["url"]
                
                send_wechat_reply(from_user, f"⏳ 正在推送: {selected_item['title'][:30]}...")
                success, detail = cd2_offline_download(target_url)
                
                if success:
                    send_wechat_reply(from_user, f"✅ 离线任务建立成功！\n🤖 状态: {detail}")
                else:
                    send_wechat_reply(from_user, f"❌ 离线任务失败\n⚠️ 原因: {detail}")
            else:
                send_wechat_reply(from_user, "⚠️ 无效的序号，请回复列表中存在的数字。")
        else:
            send_wechat_reply(from_user, "⚠️ 未找到你的搜索记录，请先输入番号进行搜索。")
        return

    # 【模式2】用户直接发送磁力/种子链接
    if content.startswith("magnet:?") or content.startswith("http"):
        success, detail = cd2_offline_download(content)
        if success:
            send_wechat_reply(from_user, f"✅ 直链离线成功\n🤖 状态: {detail}")
        else:
            send_wechat_reply(from_user, f"❌ 直链离线失败\n⚠️ 原因: {detail}")
        return

    # 【模式3】用户发送关键词进行搜索
    if len(content) > 3: 
        send_wechat_reply(from_user, f"🔍 正在检索【{content}】...")
        results = get_search_results(content)
        
        if results:
            # 将搜索结果存入该用户的“短期记忆”中
            user_search_cache[from_user] = results
            
            # 拼接回复文案
            reply_lines = [f"🔍 找到 {len(results)} 个结果，请直接回复【序号】下载：\n"]
            for i, res in enumerate(results):
                size_str = format_size(res['size'])
                # 截取标题长度防止过长
                title_short = res['title'][:40] + ("..." if len(res['title']) > 40 else "")
                reply_lines.append(f"{i+1}. [{size_str}] {title_short} (源:{res['indexer']} 种:{res['seeders']})")
                
            # 一次性发送列表给用户
            send_wechat_reply(from_user, "\n".join(reply_lines))
        else:
            send_wechat_reply(from_user, f"😭 未能在库中找到【{content}】。")

@app.route('/wechat', methods=['GET', 'POST'])
def wechat_callback():
    signature = request.args.get('msg_signature', '')
    timestamp = request.args.get('timestamp', '')
    nonce = request.args.get('nonce', '')

    if request.method == 'GET':
        echostr = request.args.get('echostr', '')
        try:
            return crypto.check_signature(signature, timestamp, nonce, echostr)
        except Exception as e:
            return f"验证失败: {e}", 403

    if request.method == 'POST':
        try:
            msg_xml = crypto.decrypt_message(request.data, signature, timestamp, nonce)
            tree = ET.fromstring(msg_xml)
            
            msg_id_node = tree.find('MsgId')
            if msg_id_node is not None:
                msg_id = msg_id_node.text
                if msg_id in recent_msg_ids:
                    return "success"
                recent_msg_ids.append(msg_id)
                if len(recent_msg_ids) > 100:
                    recent_msg_ids.pop(0)
            
            msg_type = tree.find('MsgType').text
            from_user = tree.find('FromUserName').text
            
            if msg_type == 'text':
                content = tree.find('Content').text.strip()
                threading.Thread(target=process_message_async, args=(from_user, content)).start()
                
            return "success"
        except Exception as e:
            return "success"

if __name__ == '__main__':
    print("[*] 机器人已启动，监听 5000 端口...")
    app.run(host='0.0.0.0', port=5000)
