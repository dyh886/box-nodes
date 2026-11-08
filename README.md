# box-nodes

每两小时自动刷新一次的代理节点池，产物通过 **jsDelivr CDN** 分发，任何设备只要订阅一个固定 URL 即可随时拿到最新节点。

## 订阅地址（固定不变，内容自动更新）

| 格式 | URL |
|---|---|
| Clash / Clash Verge / Mihomo | `https://cdn.jsdelivr.net/gh/dyh886/box-nodes@main/dist/clash.yaml` |
| v2rayN / Shadowrocket / NekoBox（base64 订阅） | `https://cdn.jsdelivr.net/gh/dyh886/box-nodes@main/dist/sub.base64.txt` |
| 明文链接列表 | `https://cdn.jsdelivr.net/gh/dyh886/box-nodes@main/dist/links.txt` |
| sing-box | `https://cdn.jsdelivr.net/gh/dyh886/box-nodes@main/dist/singbox.json` |
| 状态元信息 | `https://cdn.jsdelivr.net/gh/dyh886/box-nodes@main/dist/meta.json` |

> 客户端只需配置一次；上游每两小时提交一次，jsDelivr 自动回源，客户端按自己的间隔拉取即可。

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

节点池每两小时刷新，由云电脑（box）上的调度器触发 `sync.py`。云机时区为 UTC，`0 */2 * * *` 即每两小时整点。

## 注意

- 产物是公开仓库里的明文节点配置。若上游订阅是私有的，建议把仓库改为 private 并改用带 token 的拉取方式，或把 `dist/` 加密后再发布。
- `state/seed.txt` 里默认是**占位节点**，仅用于验证管线；换成真实订阅后才会有可用节点。
- TCP 探活只证明端口开着，不代表协议可用。要更严格的判定，可把 `probe` 换成真实的协议握手（需额外依赖）。
