import os
import re
import shutil
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
import uuid
import time
from typing import Optional

import grpc
import requests
import yaml
from google.protobuf.empty_pb2 import Empty
from flask import Flask, request
from wechatpy.enterprise.crypto import WeChatCrypto

import clouddrive_pb2
import clouddrive_pb2_grpc

app = Flask(__name__)


def log_info(message: str):
    print(f"[*] {message}", flush=True)


def log_warn(message: str):
    print(f"[!] {message}", flush=True)

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

# --- 4. 清洗配置 ---
ENABLE_CLEANUP = os.getenv("ENABLE_CLEANUP", "false").lower() in ("true", "1", "yes", "on")
JUNK_EXTENSIONS = os.getenv("JUNK_EXTENSIONS", "txt,url,html,mhtml,htm,mht,mp4,exe,rar,apk,gif,png,jpg")
JUNK_SIZE_THRESHOLD_MB = os.getenv("JUNK_SIZE_THRESHOLD_MB")

# --- 5. 中转清洗全局状态 ---
STAGING_FOLDER = ""
staging_tasks = {}
staging_lock = threading.Lock()

recent_msg_ids = []
recent_msg_ids_lock = threading.Lock()
DOWNLOAD_ROUTES = {}
DEFAULT_DOWNLOAD_ROUTE = "main"
CD2_MOUNT_ROOT_CACHE_TTL_SECS = 60
cd2_mount_root_cache = {"names": None, "ts": 0.0}
cd2_mount_root_cache_lock = threading.Lock()

# CD2 API 全局流控：每秒最多 2 次请求（官方上限 5 次）
_cd2_rate_lock = threading.Lock()
_CD2_MIN_INTERVAL = 0.5  # 2 req/s


def _cd2_rate_limit():
    """全局 CD2 流控：持有锁 + sleep，保证每秒至多 2 次 API 调用。"""
    with _cd2_rate_lock:
        time.sleep(_CD2_MIN_INTERVAL)


def _parse_bool(val):
    """安全解析 YAML / env 布尔值，支持 true/false/1/0/yes/no/on/off 和带类型标记的字符串。"""
    s = str(val or "").strip().lower()
    return s in ("true", "1", "yes", "on")

def _validate_config():
    """启动时校验关键配置，提前暴错避免运行时异常。"""
    issues = []
    for name, val in [("CORP_ID", CORP_ID), ("APP_SECRET", APP_SECRET),
                      ("AGENT_ID", AGENT_ID), ("APP_TOKEN", APP_TOKEN),
                      ("ENCODING_AES_KEY", ENCODING_AES_KEY)]:
        if not (val or "").strip():
            issues.append(f"缺少环境变量 {name}")
    if not (CD2_HOST or "").strip():
        issues.append("缺少环境变量 CD2_HOST")
    if not (CD2_TOKEN or "").strip():
        issues.append("缺少环境变量 CD2_TOKEN")
    threshold_raw = str(JUNK_SIZE_THRESHOLD_MB or "").strip()
    if threshold_raw:
        try:
            float(threshold_raw)
        except ValueError:
            issues.append(f"JUNK_SIZE_THRESHOLD_MB 值无效：{threshold_raw}（应为数字）")
    if issues:
        for msg in issues:
            log_warn(f"配置校验失败: {msg}")
        raise ValueError("配置校验失败: " + "; ".join(issues))
    log_info("启动配置校验通过")


def _ensure_routes_config():
    """确保下载路由配置文件存在；若不存在则从示例文件初始化。"""
    config_path = DOWNLOAD_ROUTES_CONFIG
    config_dir = os.path.dirname(config_path)
    if config_dir:
        os.makedirs(config_dir, exist_ok=True)

    if os.path.exists(config_path):
        log_info(f"已检测到下载路由配置文件: {config_path}")
        return False

    if os.path.exists(DOWNLOAD_ROUTES_EXAMPLE):
        shutil.copyfile(DOWNLOAD_ROUTES_EXAMPLE, config_path)
        log_info(f"未发现下载路由配置，已初始化到: {config_path}")
        log_info("请按需修改该文件后重启容器。")
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

    # 读取全局中转目录
    global STAGING_FOLDER
    STAGING_FOLDER = str(config.get("staging_folder") or "").strip()
    if STAGING_FOLDER:
        log_info(f"中转清洗已启用，中转目录: {STAGING_FOLDER}")
    else:
        log_info("中转清洗未启用（YAML 未配置 staging_folder）")

    normalized_routes = {}
    for route_name, route_conf in routes.items():
        name = str(route_name or "").strip().lower()
        conf = route_conf or {}
        path = str(conf.get("path") or "").strip()
        if not name or not path:
            continue
        normalized_routes[name] = {
            "path": path,
            "organize_by_date": _parse_bool(conf.get("organize_by_date", True)),
            "allow_subdir": _parse_bool(conf.get("allow_subdir", True)),
            "comment": str(conf.get("comment") or "").strip(),
        }

    if not normalized_routes:
        log_warn("下载路由配置读取失败：routes 为空")
        raise ValueError("下载路由配置为空，请至少配置一个 routes 项。")

    if default_route not in normalized_routes:
        default_route = next(iter(normalized_routes.keys()))

    DOWNLOAD_ROUTES = normalized_routes
    DEFAULT_DOWNLOAD_ROUTE = default_route

    log_info(f"下载路由配置读取成功，默认路由: {DEFAULT_DOWNLOAD_ROUTE}")
    log_info(f"可用路由: {', '.join(DOWNLOAD_ROUTES.keys())}")
    if initialized:
        log_info("当前运行使用的是自动初始化后的路由配置。")


# Gunicorn 以 `app:app` 导入模块时不会执行 __main__，
# 所以需要在模块导入阶段完成配置初始化。
_load_download_routes()
_validate_config()
crypto = WeChatCrypto(APP_TOKEN, ENCODING_AES_KEY, CORP_ID)
log_info("企业微信加解密模块初始化成功")



def _parse_magnet_info(url: str):
    """从 magnet URI 解析 dn(文件名) 和 xl(大小bytes)。"""
    dn_match = re.search(r'[?&]dn=([^&]+)', url, re.IGNORECASE)
    xl_match = re.search(r'[?&]xl=(\d+)', url, re.IGNORECASE)
    if not dn_match:
        return None, None
    from urllib.parse import unquote
    filename = unquote(dn_match.group(1))
    size_bytes = int(xl_match.group(1)) if xl_match else None
    return filename, size_bytes


