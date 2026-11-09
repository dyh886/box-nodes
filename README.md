# box-nodes 运维说明

代理订阅的自动更新系统。本文件记录架构、判据与踩过的坑。

## 订阅地址

```
https://dyh886.github.io/box-nodes/chain.yaml
```

FlClash / Clash Verge / mihomo 通用。iOS sing-box 用 `singbox.json`（同目录）。

## 架构：谁做什么

```
┌─ 云电脑 (24h 在线) ─────────────────────┐
│  每 4 小时: collect_only.py             │
│  拉三个机场 URL → 去重 → candidates.json │
│  ❌ 不发布订阅、不判断节点可用性          │
└─────────────────────────────────────────┘
                    │ 候选数据
                    ▼
┌─ 你的电脑 (每 4 小时) ──────────────────┐
│  1. fetch     拉三个机场 URL 全量        │
│  2. rank      去重排序（不淘汰任何协议）  │
│  3. l2_verify 经节点→落地实连 GitHub     │
│  4. stability 三轮复测                   │
│  5. build     只留 3/3 全通              │
│  6. publish   推 main + gh-pages         │
└─────────────────────────────────────────┘
                    │
                    ▼
              GitHub Pages 分发
```

**为什么验证必须在本机**：云电脑的出口代理只放行 443/22 端口，而落地是 9443、多数前置也是非 443 端口。云机无法建立「前置 → 落地」这一跳，也就无法判断节点是否真能用。本机有真实出口，可以完整走通链路。

## 链式代理结构

```
本机 → 前置节点 → 落地 107.172.186.223:9443 → 外网
```

| 组 | 类型 | 作用 |
|---|---|---|
| `前置` | select | 手动选前置 |
| `前置自动` | url-test | 自动测速选最快（推荐） |
| `前置总控` | select | 链式锚点，默认指向 `前置自动` |
| `代理` | select | 最终出口，选 `DediRock-LA` 走完整链路 |

落地 `DediRock-LA` 带 `dialer-proxy: 前置总控`，所以流量必然先过前置再到落地。**落地是唯一出口**。

## 验证判据

- **必需**：`https://github.com/` → 200
- **加分**（不淘汰）：`https://www.google.com/generate_204` → 204
- **稳定性**：三轮全通才入选

只要求 204 会被「国内能通的假节点」骗过；同时要求 Google 会误杀可用节点（本机网络对 Google 本身不稳）。

## 踩过的坑（重要）

### 1. 不要按协议类型淘汰节点

曾给 `http`/`socks5` 加 5 分并以 `qscore >= 8` 剔除，理由是「明文代理质量差」。实测抽样 40 个，**25 个能正常打开 GitHub（62.5%）**。这导致 293 个候选里 213 个被误杀，最终只剩 5 个节点。

现在所有协议都进实测池，由实连结果决定去留。当前 65 个稳定节点中 50 个是 http 类型。

### 2. 订阅里不要写 `dns:` 段

曾硬编码 `119.29.29.29`、`1.1.1.1`、`8.8.8.8` 的 DoH，实测在部分网络超时，导致代理侧域名解析卡约 5 秒、TLS 握手失败——表现为「能测速但打不开网页」。

原方案（`vps/chain/update_chain.py`）从不写 dns 段，客户端用自身设置。现在订阅不含 dns，发布前会硬性拦截。

### 3. 不要用直连落地来判断服务器是否可用

落地只接受经前置的连接。本机直连 `107.172.186.223` 的 22/80/443/9443 全部超时是**正常现象**，不代表服务器故障。判断服务器状态应该看手机端是否正常。

### 4. 批量验证必须分批 + 确认端口就绪

曾把 101 个节点塞进一个 sing-box 实例、开 101 个监听端口，只等 6 秒就开测，大量端口未就绪 → 全部超时 → 结果 0/101。而同样的方法测 5 个节点却是 5/5 全通。

现在每批 12 个，逐个 `wait_port` 确认就绪后再测。

### 5. 节点名每轮都会变

机场每次重新分配后缀（`-hhxm` → `-ppuf`）。所以「前置总控」默认指向「前置自动」，不依赖任何固定节点名。

### 6. 云机不能发布订阅

云机曾每小时发布一次未经链路验证的节点集，会把本机验证好的结果覆盖掉。现在云机只写 `candidates.json`，发布权只在本机。

## 常用命令

```powershell
cd D:\AI_Workstation\Work\日常工作\box-nodes

# 完整流程（拉取→验证→复测→发布）
python sync_local.py

# 只验证不发布
python sync_local.py --dry-run

# 只用现有 state 重建并发布（不重新拉取）
python sync_local.py --no-l2

# 直接发布现有 docs/chain.yaml
python sync_local.py --publish-only

# 单独跑各阶段
python build_live.py fetch
python build_live.py rank --top 400
python l2_verify.py --batch 10
python stability_check.py
python select_and_build.py
```

云机侧：

```bash
systemctl list-timers box-nodes-sync.timer    # 下次聚合时间
sudo systemctl start box-nodes-sync.service   # 立即聚合一次
tail -f ~/box-nodes/collect.log               # 聚合日志
```

## 定时任务

| 位置 | 任务名 | 周期 | 时点 |
|---|---|---|---|
| 你的电脑 | `box-nodes-sync-local` | 4 小时 | 00:00 起 |
| 云电脑 | `box-nodes-sync.timer` | 4 小时 | UTC 02:05 起（北京 10:05） |

本机日志：`D:\AI_Workstation\Work\日常工作\box-nodes\sync-local.log`

## 发布保护

`sync_local.py` 发布前会检查：

- 含 `dns` 段 → 拒绝发布
- 前端节点 < 3 个 → 拒绝发布（说明验证失败，保留上一版）
- 前端节点 > 200 个 → 拒绝发布（疑似未经筛选）

所以某一轮网络差导致通过数过少时，不会把坏结果推上去，线上订阅保持上一版不变。

## 当前状态

- 前端节点：65 个（http 50、vless 6、ss 5、vmess 2、trojan 1、socks5 1）
- 落地：`107.172.186.223:9443`（shadowsocks, chacha20-ietf-poly1305）
- 订阅不含 dns 段
- 测速目标：`https://www.google.com/generate_204`
