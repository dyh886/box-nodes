# box-nodes 运维说明（双轨架构）

代理订阅系统的架构、凭据管理与踩坑记录。

## 一、核心设计：公开与私有分离

```
公开（GitHub Pages，任何人都能访问，但不含任何凭据）
  https://dyh886.github.io/box-nodes/fronts.yaml   前置节点池
  https://dyh886.github.io/box-nodes/meta.json     状态信息

私有（只在本机，绝不入库）
  private/private-chain.yaml   完整配置，落地凭据在这里
  config.json                  落地凭据来源（已 gitignore）
```

**为什么这样能根治泄露**：公开文件里根本不存在落地密码。即使有人盯着仓库看，也拿不到能用的凭据。

## 二、链式代理结构

```
本机 → 前置节点（公开池） → 落地 107.172.186.223:19443 → 外网
```

| 组 | 类型 | 作用 |
|---|---|---|
| `前置` | select | 手动选前置 |
| `前置自动` | url-test | 自动测速（推荐） |
| `前置总控` | select | 链式锚点，默认指向 `前置自动` |
| `代理` | select | 最终出口，选 `DediRock-LA` 走完整链路 |

落地通过 `dialer-proxy: 前置总控` 挂在链式锚点上，所以流量必然先过前置再到落地。落地是唯一出口。

## 三、客户端接入方式

核心机制是 `proxy-providers`：

```yaml
proxy-providers:
  fronts:
    type: http
    url: "https://dyh886.github.io/box-nodes/fronts.yaml"
    interval: 3600
    path: ./providers/fronts.yaml
    proxy: DIRECT          # ← 关键，缺了会死锁
    health-check:
      enable: true
      url: https://www.google.com/generate_204
      interval: 180
```

**`proxy: DIRECT` 是必须的**。否则 provider 拉取请求走代理 → 代理需要前置 → 前置还没加载 = 循环依赖，报 `provider pull error: EOF`。

客户端只导入一次 `private/private-chain.yaml`，之后前置池由 provider 自动刷新，落地凭据不变。

## 四、双轨更新：本机优先，云机兜底

| 位置 | 频率 | 角色 | 做什么 |
|---|---|---|---|
| 你的电脑 | 4 小时 | **主** | 拉取 → 端到端验证 → 三轮稳定性 → 发布 front 池 + 写心跳 |
| 云电脑 | 4 小时 | **兜底** | 读心跳，本机 8 小时内活跃就跳过；否则聚合发布未验证节点池 |

协调靠 `state/heartbeat.json`：

```json
{ "updatedAt": 1762... , "source": "local (verified)", "staleAfterHours": 8 }
```

云机逻辑：`age < 8h` → 跳过，让本机的验证结果作数。

**这就是电脑关机的答案**：
- 电脑关机 → 8 小时后云机接管，手机仍能拉到新节点
- 电脑开机 → 本机验证结果重新生效，质量更高

## 五、验证判据

- **必需**：`https://github.com/` → 200
- **加分**（不淘汰）：`https://www.google.com/generate_204` → 204
- **稳定性**：三轮全通才入选

只要求 204 会被「国内能通的假节点」骗过；同时要求 Google 会误杀可用节点（本机网络对 Google 本身不稳）。

## 六、踩过的坑（重要）

### 1. 落地凭据曾被公开泄露

`chain.yaml` / `singbox.json` / `config.json` 曾含落地密码并推到公开仓库，3 天里被两个陌生 IP 盗用（台湾 `61.223.79.75`、AWS `54.169.49.130`），盗用者甚至跟着我们的更新节奏访问 gstatic/github。

**处置**：换端口（9443 → 19443）+ 换 32 位密码 + 公开文件彻底移除凭据。

**教训**：订阅文件本身要公开，就不能放任何私密凭据。必须走 provider 分离。

### 2. 不要用「CONNECT 200」判断端口开放

本机代理对**任何**端口都返回 `200 Connection established`（测过 59999、12345 也是 200）。所以「端口可达」的结论不可信。

判断服务是否真的活着，要实际读数据（SSH banner、HTTP 响应体），或从别的网络路径验证。

### 3. 不要用直连落地判断服务器故障

落地只接受经前置的连接。本机直连 `107.172.186.223` 的 22/80/443/19443 全部超时是**正常现象**。

判断服务器状态应看手机端是否正常，或经前置链路测试。

### 4. `systemctl restart xray` 会短暂切断 SSH

改完 xray 配置重启时，SSH 连接会断。这是因为它走的是同一条链路。

**做法**：改配置前先备份，用 `xray -test` 校验，重启后从 VNC 或等代理恢复再连。

**救回方式**（VNC 控制台）：
```bash
cd /usr/local/etc/xray/
ls -la | grep bak                    # 找到备份
cp -v config.json.bak_xxx config.json
systemctl restart xray
systemctl restart ssh
```

### 5. 不要按协议类型淘汰节点

曾给 `http`/`socks5` 加 5 分并剔除，理由是「明文代理质量差」。实测抽样 40 个，**25 个能正常打开 GitHub（62.5%）**。这导致 293 个候选里 213 个被误杀，最终只剩 5 个节点。

现在所有协议都进实测池，由实连结果决定去留。

### 6. 批量验证必须分批 + 确认端口就绪

曾把 101 个节点塞进一个 sing-box 实例、开 101 个监听端口，只等 6 秒就开测，大量端口未就绪 → 全部超时 → 结果 0/101。而同样的方法测 5 个节点却是 5/5 全通。

现在每批 12 个，逐个 `wait_port` 确认就绪后再测。

### 7. 节点名每轮都会变

机场每次重新分配后缀（`-hhxm` → `-ppuf`）。所以「前置总控」默认指向「前置自动」，不依赖任何固定节点名。

### 8. 云机没有直连出口

云机访问 GitHub API 必须走 `http://127.0.0.1:3128` 代理，**不能加 `--noproxy '*'`**，否则 `Network is unreachable`（只会得到 status=000）。

## 七、常用命令

```powershell
cd D:\AI_Workstation\Work\日常工作\box-nodes

# 完整流程（拉取→验证→复测→生成→发布）
python sync_split.py

# 只重建并发布（不重新验证）
python sync_split.py --skip-verify

# 只生成产物不发布
python sync_split.py --dry-run

# 只生成分离产物
python build_split.py
```

云机侧：
```bash
systemctl list-timers box-nodes-sync.timer --no-pager
sudo systemctl start box-nodes-sync.service     # 立即跑一次
tail -f ~/box-nodes/box-publish.log
```

## 八、定时任务

| 位置 | 任务 | 周期 | 时点 |
|---|---|---|---|
| 你的电脑 | `box-nodes-sync-local` | 4 小时 | 00:00 起 |
| 云电脑 | `box-nodes-sync.timer` | 4 小时 | UTC 02:05 起（北京 10:05） |

本机日志：`sync-split.log`

## 九、发布保护

`sync_split.py` 发布前检查：

- `fronts.yaml` 含落地密码 / 落地 IP / `dialer-proxy` → **拒绝发布**
- 端到端验证通过数为 0 → 中止，保留上一版

云机侧同样检查，并且默认跳过（心跳新鲜时）。

## 十、当前状态

- 落地：`107.172.186.223:19443`（shadowsocks, chacha20-ietf-poly1305, 32 位随机密码）
- 旧端口 9443 已永久移除，旧密码已失效
- 公开 `fronts.yaml`：不含任何凭据
- 测速目标：`https://www.google.com/generate_204`
- 后端服务：xray（入站 19443 ss / 443 vless-tls / 2083 vless-reality / 10001 vless-ws）