def _parse_ed2k_info(url: str):
    """从 ed2k 链接解析文件名和大小(bytes)。返回 (filename, size_bytes) 或 (None, None)。"""
    match = re.match(r'ed2k://\|file\|([^|]+)\|(\d+)\|[a-fA-F0-9]{32}\|/', url, re.IGNORECASE)
    if not match:
        return None, None
    return match.group(1), int(match.group(2))


def _get_junk_extensions() -> set:
    return set(e.strip().lower() for e in JUNK_EXTENSIONS.split(",") if e.strip())


def _get_size_threshold_mb() -> Optional[float]:
    val = str(JUNK_SIZE_THRESHOLD_MB or "").strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        log_warn(f"JUNK_SIZE_THRESHOLD_MB 值无效，已忽略: {val}")
        return None




def send_wechat_reply(touser, content) -> bool:
    try:
        ok, access_token, message = _get_wechat_access_token(timeout=10)
        if not ok:
            log_warn(f"微信回复失败: {message}")
            return False

        send_url = f"{WECHAT_PROXY}/cgi-bin/message/send?access_token={access_token}"
        payload = {
            "touser": touser,
            "msgtype": "text",
            "agentid": AGENT_ID,
            "text": {"content": content},
        }
        res = requests.post(send_url, json=payload, timeout=10)
        try:
            data = res.json()
        except Exception:
            data = {"status_code": res.status_code, "text": res.text[:200]}
        if res.status_code == 200 and data.get("errcode") == 0:
            log_info(f"微信回复发送成功: touser={touser}, len={len(content)}")
            return True
        log_warn(f"微信回复发送失败: status={res.status_code}, response={data}")
        return False
    except Exception as e:
        log_warn(f"微信回复失败: {e}")
        return False



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


def _route_bool_text(value: bool, true_text: str = "开", false_text: str = "关") -> str:
    return true_text if bool(value) else false_text


def _short_text(value: str, max_len: int = 80) -> str:
    value = str(value or "").strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


def _format_routes_config(max_routes: int = 8) -> str:
    """格式化当前下载路由配置，面向企微菜单查询，保持短而可读。"""
    route_count = len(DOWNLOAD_ROUTES)
    staging = _short_text(STAGING_FOLDER or "未启用", 60)
    lines = [
        "🗺️ 路由配置",
        "━━━━━━━━━━",
        f"默认：/{DEFAULT_DOWNLOAD_ROUTE}",
        f"中转：{staging}",
        f"路由：{route_count} 个",
    ]

    route_names = list(DOWNLOAD_ROUTES.keys())
    if DEFAULT_DOWNLOAD_ROUTE in DOWNLOAD_ROUTES:
        route_names = [DEFAULT_DOWNLOAD_ROUTE] + [name for name in route_names if name != DEFAULT_DOWNLOAD_ROUTE]

    display_names = route_names[:max_routes]
    for name in display_names:
        conf = DOWNLOAD_ROUTES.get(name) or {}
        lines.extend([
            "",
            f"/{name}" + ("  默认" if name == DEFAULT_DOWNLOAD_ROUTE else ""),
            f"路径：{_short_text(conf.get('path') or '-', 80)}",
            f"日期：{_route_bool_text(conf.get('organize_by_date', True))}｜子目录：{_route_bool_text(conf.get('allow_subdir', True), '允许', '禁止')}",
        ])

    if route_count > len(display_names):
        lines.append(f"… 仅显示前 {len(display_names)} 个，共 {route_count} 个")

    issues = []
    if route_count <= 0:
        issues.append("未配置任何路由")
    if DEFAULT_DOWNLOAD_ROUTE not in DOWNLOAD_ROUTES:
        issues.append(f"默认路由 /{DEFAULT_DOWNLOAD_ROUTE} 不存在")
    bad_routes = [name for name, conf in DOWNLOAD_ROUTES.items() if not str(conf.get("path") or "").strip().startswith("/")]
    if bad_routes:
        issues.append("路径非绝对：" + "、".join(f"/{name}" for name in bad_routes))
    if STAGING_FOLDER and not STAGING_FOLDER.startswith("/"):
        issues.append("中转目录不是绝对路径")

    lines.append("")
    if issues:
        lines.append("⚠️ 异常：")
        lines.extend(issues[:5])
        if len(issues) > 5:
            lines.append(f"… 还有 {len(issues) - 5} 项")
    else:
        lines.append("状态：正常")

    return "\n".join(lines)


def _reply_routes_config(user_id: str):
    """回复当前下载路由配置摘要。"""
    send_wechat_reply(user_id, _format_routes_config())


def _get_wechat_access_token(timeout: int = 10) -> tuple[bool, str, str]:
    """获取企微 access_token，返回 (ok, token, message)。"""
    try:
        token_url = f"{WECHAT_PROXY}/cgi-bin/gettoken?corpid={CORP_ID}&corpsecret={APP_SECRET}"
        token_res = requests.get(token_url, timeout=timeout).json()
        access_token = token_res.get("access_token")
        if access_token:
            return True, access_token, "企微 access_token 获取成功"
        err = token_res.get("errmsg") or token_res.get("errcode") or "未知错误"
        return False, "", f"企微 access_token 获取失败: {err}"
    except Exception as e:
        return False, "", f"企微 access_token 请求异常: {e}"


