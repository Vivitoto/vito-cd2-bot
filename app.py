import os
import re
import shutil
import threading
import xml.etree.ElementTree as ET
from datetime import datetime

import grpc
import requests
import yaml
from flask import Flask, request
from wechatpy.enterprise.crypto import WeChatCrypto

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

# --- 3. 下载路由配置(YAML) ---
DOWNLOAD_ROUTES_CONFIG = os.getenv("DOWNLOAD_ROUTES_CONFIG", "/config/download-routes.yml")
DOWNLOAD_ROUTES_EXAMPLE = os.getenv("DOWNLOAD_ROUTES_EXAMPLE", "/app/download-routes.example.yml")

# --- 内存缓存区 ---
recent_msg_ids = []
user_search_cache = {}
DOWNLOAD_ROUTES = {}
DEFAULT_DOWNLOAD_ROUTE = "main"


def _ensure_routes_config():
    """确保下载路由配置文件存在；若不存在则从示例文件初始化。"""
    config_path = DOWNLOAD_ROUTES_CONFIG
    config_dir = os.path.dirname(config_path)
    if config_dir:
        os.makedirs(config_dir, exist_ok=True)

    if os.path.exists(config_path):
        return False

    if os.path.exists(DOWNLOAD_ROUTES_EXAMPLE):
        shutil.copyfile(DOWNLOAD_ROUTES_EXAMPLE, config_path)
        print(f"[*] 未发现下载路由配置，已初始化到: {config_path}")
        print("[*] 请按需修改该文件后重启容器。")
        return True

    raise FileNotFoundError(f"下载路由示例文件不存在: {DOWNLOAD_ROUTES_EXAMPLE}")



