# box-nodes

链式代理订阅 + 通用节点池。云电脑每 2 小时刷新并发布，任何设备订阅一个固定 URL 即可随时同步。

## 主产物：链式代理订阅

**订阅地址（固定，内容自动更新）**

```
https://dyh886.github.io/box-nodes/chain.yaml
```

备用（jsDelivr 镜像，`@main` 有缓存，需 purge）：

```
https://cdn.jsdelivr.net/gh/dyh886/box-nodes@main/dist/chain.yaml
```

### 链路拓扑

```
本机 / 手机
    ↓
前置节点（157 个可选，手动或自动测速）
    ↓
落地 DediRock-LA  107.172.186.223:443  ← 唯一出口
    ↓
外网
```

落地节点带 `dialer-proxy: 前置总控`，所以流量必然先过前置、再由前置连到落地。
**落地是唯一出口**，前置只负责中转，不暴露真实来源。

### 四个界面（proxy-groups）

| 组名 | 类型 | 作用 |
|---|---|---|
| `前置` | select | 手动挑一个前置节点（157 个） |
| `前置自动` | url-test | 自动测速选最快前置（60 个候选，180s 间隔） |
| `前置总控` | select | **链式锚点**，落地节点的 `dialer-proxy` 指向它 |
| `代理` | select | 最终出口，选 `DediRock-LA` 走完整链路 |

规则：`MATCH,代理`，全部流量走代理组。

### 落地节点参数

```
name:    DediRock-LA
type:    vless
server:  107.172.186.223
port:    443
flow:    xtls-rprx-vision
tls:     true
sni:     www.apple.com
reality: public-key / short-id 见配置
dialer-proxy: 前置总控
```

## 副产物：通用节点池（可选）

`state/seed.txt` 或 `config.json` 的 `sources` 里配了真实订阅时才产出：

| 格式 | URL |
|---|---|
| Clash | `https://dyh886.github.io/box-nodes/clash.yaml` |
| base64 订阅 | `https://dyh886.github.io/box-nodes/sub.base64.txt` |
| sing-box | `https://dyh886.github.io/box-nodes/singbox.json` |

没配源时自动跳过，不影响主产物。

## 组件

| 文件 | 作用 |
|---|---|
| `extract_source.py` | 从本地机场 profile 用 PyYAML 提取前置/静态住宅/落地节点 |
| `build_chain.py` | 生成四层链式代理配置，内置 8 项结构自检 |
| `pool.py` | 通用节点池：collect / probe / export |
| `sync.py` | 编排全流程 + 双通道发布（main / gh-pages） |

### 提取器的两个坑（已修）

手写 YAML 解析曾踩过：

1. `alpn:\n  - http/1.1` 的列表项被误判成新节点 → 产生空名假节点
2. 原始块缩进不统一（有的顶格、有的两格）→ 拼接后 YAML 非法

现在用 `yaml.safe_load` 解析、`yaml.dump` 序列化，不再复用原始文本。

### 发布双通道

| 通道 | 延迟 | 用途 |
|---|---|---|
| GitHub Pages (`dyh886.github.io`) | 提交后 ~60s | **主入口** |
| jsDelivr `@main` | 有缓存，purge 可刷新 | 国内加速 |
| `raw.githubusercontent.com` | 立即 | 兜底/调试 |

## 定时

云电脑上 systemd timer 每 2 小时跑一次：

```
box-nodes-sync.timer   →  OnCalendar=*-*-* 00/2:05:00 (UTC), Persistent=true
box-nodes-sync.service →  User=user, WorkingDirectory=~/box-nodes
```

```bash
systemctl list-timers box-nodes-sync.timer      # 下次运行
sudo systemctl start box-nodes-sync.service     # 立即跑一次
tail -f ~/box-nodes/sync.log                    # 日志
```

## 环境适配（沙箱云机）

1. **没有直连出口**：所有流量强制走 `127.0.0.1:3128`，本地 DNS 不可用。`pool.py probe` 因此改为走代理 CONNECT 隧道，打节点真实 `host:port`。
2. **systemd 不继承 shell 环境**：代理变量必须用 `Environment=` 显式写进 unit，否则定时任务里探活全灭。
3. **编码**：Windows 下 `subprocess` 默认 GBK，遇到中文/emoji stderr 会崩。`sync.py` 统一用 `encoding="utf-8", errors="replace"`。

## 注意

- 仓库是公开的，产物含明文节点凭据。若要私有化，改仓库可见性并换带 token 的拉取方式。
- 前置节点由机场维护，会随订阅变化。要更新前置池，重跑 `extract_source.py` 即可。