def _cd2_create_folder(folder_path):
    if not CD2_TOKEN:
        log_warn("CD2 创建目录失败：未配置 CD2_TOKEN")
        return False
    try:
        # CD2 CreateFolderRequest 字段是 parentPath + folderName，不是 path
        folder_path = str(folder_path or "").strip().rstrip("/")
        if not folder_path or folder_path == "/":
            return True
        parent_path = "/".join(folder_path.split("/")[:-1]) or "/"
        folder_name = folder_path.split("/")[-1]
        metadata = [("authorization", f"Bearer {CD2_TOKEN}")]
        req = clouddrive_pb2.CreateFolderRequest(parentPath=parent_path, folderName=folder_name)
        with grpc.insecure_channel(CD2_HOST) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            _cd2_rate_limit()
            res = stub.CreateFolder(req, metadata=metadata, timeout=10)
        if res.result.success:
            log_info(f"CD2 目录创建成功: {folder_path}")
            return True
        error_message = res.result.errorMessage or ""
        if "exist" in error_message.lower() or "已存在" in error_message or "存在" in error_message:
            log_info(f"CD2 目录已存在: {folder_path}")
            return True
        log_warn(f"CD2 创建目录返回失败: {folder_path} / {error_message}")
        return False
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.ALREADY_EXISTS:
            log_info(f"CD2 目录已存在: {folder_path}")
            return True
        log_warn(f"CD2 创建目录异常: {e}")
        return False
    except Exception as e:
        log_warn(f"CD2 创建目录异常: {e}")
        return False


def _cd2_get_mount_root_names(force_refresh: bool = False) -> Optional[set]:
    """读取 CD2 顶层云盘/挂载点名称，带短期缓存，避免每个任务反复扫根目录。"""
    if not CD2_TOKEN:
        log_warn("CD2 读取云盘根目录失败：未配置 CD2_TOKEN")
        return None

    # 缓存命中时快速返回；缓存未命中时不持锁调 CD2，避免阻塞其他读线程。
    with cd2_mount_root_cache_lock:
        now = time.time()
        cached_names = cd2_mount_root_cache.get("names")
        cached_ts = float(cd2_mount_root_cache.get("ts") or 0)
        if not force_refresh and cached_names is not None and now - cached_ts < CD2_MOUNT_ROOT_CACHE_TTL_SECS:
            return set(cached_names)

    try:
        metadata = [("authorization", f"Bearer {CD2_TOKEN}")]
        req = clouddrive_pb2.ListSubFileRequest(path="/", forceRefresh=True)
        names = set()
        with grpc.insecure_channel(CD2_HOST) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            _cd2_rate_limit()
            for reply in stub.GetSubFiles(req, metadata=metadata, timeout=10):
                for item in reply.subFiles:
                    file_type = getattr(item, "fileType", None)
                    is_cloud_root = bool(getattr(item, "isCloudRoot", False))
                    is_directory = bool(getattr(item, "isDirectory", False))
                    if file_type != clouddrive_pb2.CloudDriveFile.Directory and not (is_cloud_root or is_directory):
                        continue
                    name = str(getattr(item, "name", "") or "").strip().strip("/")
                    full_path = str(getattr(item, "fullPathName", "") or "").strip().rstrip("/")
                    if name:
                        names.add(name)
                    if full_path.startswith("/") and full_path.count("/") == 1:
                        names.add(full_path.lstrip("/"))
        with cd2_mount_root_cache_lock:
            cd2_mount_root_cache["names"] = set(names)
            cd2_mount_root_cache["ts"] = time.time()
        return names
    except Exception as e:
        log_warn(f"CD2 读取云盘根目录失败: {e}")
        return None


def _cd2_mount_root_exists(root_name: str) -> bool:
    """确认路径第一段是已存在的 CD2 云盘/挂载点，而不是需要新建的普通目录。"""
    root_name = str(root_name or "").strip().strip("/")
    if not root_name:
        return True
    mount_roots = _cd2_get_mount_root_names()
    if mount_roots is None:
        return False
    if root_name in mount_roots:
        return True

    # 缓存里没找到时强制刷新一次，避免刚新增/重命名网盘后被缓存误挡。
    fresh_roots = _cd2_get_mount_root_names(force_refresh=True)
    if fresh_roots is None:
        return False
    if root_name in fresh_roots:
        return True

    log_warn(
        f"CD2 云盘根目录不存在: /{root_name}；"
        f"不会在其它网盘下代建该路径。当前可见根目录: {', '.join(sorted(fresh_roots)) or '空'}"
    )
    return False


def _cd2_ensure_folder_recursive(folder_path: str) -> bool:
    """按层级逐级创建目录。第一段是 CD2 云盘/挂载点，只校验存在，不尝试创建。"""
    folder_path = str(folder_path or "").strip()
    if not folder_path or folder_path == "/":
        return True

    parts = [p for p in folder_path.split("/") if p]
    if not parts:
        return True

    root_name = parts[0]
    if not _cd2_mount_root_exists(root_name):
        log_warn(f"CD2 递归建目录失败，云盘根不存在或不可读取: /{root_name}")
        return False

    current = f"/{root_name}"
    for part in parts[1:]:
        current = f"{current}/{part}"
        ok = _cd2_create_folder(current)
        if not ok:
            log_warn(f"CD2 递归建目录失败，停止在: {current}")
            return False
    return True



def cd2_offline_download(target_url, target_folder, ensure_folder: bool = True):
    if not CD2_TOKEN:
        log_warn("转存失败：未配置 CD2_TOKEN")
        return False, "未配置 CD2_TOKEN"
    try:
        target_folder = (target_folder or "/").strip() or "/"
        log_info(f"开始提交离线任务，目标目录: {target_folder}")
        log_info(f"离线源: {target_url[:200]}")
        if ensure_folder:
            created = _cd2_ensure_folder_recursive(target_folder)
            if not created:
                log_warn(f"目标目录不可用或创建失败，已取消转存提交: {target_folder}")
                return False, f"目标目录不可用或创建失败: {target_folder}"

        metadata = [("authorization", f"Bearer {CD2_TOKEN}")]
        req = clouddrive_pb2.AddOfflineFileRequest(urls=target_url, toFolder=target_folder, checkFolderAfterSecs=0)
        with grpc.insecure_channel(CD2_HOST) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            _cd2_rate_limit()
            res = stub.AddOfflineFiles(req, metadata=metadata, timeout=10)
        if res.success:
            log_info(f"转存提交成功: {target_folder}")
            return True, f"提交成功 → {target_folder}"
        log_warn(f"转存提交失败: {res.errorMessage}")
        return False, f"被拒: {res.errorMessage}"
    except Exception as e:
        log_warn(f"转存系统异常: {e}")
        return False, f"系统异常: {str(e)}"



# --- 中转清洗相关函数 ---

