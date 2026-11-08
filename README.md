# box-nodes

每两小时自动刷新一次的代理节点池，产物通过 **jsDelivr CDN** 分发，任何设备只要订阅一个固定 URL 即可随时拿到最新节点。

## 订阅地址（固定不变，内容自动更新）

| 格式 | URL |
|---|---|
| Clash / Clash Verge / Mihomo | `https://cdn.jsdelivr.net/gh/dyh886/box-nodes@latest/dist/clash.yaml` |
| v2rayN / Shadowrocket / NekoBox（base64 订阅） | `https://cdn.jsdelivr.net/gh/dyh886/box-nodes@latest/dist/sub.base64.txt` |
| 明文链接列表 | `https://cdn.jsdelivr.net/gh/dyh886/box-nodes@latest/dist/links.txt` |
| sing-box | `https://cdn.jsdelivr.net/gh/dyh886/box-nodes@latest/dist/singbox.json` |
| 状态元信息 | `https://cdn.jsdelivr.net/gh/dyh886/box-nodes@latest/dist/meta.json` |

> **必须用 `@latest`，不要用 `@main`。** jsDelivr 对 `@main` 分支引用有长 TTL 缓存（实测数十分钟不更新，`?v=` 之类的 query 也无效）；`@latest` 会解析到最新 release/commit，实测提交后立即生效。详见下方「CDN 缓存」。
>
> 客户端只需配置一次；上游每两小时刷新，客户端按自己的间隔拉取即可。

**备用入口（CDN 出问题时）**：`https://raw.githubusercontent.com/dyh886/box-nodes/main/dist/...` — 无缓存，永远最新，但国内可能不稳。

## 架构

```
上游订阅源 ──┐
本地种子文件 ─┼─> pool.py collect ──> state/nodes.json
             │        (解析+去重)
             ├─> pool.py probe  ──> 并发 TCP 探活 + 延迟排序 + 截断
             └─> pool.py export ──> dist/{clash.yaml, links.txt, sub.base64.txt, singbox.json, meta.json}
                                          │
                                    sync.py git push
                                          │
                                   jsDelivr 回源分发
```

三个职责分离，任一环节都能单独跑、单独调试。

## 组件

### `pool.py` — 采集 / 探活 / 导出

纯 Python 标准库，无第三方依赖（云机没有 pip3）。

**支持的节点格式**

| 协议 | 链接 | Clash YAML |
|---|---|---|
| vmess | ✅ base64 JSON | ✅ |
| vless | ✅ 含 Reality (`pbk`/`sid`/`fp`) | ✅ |
| trojan | ✅ | ✅ |
| shadowsocks | ✅ 两种 userinfo 写法 | ✅ |
| shadowsocksR | ✅ | — |
| hysteria2 | ✅ | ✅ |
| tuic | ✅ | ✅ |

**来源类型自动嗅探**：base64 整段订阅、明文链接列表、Clash YAML（内置极简 YAML 子集解析器，不引 PyYAML）。

**去重指纹** = `sha1(protocol, server, port, uuid|password, network, path, sni, security, publicKey)` 取前 12 位。同一条线路从多个源进来只留一份，且排序稳定，避免每次 commit 产生无意义 diff。

**探活**：`ThreadPoolExecutor` 并发 TCP connect，默认 64 并发 / 5s 超时。只测 TCP 可达性与延迟，不做协议握手——快，且不会向上游暴露探测行为。

```bash
python pool.py collect                    # 拉取+去重 -> state/nodes.json
python pool.py probe --workers 64 --keep 60
python pool.py export                     # -> dist/
```

### `sync.py` — 编排 + 发布

```bash
python sync.py              # 全流程 + commit/push + jsDelivr purge/verify
python sync.py --dry-run    # 只跑管线，不发布
python sync.py --no-probe   # 跳过探活
```

推送后主动调用 `purge.jsdelivr.net` 让 CDN 立即回源，再 GET 一次产物确认可读。

### `config.json` — 源配置

```json
{
  "sources": [
    { "name": "my-sub", "url": "https://.../sub?token=xxx", "kind": "auto", "enabled": true },
    { "name": "local",  "file": "state/seed.txt",           "kind": "auto", "enabled": true }
  ]
}
```

`kind` 可选 `auto` / `clash` / `base64`。`enabled: false` 可临时停用某个源。

## 定时

节点池每两小时刷新，由云电脑（box）上的 **systemd timer** 触发：

```
box-nodes-sync.timer   →  OnCalendar=*-*-* 00/2:05:00  (UTC), Persistent=true
box-nodes-sync.service →  User=user, WorkingDirectory=~/box-nodes
```

`Persistent=true` 保证机器休眠/重启后补跑错过的那次；`RandomizedDelaySec=120` 避免整点堆积。

```bash
systemctl list-timers box-nodes-sync.timer      # 看下次运行
sudo systemctl start box-nodes-sync.service     # 立即手动跑一次
tail -f ~/box-nodes/sync.log                    # 看日志
```

> 云机时区是 UTC，`00/2:05` 即北京时间 08:05 / 10:05 / 12:05 …（每两小时）。

## 环境适配要点（沙箱云机特有）

这套东西能跑起来，踩了三个坑，全部已在代码里处理：

1. **没有直连出口**。云机所有流量强制经 `127.0.0.1:3128` 出口代理，本地 DNS 不可用（`nameserver 8.8.8.8` 但解析必失败）。所以直接 `socket.connect` 探活会 100% 失败 —— `probe` 改为走代理的 **CONNECT 隧道**打到节点真实 `host:port`，由代理侧完成 DNS + TCP，既不发送协议数据也能验证「域名可解析 + 端口可达」。

2. **systemd 不继承 shell 环境**。代理变量只在交互式 shell 里存在，`Environment=` 必须显式写进 unit 文件，否则定时任务里探活全灭、`export` 直接以 `no nodes` 退出。

3. **jsDelivr 的 `@main` 缓存不可控**。实测 `@main` 数十分钟不回源，`purge` 接口返回 200 但无效，加 query 参数也无效。`@latest` 立即生效，所以全部入口改用 `@latest`。

## 注意

- 产物是公开仓库里的明文节点配置。若上游订阅是私有的，建议把仓库改为 private 并改用带 token 的拉取方式，或把 `dist/` 加密后再发布。
- `state/seed.txt` 里默认是**占位节点**，仅用于验证管线；换成真实订阅后才会有可用节点。
- TCP 探活只证明端口开着，不代表协议可用。要更严格的判定，可把 `probe` 换成真实的协议握手（需额外依赖）。
