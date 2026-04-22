# 🤖 企业微信 CD2 离线下载机器人 (WeChat CD2 Bot)

基于 Python + Flask + gRPC 构建的企业微信机器人。将你的企业微信打造成一个**全自动找资源 + 离线下载的超级中枢**！发送车牌号，一键检索并推送到本地的 CloudDrive2 进行离线下载。

## ✨ 核心功能 (Features)

* 🧲 **直链解析：** 直接发送磁力链接 (`magnet:?`) 或 种子下载链接 (`http://...*.torrent`)，秒推 CD2 离线下载。
* 🔍 **聚合搜索：** 发送番号/车牌或电影关键词，自动调用本地 Prowlarr 接口进行全网 BT 站（如 Sukebei、BTDigg 等）检索。
* 🔢 **交互式选择：** 搜索完毕后返回带文件大小和做种人数的资源列表，**只需回复序号（如 `1`、`2`）即可精准下载**。
* ⚡ **底层通信：** 彻底抛弃低效的网页模拟，采用官方标准的 **gRPC 协议 + JWT Token** 与 CloudDrive2 通信，极速且稳定。
* 🛡️ **防重防抖：** 内置异步线程与消息去重机制，完美绕过企业微信服务器“5秒内无响应自动重试三次”的变态机制。

---

## 📦 准备工作 (Prerequisites)

在开始部署之前，你需要准备好以下基础设施：

1.  **企业微信管理员权限：** 需要创建一个【自建应用】，并获取 `CORP_ID`, `APP_SECRET`, `AGENT_ID`, `APP_TOKEN`, `ENCODING_AES_KEY`。
2.  **企业微信 API 反向代理：** 企微新规要求回调地址必须有固定 IP。你需要一台拥有公网固定 IP 的服务器搭建反代（如 Nginx），代理目标为 `https://qyapi.weixin.qq.com`。
3.  **CloudDrive2：** 运行在本地 NAS/PVE 上。需在后台生成 **API 令牌 (Token)**。
4.  **Prowlarr：** 运行在本地 NAS/PVE 上的聚合索引器。需配置好常用的 Indexer（如 Sukebei），并获取 **API Key**。

---

## 🚀 部署指南 (Deployment)

推荐使用 Docker Compose 进行部署。
🛠️ 第一部分：Prowlarr 部署与配置 (聚合搜索中心)
Prowlarr 是本系统的“眼睛”，负责从全球各大索引站抓取资源。

1. Docker 部署
在你的 PVE 或 NAS 上创建 prowlarr 目录，新建 docker-compose.yml：

YAML
version: '3'
services:
  prowlarr:
    image: ghcr.io/linuxserver/prowlarr:latest
    container_name: prowlarr
    environment:
      - PUID=0
      - PGID=0
      - TZ=Asia/Shanghai
    ports:
      - 9696:9696
    restart: unless-stopped
运行 docker compose up -d 启动。

2. 初始化教程
访问后台： 浏览器打开 http://你的IP:9696。

添加索引器 (Indexer)：

点击左侧 Indexers -> Add Indexer。

搜索并添加 Sukebei (找车牌神器) 和 Solid Torrents (通用搜索)。

点击 Save 保存。

获取 API Key：

前往 Settings -> General -> 复制 API Key（稍后填入机器人配置）。

### 1. 创建 `docker-compose.yml`

新建一个目录，创建并编辑 `docker-compose.yml` 文件：

```yaml
version: '3.8'

services:
  qywx-cd2-bot:
    image: ghcr.io/jiumian8/qywx-cd2-bot:latest
    container_name: qywx-cd2-bot
    restart: unless-stopped
    ports:
      - "5110:5000"  # 左侧可以改为你想要暴露的外部端口
    environment:   
      # --- 企业微信凭证 ---
      - CORP_ID=企业ID
      - APP_SECRET=你的自建应用Secret
      - AGENT_ID=应用id
      - APP_TOKEN=你的接收消息Token
      - ENCODING_AES_KEY=你的43位消息加解密Key
      
      # --- 企业微信 API 代理 ---
      - WECHAT_PROXY=http://你的反向代理IP:端口
      
      # --- CloudDrive2 配置 ---
      - CD2_HOST=192.168.x.x:19798       # CD2的内网IP和端口，不要带 http://
      - CD2_TOKEN=你的CD2_API令牌         # token权限至少要给离线下载 我是网盘那个都给了 具体可以看看cd2文档
      - DOWNLOAD_PATH=/115/离线下载目录   # CD2中真实存在的挂载路径
      
      # --- Prowlarr 聚合搜索配置 ---
      - PROWLARR_URL=http://192.168.x.x:9696  # Prowlarr的内网地址，必须带 http://
      - PROWLARR_API_KEY=你的Prowlarr_API_Key
```
### 2. 配置企业微信回调

前往企业微信后台 -> 应用管理 -> 你的应用 -> 接收消息 -> 设置 API 接收。
* **URL:** 填写 `http://你的公网穿透域名或IP:5000/wechat`  **(注意结尾必须带 `/wechat`)**
* **Token / EncodingAESKey:** 与 docker-compose 中的配置保持一致。
点击保存，提示成功即可！


## 💡 使用说明 (Usage)

直接在微信中找到你的自建应用机器人，发送消息即可交互：

* **场景 1：直接下载**
    发送：`magnet:?xt=urn:btih:XXXXXX`
    回复：✅ 直链离线成功

* **场景 2：搜索资源**
    发送：`SDMM-229` 或 `漫威`
    回复：
    > 🔍 找到 8 个结果，请直接回复【序号】下载：
    > 1. [4.60 GB] SDMM-229 高清版 (源:sukebei 种:123)
    > 2. [2.10 GB] SDMM-229 压缩版 (源:btdigg 种:45)

* **场景 3：选择下载**
    发送：`1`
    回复：✅ 离线任务建立成功！