def _cd2_list_offline_files(path: str):
    """查询某路径下的离线任务列表。失败返回 None，避免把网络/API异常误判为空任务。"""
    if not CD2_TOKEN:
        log_warn("查询离线任务失败：未配置 CD2_TOKEN")
        return None
    try:
        metadata = [("authorization", f"Bearer {CD2_TOKEN}")]
        req = clouddrive_pb2.FileRequest(path=path)
        with grpc.insecure_channel(CD2_HOST) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            _cd2_rate_limit()
            res = stub.ListOfflineFilesByPath(req, metadata=metadata, timeout=10)
        return list(res.offlineFiles)
    except Exception as e:
        log_warn(f"查询离线任务失败 {path}: {e}")
        return None


def _cd2_list_directory_files(path: str):
    """用 GetSubFiles 列出目录下的文件和子目录。失败返回 None。"""
    if not CD2_TOKEN:
        log_warn("列出目录失败：未配置 CD2_TOKEN")
        return None
    try:
        metadata = [("authorization", f"Bearer {CD2_TOKEN}")]
        req = clouddrive_pb2.ListSubFileRequest(path=path, forceRefresh=True)
        files = []
        with grpc.insecure_channel(CD2_HOST) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            _cd2_rate_limit()
            for reply in stub.GetSubFiles(req, metadata=metadata, timeout=10):
                for f in reply.subFiles:
                    files.append(f)
        return files
    except Exception as e:
        log_warn(f"列出目录失败 {path}: {e}")
        return None


def _cd2_move_file(src_path: str, dest_folder: str):
    """移动文件到目标目录。"""
    if not CD2_TOKEN:
        return False
    try:
        metadata = [("authorization", f"Bearer {CD2_TOKEN}")]
        req = clouddrive_pb2.MoveFileRequest(
            theFilePaths=[src_path],
            destPath=dest_folder,
            conflictPolicy=clouddrive_pb2.MoveFileRequest.Overwrite
        )
        with grpc.insecure_channel(CD2_HOST) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            _cd2_rate_limit()
            res = stub.MoveFile(req, metadata=metadata, timeout=10)
        if res.success:
            log_info(f"文件移动成功: {src_path} -> {dest_folder}")
            return True
        log_warn(f"文件移动失败: {src_path} -> {dest_folder}: {res.errorMessage}")
        return False
    except Exception as e:
        log_warn(f"文件移动异常: {src_path} -> {dest_folder}: {e}")
        return False


def _cd2_delete_file(path: str):
    """删除单个文件。"""
    if not CD2_TOKEN:
        return False
    try:
        metadata = [("authorization", f"Bearer {CD2_TOKEN}")]
        req = clouddrive_pb2.FileRequest(path=path)
        with grpc.insecure_channel(CD2_HOST) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            _cd2_rate_limit()
            res = stub.DeleteFile(req, metadata=metadata, timeout=10)
        if res.success:
            log_info(f"文件删除成功: {path}")
            return True
        log_warn(f"文件删除失败: {path}: {res.errorMessage}")
        return False
    except Exception as e:
        log_warn(f"文件删除异常: {path}: {e}")
        return False


def _process_staging_directory(dir_path: str, target_folder: str, ext_blacklist: set, threshold: Optional[float], junk_list: list):
    """递归处理中转目录下的子目录：列出文件、清洗垃圾、保留结构。
    返回 (保留条目数, 垃圾文件数)。"""
    import time
    entries = _cd2_list_directory_files(dir_path)
    if entries is None:
        raise RuntimeError(f"列出中转目录失败: {dir_path}")
    if not entries:
        return 0, 0

    keep_count = 0
    junk_count = 0

    for entry in entries:
        if entry.fileType == clouddrive_pb2.CloudDriveFile.File:
            ext = entry.name.rsplit(".", 1)[-1].lower() if "." in entry.name else ""
            size_mb = entry.size / (1024 * 1024) if entry.size else 0
            is_junk = False
            reason = ""
            if ext in ext_blacklist:
                if threshold is not None:
                    if size_mb < threshold:
                        is_junk = True
                        reason = f"{entry.name} ({ext}, {size_mb:.1f}MB < {threshold}MB)"
                else:
                    is_junk = True
                    reason = f"{entry.name} ({ext})"
            if is_junk:
                if _cd2_delete_file(entry.fullPathName):
                    junk_list.append(reason)
                    junk_count += 1
                time.sleep(5)
            else:
                keep_count += 1
        elif entry.fileType == clouddrive_pb2.CloudDriveFile.Directory:
            # 递归处理子目录
            sub_keep, sub_junk = _process_staging_directory(
                entry.fullPathName, target_folder, ext_blacklist, threshold, junk_list
            )
            keep_count += sub_keep
            junk_count += sub_junk

    return keep_count, junk_count


def _process_staging_task(task: dict):
    """处理单个中转任务：下载完成后清洗并转存。"""
    import time
    staging_path = task["staging_path"]
    target_folder = task["target_folder"]
    user_id = task["user_id"]

    log_info(f"开始处理中转任务: {staging_path}")

    # 列出中转目录下的所有条目
    entries = _cd2_list_directory_files(staging_path)
    if entries is None:
        raise RuntimeError(f"列出中转目录失败: {staging_path}")
    if not entries:
        log_info(f"中转目录为空: {staging_path}")
        send_wechat_reply(user_id, f"📦 中转任务完成\n目标目录: {target_folder}\n⚠️ 目录为空，无文件可处理。")
        return

    ext_blacklist = _get_junk_extensions()
    threshold = _get_size_threshold_mb()
    junk_list = []  # 收集所有垃圾文件原因

    moved_items = 0
    deleted_items = 0

    for entry in entries:
        if entry.fileType == clouddrive_pb2.CloudDriveFile.File:
            # 根目录下的文件：判断清洗，保留的移到目标目录
            ext = entry.name.rsplit(".", 1)[-1].lower() if "." in entry.name else ""
            size_mb = entry.size / (1024 * 1024) if entry.size else 0
            is_junk = False
            reason = ""
            if ext in ext_blacklist:
                if threshold is not None:
                    if size_mb < threshold:
                        is_junk = True
                        reason = f"{entry.name} ({ext}, {size_mb:.1f}MB < {threshold}MB)"
                else:
                    is_junk = True
                    reason = f"{entry.name} ({ext})"
            if is_junk:
                if _cd2_delete_file(entry.fullPathName):
                    junk_list.append(reason)
                    deleted_items += 1
                time.sleep(5)
            else:
                if _cd2_move_file(entry.fullPathName, target_folder):
                    moved_items += 1
                time.sleep(5)

        elif entry.fileType == clouddrive_pb2.CloudDriveFile.Directory:
            # 子目录（如磁力链接下载的文件夹 aaa）
            # 1. 递归清洗子目录里的垃圾文件
            sub_keep, sub_junk = _process_staging_directory(
                entry.fullPathName, target_folder, ext_blacklist, threshold, junk_list
            )
            deleted_items += sub_junk

            # 2. 移动整个子目录到目标目录（保持结构）
            if _cd2_move_file(entry.fullPathName, target_folder):
                moved_items += 1
                log_info(f"子目录移动成功: {entry.fullPathName} -> {target_folder}")
            else:
                log_warn(f"子目录移动失败: {entry.fullPathName} -> {target_folder}")
            time.sleep(5)

    # 通知用户
    log_info(f"中转清洗统计: 总条目 {len(entries)} 个, 保留 {moved_items} 个, 垃圾文件 {deleted_items} 个")

    clean_info = ""
    if deleted_items > 0:
        clean_info = f"\n🧹 已清洗垃圾文件: {deleted_items} 个"

    send_wechat_reply(
        user_id,
        f"✅ 中转任务完成\n📦 保留条目: {moved_items} 个\n🤖 目标目录: {target_folder}{clean_info}"
    )