def _load_download_routes():
    """从 YAML 加载下载路由配置。"""
    global DOWNLOAD_ROUTES, DEFAULT_DOWNLOAD_ROUTE

    initialized = _ensure_routes_config()

    with open(DOWNLOAD_ROUTES_CONFIG, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    routes = config.get("routes") or {}
    default_route = str(config.get("default_route") or "main").strip()

    normalized_routes = {}
    for route_name, route_conf in routes.items():
        name = str(route_name or "").strip().lower()
        conf = route_conf or {}
        path = str(conf.get("path") or "").strip()
        if not name or not path:
            continue
        normalized_routes[name] = {
            "path": path,
            "organize_by_date": bool(conf.get("organize_by_date", True)),
            "allow_subdir": bool(conf.get("allow_subdir", True)),
            "comment": str(conf.get("comment") or "").strip(),
        }

    if not normalized_routes:
        raise ValueError("下载路由配置为空，请至少配置一个 routes 项。")

    if default_route not in normalized_routes:
        default_route = next(iter(normalized_routes.keys()))

    DOWNLOAD_ROUTES = normalized_routes
    DEFAULT_DOWNLOAD_ROUTE = default_route

    if initialized:
        print(f"[*] 当前默认路由: {DEFAULT_DOWNLOAD_ROUTE}")
        print(f"[*] 可用路由: {', '.join(DOWNLOAD_ROUTES.keys())}")


# Gunicorn 以 `app:app` 导入模块时不会执行 __main__，
# 所以需要在模块导入阶段完成配置初始化。
_load_download_routes()
crypto = WeChatCrypto(APP_TOKEN, ENCODING_AES_KEY, CORP_ID)



def format_size(size_bytes):
    if not size_bytes:
        return "未知大小"
    size_mb = size_bytes / (1024 * 1024)
    if size_mb >= 1024:
        return f"{size_mb / 1024:.2f} GB"
    return f"{size_mb:.2f} MB"



def send_wechat_reply(touser, content):
    try:
        token_url = f"{WECHAT_PROXY}/cgi-bin/gettoken?corpid={CORP_ID}&corpsecret={APP_SECRET}"
        token_res = requests.get(token_url, timeout=10).json()
        access_token = token_res.get("access_token")
        if not access_token:
            return

        send_url = f"{WECHAT_PROXY}/cgi-bin/message/send?access_token={access_token}"
        payload = {
            "touser": touser,
            "msgtype": "text",
            "agentid": AGENT_ID,
            "text": {"content": content},
        }
        requests.post(send_url, json=payload, timeout=10)
    except Exception as e:
        print(f"[*] 微信回复失败: {e}")




def _join_path(base: str, *parts: str) -> str:
    current = (base or "/").rstrip("/") or "/"
    for part in parts:
        part = str(part or "").strip().strip("/")
        if not part:
            continue
        current = f"{current}/{part}" if current != "/" else f"/{part}"
    return current



def _sanitize_subdir_name(name: str) -> str:
    name = str(name or "").strip()
    name = re.sub(r"[\\\r\n\t]+", " ", name)
    name = re.sub(r"/+", "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:100]



def _get_route_config(route: str):
    route = str(route or "").strip().lower()
    return DOWNLOAD_ROUTES.get(route)



def _get_available_routes_text() -> str:
    return "、".join(f"/{name}" for name in DOWNLOAD_ROUTES.keys())



def _cd2_create_folder(folder_path):
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
        if e.code() == grpc.StatusCode.ALREADY_EXISTS:
            return True
        print(f"[*] CD2 创建目录异常: {e}")
        return False
    except Exception as e:
        print(f"[*] CD2 创建目录异常: {e}")
        return False



def cd2_offline_download(target_url, target_folder):
    if not CD2_TOKEN:
        return False, "未配置 CD2_TOKEN"
    try:
        target_folder = (target_folder or "/").strip() or "/"
        created = _cd2_create_folder(target_folder)
        if not created:
            print(f"[*] 警告：创建目录 {target_folder} 失败，将尝试直接转存到该路径")

        channel = grpc.insecure_channel(CD2_HOST)
        stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
        metadata = [("authorization", f"Bearer {CD2_TOKEN}")]
        req = clouddrive_pb2.AddOfflineFileRequest(urls=target_url, toFolder=target_folder, checkFolderAfterSecs=0)
        res = stub.AddOfflineFiles(req, metadata=metadata, timeout=10)
        return (True, f"提交成功 → {target_folder}") if res.success else (False, f"被拒: {res.errorMessage}")
    except Exception as e:
        return False, f"系统异常: {str(e)}"



def _normalize_download_url(raw: str) -> str:
    raw = str(raw or "").strip()
    lowered = raw.lower()
    if lowered.startswith("magnet:") or lowered.startswith("http://") or lowered.startswith("https://") or lowered.startswith("ed2k://"):
        return raw
    if re.fullmatch(r"[0-9a-fA-F]{40}", raw):
        return f"magnet:?xt=urn:btih:{raw.upper()}"
    return raw



def _is_supported_download_url(raw: str) -> bool:
    raw = str(raw or "").strip()
    lowered = raw.lower()
    return (
        lowered.startswith("magnet:")
        or lowered.startswith("http://")
        or lowered.startswith("https://")
        or lowered.startswith("ed2k://")
        or bool(re.fullmatch(r"[0-9a-fA-F]{40}", raw))
    )



def _resolve_target_folder(route: str, custom_subdir: str = "") -> str:
    route_conf = _get_route_config(route)
    if not route_conf:
        raise ValueError(f"未知路由: {route}")

    target = route_conf["path"]
    clean_subdir = _sanitize_subdir_name(custom_subdir)
    if clean_subdir and route_conf.get("allow_subdir", True):
        target = _join_path(target, clean_subdir)

    if route_conf.get("organize_by_date", True):
        target = _join_path(target, datetime.now().strftime("%Y-%m-%d"))
    return target



def _parse_download_command(content: str):
    text = str(content or "").strip()
    if not text:
        return None

    route = DEFAULT_DOWNLOAD_ROUTE
    custom_subdir = ""
    payload = text

    if text.startswith("/"):
        first_line, *rest_lines = text.splitlines()
        parts = first_line.split(maxsplit=2)
        command = parts[0].lower().lstrip("/")
        route_conf = _get_route_config(command)
        if route_conf:
            route = command
            if len(parts) == 1:
                payload = "\n".join(rest_lines).strip()
                if not payload:
                    return {"route": route, "custom_subdir": "", "target_urls": []}
            elif len(parts) == 2:
                if rest_lines:
                    if route_conf.get("allow_subdir", True):
                        custom_subdir = parts[1].strip()
                        payload = "\n".join(rest_lines).strip()
                    else:
                        payload = "\n".join([parts[1], *rest_lines]).strip()
                else:
                    payload = parts[1]
            else:
                maybe_dir = parts[1].strip()
                maybe_url = parts[2].strip()
                normalized = _normalize_download_url(maybe_dir)
                if _is_supported_download_url(maybe_dir) or normalized != maybe_dir:
                    payload = " ".join([maybe_dir, maybe_url]).strip() if maybe_url else maybe_dir
                else:
                    if route_conf.get("allow_subdir", True):
                        custom_subdir = maybe_dir
                        payload = "\n".join([maybe_url, *rest_lines]).strip()
                    else:
                        payload = "\n".join([maybe_dir, maybe_url, *rest_lines]).strip()
        else:
            return {"unknown_route": command}

    lines = [line.strip() for line in str(payload or "").splitlines() if line.strip()]
    if not lines and payload.strip():
        lines = [payload.strip()]

    target_urls = []
    for line in lines:
        normalized = _normalize_download_url(line)
        if _is_supported_download_url(line) or normalized != line:
            target_urls.append(normalized)
        else:
            return None

    return {"route": route, "custom_subdir": custom_subdir, "target_urls": target_urls}



def process_message_async(from_user, content):
    content = str(content or "").strip()

    if content.startswith("/") and len(content.split()) == 1:
        route_name = content[1:].strip().lower()
        if _get_route_config(route_name):
            send_wechat_reply(
                from_user,
                "⚠️ 用法示例：\n"
                f"1. 直接离线到默认目录：E808151805F0A2C8C281FBEFA682AD29EDA73FF2\n"
                f"2. 离线到 {route_name}：/{route_name} E808151805F0A2C8C281FBEFA682AD29EDA73FF2\n"
                f"3. 离线到自定义子目录：/{route_name} @你好 E808151805F0A2C8C281FBEFA682AD29EDA73FF2"
            )
            return

    parsed = _parse_download_command(content)
    if parsed:
        if parsed.get("unknown_route"):
            send_wechat_reply(
                from_user,
                f"⚠️ 未知路由：{parsed['unknown_route']}\n可用路由：{_get_available_routes_text()}"
            )
            return

        if not parsed["target_urls"]:
            send_wechat_reply(from_user, "⚠️ 你只写了路由命令，但没带 magnet / ed2k / hash / 下载链接。")
            return

        route_conf = _get_route_config(parsed["route"])
        if parsed["custom_subdir"] and not route_conf.get("allow_subdir", True):
            send_wechat_reply(from_user, f"⚠️ 路由 /{parsed['route']} 不允许自定义子目录。")
            return

        target_folder = _resolve_target_folder(parsed["route"], parsed["custom_subdir"])
        success_count = 0
        fail_count = 0
        fail_reasons = []
        for target_url in parsed["target_urls"]:
            success, detail = cd2_offline_download(target_url, target_folder=target_folder)
            if success:
                success_count += 1
            else:
                fail_count += 1
                fail_reasons.append(detail)

        extra = f"\n📂 子目录: {parsed['custom_subdir']}" if parsed["custom_subdir"] else ""
        if fail_count == 0:
            send_wechat_reply(
                from_user,
                f"✅ 离线任务建立成功\n"
                f"📦 数量: {success_count}{extra}\n"
                f"🤖 状态: 提交成功 → {target_folder}"
            )
        elif success_count == 0:
            send_wechat_reply(
                from_user,
                f"❌ 离线任务失败\n"
                f"📦 数量: {len(parsed['target_urls'])}\n"
                f"⚠️ 原因: {fail_reasons[0] if fail_reasons else '未知错误'}"
            )
        else:
            send_wechat_reply(
                from_user,
                f"⚠️ 部分离线成功\n"
                f"✅ 成功: {success_count}\n"
                f"❌ 失败: {fail_count}{extra}\n"
                f"🤖 目标目录: {target_folder}\n"
                f"⚠️ 首个失败原因: {fail_reasons[0] if fail_reasons else '未知错误'}"
            )
        return

    send_wechat_reply(
        from_user,
        "⚠️ 当前版本仅支持直接离线链接，不再提供搜索功能。\n"
        "请发送 magnet / ed2k / http(s) / 40位hash，或使用 /路由名 + 链接。"
    )


@app.route("/wechat", methods=["GET", "POST"])
def wechat_callback():
    signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")

    if request.method == "GET":
        echostr = request.args.get("echostr", "")
        try:
            return crypto.check_signature(signature, timestamp, nonce, echostr)
        except Exception as e:
            return f"验证失败: {e}", 403

    if request.method == "POST":
        try:
            msg_xml = crypto.decrypt_message(request.data, signature, timestamp, nonce)
            tree = ET.fromstring(msg_xml)

            msg_id_node = tree.find("MsgId")
            if msg_id_node is not None:
                msg_id = msg_id_node.text
                if msg_id in recent_msg_ids:
                    return "success"
                recent_msg_ids.append(msg_id)
                if len(recent_msg_ids) > 100:
                    recent_msg_ids.pop(0)

            msg_type = tree.find("MsgType").text
            from_user = tree.find("FromUserName").text

            if msg_type == "text":
                content = tree.find("Content").text.strip()
                threading.Thread(target=process_message_async, args=(from_user, content)).start()

            return "success"
        except Exception:
            return "success"


if __name__ == "__main__":
    print("[*] 机器人已启动，监听 5000 端口...")
    app.run(host="0.0.0.0", port=5000)
