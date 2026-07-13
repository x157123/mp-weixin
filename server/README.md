# 喝奶数据同步服务

给「宝宝喂养记录」小程序用的云端同步服务。纯 Python 标准库实现（Python 3.8+），
无任何第三方依赖，`python3 app.py` 即可运行。

## 关于备案（新加坡腾讯云）

- **服务器本身不需要备案**。ICP 备案只针对托管在中国大陆境内的服务器/接入商，
  腾讯云新加坡地域属于境外节点，工信部层面无备案要求。
- **但要注意微信平台的规则**：小程序后台配置「request 合法域名」时，微信要求
  （中国大陆主体的小程序）域名必须已 ICP 备案，且必须是 HTTPS。而 ICP 备案又要求
  接入商在大陆——所以"新加坡服务器 + 备案域名"这条路走不通备案流程。
- **实际可行的用法**：
  - **开发/自用（推荐先这样跑起来）**：微信开发者工具 → 详情 → 本地设置 →
    勾选「不校验合法域名」，可直接用 `http://<新加坡服务器IP>:8300`，无需域名、
    无需 HTTPS、无需备案。真机上用「体验版」打开调试模式同样可以访问。
  - **正式发布（对所有用户开放）**：中国大陆主体小程序绕不开备案域名的要求，
    届时需要一个已备案域名（通常意味着套一层大陆入口，或将服务迁回大陆地域）；
    如果小程序主体是海外公司，则无备案要求，配一个 HTTPS 域名即可。

## 快速开始（本地联调）

```bash
# 开发模式：无需微信 AppSecret，客户端可用 devId 登录
DEV_MODE=1 python3 app.py
```

## 部署到腾讯云新加坡服务器

1. 上传代码：

   ```bash
   scp -r server/ root@<服务器IP>:/opt/feeding-sync/
   ```

2. 在小程序后台（mp.weixin.qq.com → 开发管理 → 开发设置）拿到 **AppSecret**，
   然后启动服务：

   ```bash
   cd /opt/feeding-sync
   WX_APPID=wx5e292c7a374e892d WX_SECRET=<你的AppSecret> python3 app.py
   ```

3. 腾讯云控制台 → 安全组：放行 TCP 8300 端口（来源 0.0.0.0/0）。

4. 建议用 systemd 常驻（`/etc/systemd/system/feeding-sync.service`）：

   ```ini
   [Unit]
   Description=Feeding sync server
   After=network.target

   [Service]
   WorkingDirectory=/opt/feeding-sync
   Environment=WX_APPID=wx5e292c7a374e892d
   Environment=WX_SECRET=<你的AppSecret>
   ExecStart=/usr/bin/python3 /opt/feeding-sync/app.py
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```

   ```bash
   systemctl daemon-reload && systemctl enable --now feeding-sync
   curl http://127.0.0.1:8300/api/health   # 验证
   ```

5. 配置 HTTPS 域名（本项目已配置 `https://vmxjp.15712345.xyz`，
   小程序端 `utils/sync.uts` 的 `SYNC_BASE_URL` 已指向它）。
   先把域名 A 记录解析到服务器公网 IP，然后在服务器上装 Nginx + Let's Encrypt：

   ```bash
   apt install -y nginx certbot python3-certbot-nginx
   certbot --nginx -d vmxjp.15712345.xyz   # 自动申请证书并写入 nginx 配置
   ```

   Nginx 站点配置（certbot 会自动补上 ssl 部分）：

   ```nginx
   server {
       listen 443 ssl;
       server_name vmxjp.15712345.xyz;
       ssl_certificate     /etc/letsencrypt/live/vmxjp.15712345.xyz/fullchain.pem;
       ssl_certificate_key /etc/letsencrypt/live/vmxjp.15712345.xyz/privkey.pem;
       location / {
           proxy_pass http://127.0.0.1:8300;
           proxy_set_header X-Real-IP $remote_addr;
       }
   }
   ```

   安全组放行 TCP 80/443（80 用于 certbot 验证与续期）。配好后 8300 端口
   可以只对本机开放，不必暴露公网。验证：

   ```bash
   curl https://vmxjp.15712345.xyz/api/health
   ```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8300` | 监听端口 |
| `DATA_DIR` | `./data` | 数据目录 |
| `WX_APPID` | `wx5e292c7a374e892d` | 小程序 AppID |
| `WX_SECRET` | 空 | 小程序 AppSecret，正式使用必填 |
| `TOKEN_SECRET` | 自动生成 | token 签名密钥，自动生成后存 `data/.token_secret` |
| `DEV_MODE` | 关 | 置 `1` 允许 `devId` 免微信登录（仅联调，线上勿开） |

## 存储结构

每个用户（家庭）一个 JSON 文件，按 openid 中**前 4 个数字**分桶：

```
data/
├── _meta/
│   ├── shares.json      # {共享码: 数据主人openid}
│   └── bindings.json    # {成员openid: 数据主人openid}
└── 3455/
    └── ff34445k54xxxx.json   # openid ff34445k54xxxx 的数据（数字 3,4,5,5 → 桶 3455）
```

文件内容：`{"openid", "updatedAt", "updatedBy", "payload"}`，其中 payload 即
小程序导出格式（`records` + `settings` + `deletedIds` 删除墓碑）。

## 家庭共享与合并规则

- A 调 `/api/share/code` 生成共享码 → 发给 B → B 调 `/api/share/bind` 绑定。
  绑定后 B 的上传/下载都指向 A 的数据文件。
- 上传不是整体覆盖，而是**按记录 id 合并**：双方新增的记录都会保留，
  同 id 以本次上传方为准（覆盖编辑），`deletedIds` 墓碑保证删过的记录
  不会被对方的旧数据复活。`settings`（宝宝、补充剂等）以最后上传方为准。
- 上传接口会把合并后的完整数据返回，客户端随即写回本机，因此
  「同步到云端」= 双向同步一次。

## 接口

| 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/health` | - | 健康检查 |
| POST | `/api/login` | `{code}` 或 DEV_MODE 下 `{devId}` | 换取 `{openid, token}` |
| POST | `/api/sync/upload` | `{openid, token, payload}` | 合并上传，返回合并后 payload |
| GET | `/api/sync/download` | `?openid=&token=` | 下载 `{found, updatedAt, payload}` |
| POST | `/api/share/code` | `{openid, token}` | 获取/生成我的共享码 |
| POST | `/api/share/bind` | `{openid, token, shareCode}` | 绑定他人共享码 |
| POST | `/api/share/unbind` | `{openid, token}` | 解绑 |
| GET | `/api/share/info` | `?openid=&token=` | 查询绑定状态 |