def _staging_cleanup_worker():
    """后台线程：定期扫描中转任务，下载完成后自动清洗。"""
    import time
    log_info("中转清洗监控线程已启动")
    while True:
        time.sleep(5)
        try:
            with staging_lock:
                tasks = list(staging_tasks.items())

            for task_id, task in tasks:
                if task.get("status") != "pending":
                    continue

                staging_path = task["staging_path"]

                # 查离线任务状态
                offline_files = _cd2_list_offline_files(staging_path)
                if offline_files is None:
                    log_warn(f"离线任务状态暂不可读，跳过本轮扫描: {staging_path}")
                    continue
                if not offline_files:
                    # 离线记录为空时，先看是否需要做目录兜底扫描（每 6 个周期一次≈30 秒，避免频繁调 CD2）
                    with staging_lock:
                        cycle = staging_tasks[task_id].get("empty_offline_cycles", 0) + 1
                        staging_tasks[task_id]["empty_offline_cycles"] = cycle
                    if cycle % 6 != 0:
                        continue  # 非扫描周期，跳过

                    entries = _cd2_list_directory_files(staging_path)
                    if entries is None:
                        log_warn(f"中转目录暂不可读，跳过本轮兜底扫描: {staging_path}")
                        continue
                    if not entries:
                        # 还没有离线任务记录，也没有文件，继续等待
                        with staging_lock:
                            staging_tasks[task_id].pop("empty_offline_snapshot", None)
                            staging_tasks[task_id].pop("empty_offline_stable_checks", None)
                        continue

                    snapshot = tuple(sorted(
                        f"{getattr(entry, 'fullPathName', '')}|{getattr(entry, 'fileType', '')}|{getattr(entry, 'size', '')}"
                        for entry in entries
                    ))
                    with staging_lock:
                        old_snapshot = staging_tasks[task_id].get("empty_offline_snapshot")
                        stable_checks = staging_tasks[task_id].get("empty_offline_stable_checks", 0)
                        stable_checks = stable_checks + 1 if old_snapshot == snapshot else 1
                        staging_tasks[task_id]["empty_offline_snapshot"] = snapshot
                        staging_tasks[task_id]["empty_offline_stable_checks"] = stable_checks

                    if stable_checks < 3:
                        log_warn(f"离线任务记录为空但中转目录已有文件，等待目录稳定: {staging_path} ({stable_checks}/3)")
                        continue

                    log_warn(f"离线任务记录为空但中转目录连续稳定，按完成状态处理: {staging_path}")
                    all_finished = True
                    any_error = False
                else:
                    # 检查是否全部完成或出错
                    all_finished = all(f.status == clouddrive_pb2.OFFLINE_FINISHED for f in offline_files)
                    any_error = any(f.status == clouddrive_pb2.OFFLINE_ERROR for f in offline_files)

                if any_error:
                    with staging_lock:
                        staging_tasks[task_id]["status"] = "failed"
                    send_wechat_reply(
                        task["user_id"],
                        f"❌ 中转任务失败\n目标目录: {task['target_folder']}\n⚠️ 有离线任务出错，请检查 CD2 后台。"
                    )
                    try:
                        if _cd2_delete_file(staging_path):
                            entries_after_delete = _cd2_list_directory_files(staging_path)
                            if entries_after_delete is None:
                                log_info(f"失败中转子目录已清理或暂不可读: {staging_path}")
                            else:
                                log_warn(f"失败中转子目录仍可访问，可能未删除干净: {staging_path}")
                        else:
                            log_warn(f"失败中转子目录清理失败（非关键）: {staging_path}")
                    except Exception as e:
                        log_warn(f"失败中转子目录清理失败（非关键）: {staging_path} / {e}")
                    continue

                if not all_finished:
                    continue  # 还有任务在下载中

                # 全部完成，标记为处理中
                with staging_lock:
                    staging_tasks[task_id]["status"] = "processing"

                try:
                    # 处理任务（逐个文件有 5 秒间隔，可能耗时较长）
                    _process_staging_task(task)

                    # 标记完成，并清理空中转子目录
                    with staging_lock:
                        staging_tasks[task_id]["status"] = "completed"
                    # 尝试删除已清空的中转子目录
                    try:
                        if _cd2_delete_file(staging_path):
                            entries_after_delete = _cd2_list_directory_files(staging_path)
                            if entries_after_delete is None:
                                log_info(f"中转子目录已清理或暂不可读: {staging_path}")
                            else:
                                log_warn(f"中转子目录仍可访问，可能未删除干净: {staging_path}")
                        else:
                            log_warn(f"中转子目录清理失败（非关键）: {staging_path}")
                    except Exception as e:
                        log_warn(f"中转子目录清理失败（非关键）: {staging_path} / {e}")
                    time.sleep(1)  # 文件操作收尾
                except Exception as e:
                    log_warn(f"中转任务处理异常: task_id={task_id}, path={staging_path}, error={e}")
                    with staging_lock:
                        staging_tasks[task_id]["status"] = "failed"
                    send_wechat_reply(
                        task["user_id"],
                        f"❌ 中转任务处理异常\n目标目录: {task['target_folder']}\n⚠️ 错误: {e}"
                    )

        except Exception as e:
            log_warn(f"中转监控线程异常: {e}")


