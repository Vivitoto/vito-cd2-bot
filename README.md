# 🤖 企业微信 CD2 离线下载机器人 (qywx-cd2-bot)

基于 Python + Flask + gRPC 构建、使用 Gunicorn 运行的企业微信机器人。将你的企业微信打造成一个**直链 / 磁链 / ED2K 离线下载中枢**，把消息直接推送到本地的 CloudDrive2 进行离线下载。

> 本项目 fork 自 [jiumian8/jiumian-cd2-bot](https://github.com/jiumian8/jiumian-cd2-bot)，在此基础上重构为**YAML 路由化下载目录**，并新增了**按路由独立日期归档**、**自定义离线子目录**、**ED2K 支持**、**多链接批量提交**等功能。

## ✨ 核心功能 (Features)

* 🧲 **直链解析：** 直接发送磁力链接 (`magnet:?`)、种子下载链接 (`http://...*.torrent`)、ED2K 链接 (`ed2k://...`) 或 40 位 info_hash，秒推 CD2 离线下载。
* ⚡ **底层通信：** 彻底抛弃低效的网页模拟，采用官方标准的 **gRPC 协议 + JWT Token** 与 CloudDrive2 通信，极速且稳定。
* 🛡️ **防重防抖：** 内置异步线程与消息去重机制，完美绕过企业微信服务器“5秒内无响应自动重试三次”的变态机制。
* 📅 **日期归档（新增）：** 每个下载路由可独立决定是否在目标路径末尾创建 `YYYY-MM-DD` 日期目录。
* 🗂️ **YAML 路由配置：** 下载目录改为 `download-routes.yml` 管理，可自由定义 `/main`、`/sub`、`/temp` 等任意路由。
* 🪄 **自定义子目录：** 支持 `/sub @你好 磁链/哈希` 这种格式，自动落到类似 `/115open/手动转存/@你好/2026-04-22` 的路径，不存在则自动创建。
* 📦 **批量提交：** 支持一条消息中提交多个 magnet / ed2k / 直链，统一落到同一路径。

---

## 📦 准备工作 (Prerequisites)

在开始部署之前，你需要准备好以下基础设施：

1.  **企业微信管理员权限：** 需要创建一个【自建应用】，并获取 `CORP_ID`, `APP_SECRET`, `AGENT_ID`, `APP_TOKEN`, `ENCODING_AES_KEY`。
2.  **企业微信 API 反向代理：** 企微新规要求回调地址必须有固定 IP。你需要一台拥有公网固定 IP 的服务器搭建反代（如 Nginx），代理目标为 `https://qyapi.weixin.qq.com`。
3.  **CloudDrive2：** 运行在本地 NAS/PVE 上。需在后台生成 **API 令牌 (Token)**。

---

## 🚀 部署指南 (Deployment)

推荐使用 Docker Compose 进行部署。

### 🛠️ 部署：qywx-cd2-bot

#### 1. 创建 `docker-compose.yml`

新建一个目录，创建并编辑 `docker-compose.yml` 文件：

```yaml
version: '3.8'

services:
  qywx-cd2-bot:
    image: vivitoto/qywx-cd2-bot:latest
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
      - CD2_HOST=192.168.x.x:19798            # CD2 的内网 IP 和端口，不要带 http://
      - CD2_TOKEN=你的CD2_API令牌              # token 权限至少要给离线下载（建议网盘权限全开）

      # --- 下载路由配置文件路径 ---
      - DOWNLOAD_ROUTES_CONFIG=/config/download-routes.yml
    volumes:
      - ./config:/config
```

> 💡 **首次启动说明**
> - 容器首次启动时，若 `/config/download-routes.yml` 不存在，会自动从镜像里的示例文件初始化一份。
> - 初始化后可直接编辑宿主机上的 `./config/download-routes.yml`。
> - 修改路由配置后，重启容器即可生效。
> - 容器现在使用 **Gunicorn** 启动，不再出现 Flask development server 的那条警告。

#### 1.1 `download-routes.yml` 示例

> 容器首次启动时会自动生成这个文件。你可以直接改宿主机上的 `./config/download-routes.yml`。
> 文件里已经带详细中文注释；下面是精简版结构说明：

```yaml
default_route: main

routes:
  main:
    path: /115open/磁力
    organize_by_date: true
    allow_subdir: true
    comment: 默认离线目录

  sub:
    path: /115open/手动转存
    organize_by_date: true
    allow_subdir: true
    comment: 手动转存目录

  temp:
    path: /115open/临时
    organize_by_date: false
    allow_subdir: false
    comment: 临时目录，不允许自定义子目录
```

字段说明：
- `default_route`：默认路由。直接发 magnet / ed2k / hash 时走它。
- `routes.<路由名>.path`：这个路由对应的基础目录。
- `routes.<路由名>.organize_by_date`：是否自动在最后追加日期目录。
- `routes.<路由名>.allow_subdir`：是否允许 `/路由名 @子目录 链接` 这种写法。
- `routes.<路由名>.comment`：备注说明，只给人看。

> 💡 **路径示例**
> - 默认磁链 → `/115open/磁力/2026-04-22`
> - `/sub E808...` → `/115open/手动转存/2026-04-22`
> - `/sub @你好 E808...` → `/115open/手动转存/@你好/2026-04-22`
> - `/temp E808...` → `/115open/临时`

#### 2. 配置企业微信回调

前往企业微信后台 -> 应用管理 -> 你的应用 -> 接收消息 -> 设置 API 接收。

- **URL:** 若按上面的端口映射 `5110:5000` 部署，则填写 `http://你的公网穿透域名或IP:5110/wechat` **(注意结尾必须带 `/wechat`)**
- **Token / EncodingAESKey:** 与 docker-compose 中的配置保持一致。

点击保存，提示成功即可！

---

## 💡 使用说明 (Usage)

直接在微信中找到你的自建应用机器人，发送消息即可交互：

### 场景 1：直接下载

发送：`magnet:?xt=urn:btih:XXXXXX`

回复：✅ 离线任务建立成功 → `/115open/磁力/2026-04-22`

### 场景 1.1：直接发送 ed2k

发送：`ed2k://|file|demo.mkv|123456|ABCDEF1234567890ABCDEF1234567890|/`

回复：✅ 离线任务建立成功 → `/115open/磁力/2026-04-22`

### 场景 2：离线到 sub 目录

发送：`/sub E808151805F0A2C8C281FBEFA682AD29EDA73FF2`

回复：✅ 离线任务建立成功！→ `/115open/手动转存/2026-04-22`

### 场景 3：离线到自定义子目录

发送：`/sub @你好 E808151805F0A2C8C281FBEFA682AD29EDA73FF2`

回复：✅ 离线任务建立成功！→ `/115open/手动转存/@你好/2026-04-22`

### 场景 4：一次提交多个 magnet / ed2k

发送：
```text
/sub @你好
ed2k://|file|a.mkv|111|HASH1|/
magnet:?xt=urn:btih:E808151805F0A2C8C281FBEFA682AD29EDA73FF2
```

回复：✅ 离线任务建立成功，统一落到 `/115open/手动转存/@你好/2026-04-22`

<!-- build trigger: 2026-04-22T03:47:19.216004 -->
