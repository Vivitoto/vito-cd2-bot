# 🤖 企业微信 CD2 离线下载机器人 (qywx-cd2-bot)

基于 Python + Flask + gRPC 构建、使用 Gunicorn 运行的企业微信机器人。将你的企业微信打造成一个**直链 / 磁链 / ED2K 离线下载中枢**，把消息直接推送到本地的 CloudDrive2 进行离线下载。

> 本项目 fork 自 [jiumian8/jiumian-cd2-bot](https://github.com/jiumian8/jiumian-cd2-bot)，在此基础上重构为**YAML 路由化下载目录**，并新增了**按路由独立日期归档**、**自定义离线子目录**、**ED2K 支持**、**多链接批量提交**等功能。

---

## ✨ 核心功能

| 功能 | 说明 |
|---|---|
| 🧲 直链解析 | 直接发送 magnet / ed2k / http(s) / 40 位 hash，秒推 CD2 |
| ⚡ 底层通信 | 采用官方标准的 **gRPC 协议 + JWT Token** 与 CloudDrive2 通信 |
| 🛡️ 防重防抖 | 内置异步线程与消息去重机制，绕过企微 5 秒重试机制 |
| 📅 日期归档 | 每个路由可独立决定是否自动追加 `YYYY-MM-DD` 日期目录 |
| 🗂️ YAML 路由 | 下载目录统一由 `download-routes.yml` 管理 |
| 🪄 自定义子目录 | 支持 `/路由名 @自定义目录 链接` 格式 |
| 📦 批量提交 | 一条消息内多个链接统一落到同一路径 |
| 🧹 清洗过滤 | 按后缀黑名单 + 体积阈值过滤垃圾文件 |
| 🔄 中转清洗 | 磁力链接先下载到中转目录，完成后根据真实文件自动清洗并转存（ed2k 直接提交） |
| 📋 任务查询 | 发送 `/tasks` 或点击企微菜单"任务状态"查看未完成任务 |

---

## 📦 准备工作

1. **企业微信管理员权限**：创建【自建应用】，获取 `CORP_ID`, `APP_SECRET`, `AGENT_ID`, `APP_TOKEN`, `ENCODING_AES_KEY`。
2. **企业微信 API 反向代理**：企微新规要求回调地址必须有固定 IP。用一台公网服务器搭建反代（如 Nginx），代理目标为 `https://qyapi.weixin.qq.com`。
3. **CloudDrive2**：运行在本地 NAS / PVE，在后台生成 **API Token**。
4. **网盘空间**：在 CD2 里确认有一个用于离线下载的网盘目录。

---

## 🚀 部署指南

### 1. docker-compose.yml

```yaml
version: '3.8'

services:
  qywx-cd2-bot:
    image: vivitoto/qywx-cd2-bot:latest
    container_name: qywx-cd2-bot
    restart: unless-stopped
    ports:
      - "5110:5000"
    environment:
      # --- 企业微信凭证 ---
      - CORP_ID=你的企业ID
      - APP_SECRET=你的应用Secret
      - AGENT_ID=你的应用ID
      - APP_TOKEN=你的接收消息Token
      - ENCODING_AES_KEY=你的43位消息加解密Key

      # --- 企业微信 API 代理 ---
      - WECHAT_PROXY=http://你的反代IP:端口

      # --- CloudDrive2 配置 ---
      - CD2_HOST=192.168.x.x:19798    # CD2 内网 IP 和端口，不要带 http://
      - CD2_TOKEN=你的CD2_API令牌    # 建议网盘权限全开

      # --- 清洗过滤配置（可选） ---
      - ENABLE_CLEANUP=false
      - JUNK_EXTENSIONS=txt,url,html,mhtml,htm,mht,mp4,exe,rar,apk,gif,png,jpg
      - JUNK_SIZE_THRESHOLD_MB=       # 留空则不执行体积过滤
    volumes:
      - ./config:/config
```

### 2. 下载路由配置

容器首次启动时，若 `/config/download-routes.yml` 不存在，会自动从镜像内的示例文件初始化一份。

示例内容：

```yaml
default_route: main

# 全局中转清洗目录（所有路由共用）
# 配置了则磁力链接先下载到这里，完成后自动清洗并转存到目标目录
# 留空或删除则磁力链接直接提交到目标目录，ed2k 始终直接提交
# 示例：staging_folder: /网盘/staging
staging_folder:

routes:
  main:
    path: /网盘/磁力
    organize_by_date: true         # true=自动追加日期目录
    allow_subdir: true             # true=支持 /main @自定义目录 链接
    comment: 默认离线目录

  sub:
    path: /网盘/手动转存
    organize_by_date: true
    allow_subdir: true
    comment: 手动转存目录

  temp:
    path: /网盘/临时
    organize_by_date: false
    allow_subdir: false
    comment: 临时目录，不允许自定义子目录
```

**字段说明：**

| 字段 | 说明 |
|---|---|
| `path` | 基础目录，离线任务默认存到这里 |
| `organize_by_date` | `true` 时自动追加 `YYYY-MM-DD` 日期目录 |
| `allow_subdir` | `true` 时支持 `/路由名 @自定义目录 链接` 格式 |
| `comment` | 备注，只给人看 |
| `staging_folder`（全局） | 磁力中转清洗目录，所有路由共用；留空则不走中转 |

修改后重启容器即可生效。

### 3. 配置企微回调

前往企微后台 → 应用管理 → 你的应用 → 接收消息 → 设置 API 接收：

- **URL:** `http://你的公网域名或IP:5110/wechat`（**结尾必须带 `/wechat`**）
- **Token / EncodingAESKey:** 与 docker-compose 中保持一致

点击保存即可。

---

## 🧹 清洗过滤说明

清洗仅对**磁力链接**生效（走中转时根据下载完成的**真实文件**判断）。ed2k 直接提交，不触发清洗。

**规则：后缀在黑名单 AND 体积 < 阈值 → 删除（两者必须同时满足）**

| 配置项 | 说明 |
|---|---|
| `ENABLE_CLEANUP` | `true` 开启清洗 |
| `JUNK_EXTENSIONS` | 垃圾后缀黑名单，逗号分隔 |
| `JUNK_SIZE_THRESHOLD_MB` | 体积阈值（MB），留空则只按后缀清洗 |

**示例：**
- `JUNK_EXTENSIONS=txt`
- `JUNK_SIZE_THRESHOLD_MB=50`
- 则体积 < 50MB 的 txt 文件会被删除，>= 50MB 的 txt 保留。

---

## 💡 使用说明

### 场景 1：直接下载（默认路由）

```
magnet:?xt=urn:btih:YOUR_HASH_HERE
```

或 40 位 hash：

```
E808151805F0A2C8C281FBEFA682AD29EDA73FF2
```

或 ed2k：

```
ed2k://|file|example.mkv|12345678|ABCDEF1234567890ABCDEF1234567890|/
```

### 场景 2：离线到指定路由

```
/sub magnet:?xt=urn:btih:YOUR_HASH_HERE
```

### 场景 3：离线到自定义子目录

```
/sub @你好 magnet:?xt=urn:btih:YOUR_HASH_HERE
```

实际路径：`/网盘/手动转存/@你好/YYYY-MM-DD`

### 场景 4：批量提交（多链接统一路径）

```
/sub @你好
magnet:?xt=urn:btih:HASH1
magnet:?xt=urn:btih:HASH2
ed2k://|file|example.mkv|12345678|HASH|
```

### 场景 5：查询未完成任务

发送：

```
/tasks
```

或在企微应用底部菜单点击 **"任务状态"**。

---

## 🔄 中转清洗流程

配置了 `staging_folder` 时，磁力链接的处理流程：

1. 提交磁力 → 下载到 `staging_folder`
2. 企微回复：`📦 已提交到中转目录... ⏳ 下载完成后自动清洗...`
3. CD2 离线下载完成
4. 后台扫描真实文件：
   - 垃圾文件（后缀命中 + 体积 < 阈值）→ `DeleteFile` 删除
   - 保留的文件 → `MoveFile` 到目标目录
   - 子目录整体移动，保持原有结构
5. 企微回复：`✅ 中转任务完成 📦 保留文件 X 个`（如有清洗则追加 `🧹 已清洗垃圾文件 Y 个`）

---

## 📝 企微菜单

容器启动时会自动尝试初始化企微应用菜单（一个"任务状态"按钮）。如果自动创建失败，你也可以手动在企微后台配置：

1. 企微后台 → 应用管理 → 你的应用 → 自定义菜单
2. 添加按钮，类型选 **"点击事件"**
3. 事件 key 填 `status`
4. 保存

---

## ❓ 常见问题

**Q: 提交磁力后企微没反应？**
- 检查容器日志是否有 `收到企微消息` 输出
- 检查回调 URL 是否正确（结尾必须 `/wechat`）
- 检查企微应用是否开启了接收消息

**Q: 离线任务列表里没有任务？**
- 检查 `CD2_TOKEN` 是否正确
- 检查 `CD2_HOST` 是否能连通
- 查看容器日志是否有 `转存提交成功` 或 `转存提交失败`

**Q: 中转清洗没触发？**
- 确认 `download-routes.yml` 里配置了 `staging_folder`
- 确认提交的是磁力链接（ed2k 不走中转）

---

> ⚠️ **免责声明**：本项目仅供个人学习与研究使用。请严格遵守你所在国家/地区的法律法规。严禁用于传播任何受版权保护的内容。开发者对因违规使用本项目而导致的任何法律纠纷或经济损失概不负责。