def _reply_staging_tasks(user_id: str):
    """回复当前进行中（未完成）的中转任务列表给用户。"""
    with staging_lock:
        tasks = list(staging_tasks.items())

    # 只保留 pending 和 processing 状态的任务
    active_tasks = [
        (task_id, task)
        for task_id, task in tasks
        if task.get("status") in ("pending", "processing")
    ]

    if not active_tasks:
        send_wechat_reply(user_id, "📋 当前没有进行中转任务。")
        return

    status_map = {
        "pending": "⏳ 正在离线下载",
        "processing": "🧹 正在清理垃圾文件/转存中",
    }

    lines = ["📋 进行中转任务列表："]
    for task_id, task in active_tasks:
        status = task.get("status", "unknown")
        submitted = task.get("submitted_at", "未知")
        target = task.get("target_folder", "未知")
        status_text = status_map.get(status, f"未知状态({status})")
        lines.append(f"\n🆔 {task_id}\n🕐 {submitted}\n📍 {status_text}\n🎯 目标: {target}")

    send_wechat_reply(user_id, "\n".join(lines))


# --- 启动中转监控线程 ---
if STAGING_FOLDER:
    _cd2_ensure_folder_recursive(STAGING_FOLDER)
    cleanup_thread = threading.Thread(target=_staging_cleanup_worker, daemon=True)
    cleanup_thread.start()
    log_info("中转清洗后台线程已启动")


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
    else:
        # 兼容默认路由下的「子目录 + 链接」写法：国产 magnet:?xt=... 或 国产\nmagnet:?xt=...
        route_conf = _get_route_config(route)
        if route_conf and route_conf.get("allow_subdir", True):
            first_line, *rest_lines = text.splitlines()
            first_parts = first_line.split(maxsplit=1)
            if len(first_parts) == 2:
                maybe_dir = first_parts[0].strip()
                maybe_payload = first_parts[1].strip()
                normalized = _normalize_download_url(maybe_payload)
                if maybe_dir and not _is_supported_download_url(maybe_dir) and (
                    _is_supported_download_url(maybe_payload) or normalized != maybe_payload
                ):
                    custom_subdir = maybe_dir
                    payload = "\n".join([maybe_payload, *rest_lines]).strip()
            elif len(first_parts) == 1 and rest_lines:
                maybe_dir = first_parts[0].strip()
                remaining_payload = "\n".join(rest_lines).strip()
                first_payload_line = next((line.strip() for line in rest_lines if line.strip()), "")
                normalized = _normalize_download_url(first_payload_line)
                if maybe_dir and not _is_supported_download_url(maybe_dir) and (
                    _is_supported_download_url(first_payload_line) or normalized != first_payload_line
                ):
                    custom_subdir = maybe_dir
                    payload = remaining_payload

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

    # 查询中转任务状态
    if content.lower() in ("/tasks", "/status"):
        _reply_staging_tasks(from_user)
        return

    # 查询路由配置
    if content.lower() in ("/routes", "/route", "路由配置"):
        _reply_routes_config(from_user)
        return

    if content.lower() in ("/help", "help", "使用说明"):
        _reply_usage_help(from_user)
        return

    if content.lower() in ("/health", "/check", "健康检查"):
        _reply_health_check(from_user)
        return

    if content.startswith("/") and len(content.split()) == 1:
        route_name = content[1:].strip().lower()
        if _get_route_config(route_name):
            send_wechat_reply(
                from_user,
                "⚠️ 用法示例：\n"
                f"1. 直接离线到默认目录：E808151805F0...73FF2\n"
                f"2. 离线到 {route_name}：/{route_name} E808151805F0...73FF2\n"
                f"3. 离线到自定义子目录：/{route_name} 电影名 E808151805F0...73FF2"
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
        log_info(f"路由解析成功: route={parsed['route']}, subdir={parsed['custom_subdir'] or '-'}, target_folder={target_folder}")

        # 分离 ed2k 和 magnet
        ed2k_urls = []
        magnet_urls = []
        for target_url in parsed["target_urls"]:
            if target_url.lower().startswith("ed2k://"):
                ed2k_urls.append(target_url)
            else:
                magnet_urls.append(target_url)

        # ed2k 直接提交，不清洗
        ed2k_success = 0
        ed2k_fail = 0
        ed2k_fail_reasons = []
        for target_url in ed2k_urls:
            success, detail = cd2_offline_download(target_url, target_folder=target_folder)
            if success:
                ed2k_success += 1
            else:
                ed2k_fail += 1
                ed2k_fail_reasons.append(detail)

        # magnet 走中转清洗（如果配置了 STAGING_FOLDER）
        staging_task_id = None
        if STAGING_FOLDER and magnet_urls:
            mag_success = 0
            mag_fail = 0
            mag_fail_reasons = []
            task_subfolder = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
            staging_sub_path = _join_path(STAGING_FOLDER, task_subfolder)
            staging_ready = _cd2_ensure_folder_recursive(staging_sub_path)
            if not staging_ready:
                mag_success = 0
                mag_fail = len(magnet_urls)
                mag_fail_reasons.append(f"中转目录不可用或创建失败: {staging_sub_path}")
            else:
                for target_url in magnet_urls:
                    success, detail = cd2_offline_download(target_url, target_folder=staging_sub_path, ensure_folder=False)
                    if success:
                        mag_success += 1
                    else:
                        mag_fail += 1
                        mag_fail_reasons.append(detail)

            if mag_success == 0:
                log_warn(f"magnet 提交全部失败: {mag_fail_reasons[0] if mag_fail_reasons else '未知错误'}")
            else:
                task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
                with staging_lock:
                    staging_tasks[task_id] = {
                        "urls": magnet_urls,
                        "target_folder": target_folder,
                        "user_id": from_user,
                        "status": "pending",
                        "staging_path": staging_sub_path,
                        "submitted_at": datetime.now().isoformat(),
                    }
                staging_task_id = task_id

                extra = f"\n📂 子目录: {parsed['custom_subdir']}" if parsed["custom_subdir"] else ""
                fail_note = ""
                if mag_fail > 0:
                    fail_note = f"\n⚠️ magnet 提交失败: {mag_fail} 个"
                send_wechat_reply(
                    from_user,
                    f"📦 已提交到中转目录\n"
                    f"📦 magnet 成功: {mag_success}/{len(magnet_urls)}{extra}{fail_note}\n"
                    f"🤖 中转路径: {staging_sub_path}\n"
                    f"⏳ 下载完成后自动清洗并转存到目标目录..."
                )
        elif magnet_urls:
            # 没配置 STAGING_FOLDER，magnet 直接提交
            mag_success = 0
            mag_fail = 0
            mag_fail_reasons = []
            for target_url in magnet_urls:
                success, detail = cd2_offline_download(target_url, target_folder=target_folder)
                if success:
                    mag_success += 1
                else:
                    mag_fail += 1
                    mag_fail_reasons.append(detail)
        else:
            mag_success = 0
            mag_fail = 0
            mag_fail_reasons = []

        # 统一回复提交结果
        extra = f"\n📂 子目录: {parsed['custom_subdir']}" if parsed["custom_subdir"] else ""
        total_ed2k = len(ed2k_urls)
        total_mag = len(magnet_urls)

        if not ed2k_urls and not magnet_urls:
            send_wechat_reply(from_user, "⚠️ 没有可提交的链接。")
            return

        parts = []
        if ed2k_urls:
            if ed2k_fail == 0 and ed2k_success == total_ed2k:
                parts.append(f"📦 ed2k: ✅ {ed2k_success}/{total_ed2k}")
            elif ed2k_success == 0:
                parts.append(f"📦 ed2k: ❌ {total_ed2k}/{total_ed2k} — {ed2k_fail_reasons[0] if ed2k_fail_reasons else '未知错误'}")
            else:
                parts.append(f"📦 ed2k: ⚠️ 成功 {ed2k_success} / 失败 {ed2k_fail} — {ed2k_fail_reasons[0] if ed2k_fail_reasons else ''}")

        if magnet_urls and not staging_task_id:
            if mag_fail == 0 and mag_success == total_mag:
                parts.append(f"📦 链接: ✅ {mag_success}/{total_mag}")
            elif mag_success == 0:
                parts.append(f"📦 链接: ❌ {total_mag}/{total_mag} — {mag_fail_reasons[0] if mag_fail_reasons else '未知错误'}")
            else:
                parts.append(f"📦 链接: ⚠️ 成功 {mag_success} / 失败 {mag_fail} — {mag_fail_reasons[0] if mag_fail_reasons else ''}")

        if parts:
            send_wechat_reply(
                from_user,
                "\n".join(parts) + f"\n🤖 目标目录: {target_folder}{extra}"
            )
        return

    send_wechat_reply(
        from_user,
        "⚠️ 当前版本仅支持直接离线链接，不再提供搜索功能。\n"
        "请发送 magnet / ed2k / http(s) / 40位hash，或使用：目录名 + 链接 / /路由名 + 链接。"
    )


def _reply_usage_help(user_id: str):
    """回复简短使用说明。"""
    routes_text = _get_available_routes_text() or "未配置"
    send_wechat_reply(
        user_id,
        "📖 CD2 转存使用说明\n\n"
        "支持：magnet / ed2k / http(s) 链接 / 40位 hash\n\n"
        "1. 默认目录\n"
        "直接发送链接或 hash：\n"
        "E808151805F0...73FF2\n\n"
        "2. 指定路由\n"
        "/sub magnet:?xt=urn:btih:...\n\n"
        "3. 指定默认路由子目录\n"
        "国产 magnet:?xt=urn:btih:...\n\n"
        "4. 指定路由子目录\n"
        "/sub 电影名 magnet:?xt=urn:btih:...\n\n"
        "5. 批量提交\n"
        "国产\n"
        "magnet:?xt=urn:btih:...\n"
        "ed2k://|file|xxx.mkv|123456|HASH|/\n\n"
        "6. 查询任务\n"
        "/tasks 或 /status\n\n"
        "7. 查询路由配置\n"
        "/routes 或 /route\n\n"
        "8. 健康检查\n"
        "/health 或 /check\n\n"
        f"可用路由：{routes_text}"
    )


def _format_check(ok: bool, name: str, detail: str = "") -> str:
    icon = "✅" if ok else "❌"
    return f"{icon} {name}" + (f"：{detail}" if detail else "")


def _run_health_checks() -> list[str]:
    """执行只读健康检查，返回适合发给企微的行列表。"""
    lines = ["🩺 CD2 转存机器人健康检查"]

    # 配置检查
    required_env = {
        "CORP_ID": CORP_ID,
        "APP_SECRET": APP_SECRET,
        "AGENT_ID": AGENT_ID,
        "APP_TOKEN": APP_TOKEN,
        "ENCODING_AES_KEY": ENCODING_AES_KEY,
        "CD2_HOST": CD2_HOST,
        "CD2_TOKEN": CD2_TOKEN,
    }
    missing = [name for name, val in required_env.items() if not str(val or "").strip()]
    lines.append(_format_check(not missing, "环境变量", "完整" if not missing else "缺少 " + ", ".join(missing)))

    threshold_raw = str(JUNK_SIZE_THRESHOLD_MB or "").strip()
    threshold_ok = True
    threshold_detail = "未设置"
    if threshold_raw:
        try:
            float(threshold_raw)
            threshold_detail = threshold_raw
        except ValueError:
            threshold_ok = False
            threshold_detail = f"无效值 {threshold_raw}"
    lines.append(_format_check(threshold_ok, "清洗体积阈值", threshold_detail))

    route_count = len(DOWNLOAD_ROUTES)
    default_ok = DEFAULT_DOWNLOAD_ROUTE in DOWNLOAD_ROUTES
    route_detail = f"{route_count} 个路由，默认 /{DEFAULT_DOWNLOAD_ROUTE}" if default_ok else f"默认路由无效: {DEFAULT_DOWNLOAD_ROUTE}"
    lines.append(_format_check(route_count > 0 and default_ok, "下载路由配置", route_detail))

    bad_routes = [name for name, conf in DOWNLOAD_ROUTES.items() if not str(conf.get("path") or "").strip().startswith("/")]
    lines.append(_format_check(not bad_routes, "路由路径", "均为绝对路径" if not bad_routes else "异常: " + ", ".join(bad_routes)))
    lines.append(_format_check(bool(STAGING_FOLDER), "中转清洗目录", STAGING_FOLDER or "未启用"))

    # CD2 网络 / 服务检查
    try:
        with grpc.insecure_channel(CD2_HOST) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            _cd2_rate_limit()
            info = stub.GetSystemInfo(Empty(), timeout=5)
        user = getattr(info, "UserName", "") or getattr(info, "userName", "") or getattr(info, "username", "") or "已连接"
        ready = getattr(info, "SystemReady", None)
        detail = str(user) if ready is None else f"{user} / SystemReady={ready}"
        lines.append(_format_check(True, "CD2 服务连接", detail))
    except grpc.RpcError as e:
        lines.append(_format_check(False, "CD2 服务连接", f"{e.code().name}: {e.details()}"))
    except Exception as e:
        lines.append(_format_check(False, "CD2 服务连接", str(e)))

    # CD2 Token 授权检查（只读）
    if CD2_TOKEN:
        try:
            metadata = [("authorization", f"Bearer {CD2_TOKEN}")]
            with grpc.insecure_channel(CD2_HOST) as channel:
                stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
                _cd2_rate_limit()
                stub.GetAccountStatus(Empty(), metadata=metadata, timeout=5)
            lines.append(_format_check(True, "CD2 Token 授权", "可用"))
        except grpc.RpcError as e:
            lines.append(_format_check(False, "CD2 Token 授权", f"{e.code().name}: {e.details()}"))
        except Exception as e:
            lines.append(_format_check(False, "CD2 Token 授权", str(e)))

    # 企微 API 检查
    ok, _, message = _get_wechat_access_token(timeout=5)
    lines.append(_format_check(ok, "企微 API", message))

    with staging_lock:
        active = sum(1 for task in staging_tasks.values() if task.get("status") in ("pending", "processing"))
    lines.append(_format_check(True, "中转任务", f"进行中 {active} 个"))

    return lines


def _reply_health_check(user_id: str):
    """执行健康检查并回复。"""
    try:
        lines = _run_health_checks()
        send_wechat_reply(user_id, "\n".join(lines))
    except Exception as e:
        log_warn(f"健康检查异常: {e}")
        send_wechat_reply(user_id, f"❌ 健康检查异常：{e}")


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
                with recent_msg_ids_lock:
                    if msg_id in recent_msg_ids:
                        log_info(f"消息去重跳过: msg_id={msg_id}")
                        return "success"
                    recent_msg_ids.append(msg_id)
                    if len(recent_msg_ids) > 100:
                        recent_msg_ids.pop(0)

            msg_type_node = tree.find("MsgType")
            if msg_type_node is None:
                log_warn("企微回调缺少 MsgType 节点")
                return "success"
            msg_type = msg_type_node.text

            from_user_node = tree.find("FromUserName")
            if from_user_node is None:
                log_warn("企微回调缺少 FromUserName 节点")
                return "success"
            from_user = from_user_node.text

            if msg_type == "text":
                content_node = tree.find("Content")
                if content_node is None or content_node.text is None:
                    log_warn("企微回调缺少 Content 节点或内容为空")
                    return "success"
                content = content_node.text.strip()
                log_info(f"收到企微消息: from={from_user}, content={content[:100]}")
                threading.Thread(target=process_message_async, args=(from_user, content)).start()
            elif msg_type == "event":
                event_node = tree.find("Event")
                event_key_node = tree.find("EventKey")
                if event_node is not None and event_key_node is not None:
                    event = event_node.text
                    event_key = event_key_node.text
                    log_info(f"收到企微菜单事件: event={event}, key={event_key}, from={from_user}")
                    if event == "click" and event_key == "status":
                        _reply_staging_tasks(from_user)
                    elif event == "click" and event_key == "routes":
                        _reply_routes_config(from_user)
                    elif event == "click" and event_key == "help":
                        _reply_usage_help(from_user)
                    elif event == "click" and event_key == "health":
                        threading.Thread(target=_reply_health_check, args=(from_user,), daemon=True).start()
                else:
                    log_warn("企微事件消息缺少 Event 或 EventKey 节点")
            else:
                log_info(f"收到非文本消息: msg_type={msg_type}, from={from_user}")

            return "success"
        except Exception as e:
            log_warn(f"企微回调处理异常: {e}")
            import traceback
            log_warn(traceback.format_exc())
            return "success"



def init_wechat_menu():
    """尝试自动初始化企业微信应用自定义菜单。"""
    try:
        ok, access_token, message = _get_wechat_access_token(timeout=10)
        if not ok:
            log_warn(f"企微菜单初始化失败：{message}")
            return

        menu_data = {
            "button": [
                {
                    "name": "查询",
                    "sub_button": [
                        {
                            "type": "click",
                            "name": "任务状态",
                            "key": "status"
                        },
                        {
                            "type": "click",
                            "name": "路由配置",
                            "key": "routes"
                        }
                    ]
                },
                {
                    "type": "click",
                    "name": "使用说明",
                    "key": "help"
                },
                {
                    "type": "click",
                    "name": "健康检查",
                    "key": "health"
                }
            ]
        }

        menu_url = f"{WECHAT_PROXY}/cgi-bin/menu/create?access_token={access_token}&agentid={AGENT_ID}"
        res = requests.post(menu_url, json=menu_data, timeout=10).json()
        if res.get("errcode") == 0:
            log_info("企微应用菜单初始化成功：查询(任务状态/路由配置) / 使用说明 / 健康检查")
        elif res.get("errcode") == 46003:
            log_info("企微应用菜单已存在，无需重复创建")
        else:
            log_warn(f"企微菜单初始化失败: {res}")
    except Exception as e:
        log_warn(f"企微菜单初始化异常: {e}")


# 尝试初始化企微菜单
init_wechat_menu()

log_info("转存功能模块初始化成功")

if __name__ == "__main__":
    log_info("机器人已启动，监听 5000 端口...")
    app.run(host="0.0.0.0", port=5000)
