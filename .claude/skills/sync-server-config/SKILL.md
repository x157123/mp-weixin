---
name: sync-server-config
description: 云同步服务器地址与微信小程序合法域名配置说明。当遇到 "url not in domain list" 报错、需要更换同步服务器域名、或排查云同步连不上服务器的问题时使用。
---

# 云同步服务器与微信合法域名配置

## 服务器地址

- 同步服务器 Base URL 定义在 `utils/sync.uts` 顶部的 `SYNC_BASE_URL` 常量:
  ```
  export const SYNC_BASE_URL = 'https://vmxjp.15712345.xyz'
  ```
- 服务器为新加坡腾讯云,后端是 `server/app.py`(纯 Python 标准库实现)。
- **更换域名时**,需要同时改两处:
  1. 代码:`utils/sync.uts` 里的 `SYNC_BASE_URL`
  2. 微信公众平台后台的服务器域名白名单(见下文)

## 微信合法域名白名单

微信小程序真机/正式环境只允许请求白名单内的域名,否则报错:

```
request:fail url not in domain list:<域名>
```

配置入口:[mp.weixin.qq.com](https://mp.weixin.qq.com) → 开发 → 开发管理 → 开发设置 → 服务器域名

当前已配置(2026-07):

| 服务器配置 | 域名 |
|---|---|
| request 合法域名 | https://vmxjp.15712345.xyz |
| uploadFile 合法域名 | https://vmxjp.15712345.xyz |
| downloadFile 合法域名 | https://vmxjp.15712345.xyz |

填写要求:必须 `https`、不带端口号、不带路径。

## 改完域名后不生效?(客户端缓存)

后台配置即时生效,但客户端会缓存白名单(最长几分钟到一小时),需手动刷新:

**微信开发者工具:**
1. 「详情 → 域名信息」查看是否已显示新域名,没有就点右上角刷新重新拉取
2. 还不行:「工具 → 清除缓存 → 全部清除」,或重启开发者工具

**真机:**
- 把小程序从「最近使用」列表删除(下拉列表长按删除),重新进入即可重新拉取配置

## 开发阶段临时绕过校验

- 开发者工具:「详情 → 本地设置 → 勾选"不校验合法域名、web-view(业务域名)、TLS 版本以及 HTTPS 证书"」
- 真机预览:打开「开发调试」(vConsole)模式也会跳过域名校验
- 仅限开发调试,正式发布必须走白名单配置

## 注意事项

- 微信要求 request 合法域名完成 ICP 备案;当前域名已成功添加。若日后更换域名被备案检查拦截,需换已备案域名或将服务器迁回境内备案。
- 自动同步失败的日志在 `utils/sync.uts` 的 `scheduleAutoSync()` 中打印(`[autoSync] fail ...`),失败不弹窗打扰用户,下次保存或手动同步时会重试。
