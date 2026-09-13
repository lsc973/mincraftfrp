# lanlink

**局域网优先的通用联机框架。** 把"让几台机器互相发数据"这件事从你的项目里拿掉。

纯 Python 标准库实现，零第三方依赖，跨平台。

```python
from lanlink import Host, Client

# 主机
host = Host("我的房间").start()
print(host.room_id, host.port)

# 客户端（在另一台机器上）
client = Client.connect("192.168.1.10", 50001, name="小明")
client.broadcast(b"大家好")
```

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| 房间发现 | UDP 广播 + 主动探测，同一局域网内自动找到房间，不用手抄 IP |
| 成员管理 | 进房/离房事件、成员列表、人数上限、房间密码、踢人 |
| 消息路由 | 单播（给别人）、广播（给所有人）、直发（给主机），二进制和 JSON 都支持 |
| 跨网段 | 挂到公网中继服务器，跨网络也能联机，上层代码完全不用改 |
| 断线检测 | 心跳 + 空闲超时。拔网线这种 TCP 不会立刻报错的情况也能及时清掉 |
| 中继自建 | 一个命令跑起自己的中继，可选接入口令 |
| 环境自检 | `lanlink doctor` 逐条检查防火墙、网段、端口，把"连不上"变成可执行的修复步骤 |
| 图形界面 | tkinter 写的窗口程序，可打包成单文件 exe，对方不用装 Python。房间、中继、隧道都在界面里 |
| 端口转发隧道 | 把房间当虚拟网线，让异地的人连上你本机的任意 TCP 服务（Minecraft、远程桌面…） |
| 公网可达性判断 | 问自家路由器要 WAN 口 IP，判断有没有公网 IP / 是否在大内网（UPnP，不依赖外部服务） |

**不适合**什么：需要低延迟音视频流的场景（那是 WebRTC 的活）；这个框架擅长的是
游戏状态同步、房间聊天、指令下发这类消息型通信。

---

## 快速开始

零第三方依赖，Python 3.8 ~ 3.14 都已实测通过（见下方[兼容性](#兼容性)）。

### 方式一：直接跑 exe（推荐给普通用户）

到 [Releases](https://github.com/lsc973/mincraftfrp/releases) 下载打包好的单文件程序，
**对方不需要装 Python**，双击就能用（仓库里不再放二进制，那些是每次构建自动出的）：

```
创建房间（当房主）   ← 建好把房间号发给朋友
加入房间             ← 自动搜局域网，或手填地址 / 走中继
我当中继服务器       ← 放公网机器上给跨网段的人中转
端口转发隧道         ← 让异地的人连上你本机的服务（Minecraft 等）
环境自检             ← 连不上先点这个
```

自己打包：

```bash
pip install pyinstaller
python packaging/build.py            # 图形版
python packaging/build.py --mode both  # 图形版 + 命令行版
```

产物在 `dist/` 下，单文件约 9 MB（里面含 Python 运行时和 tkinter）。
想验证打出来的东西真能跑，别只看打包有没有报错：

```bash
python tools/exe_smoke_test.py
```

#### 让对面什么都不用填

给别人的那份可以把服务器地址**编进 exe**。编了之后对方打开只看到两个空：
房间号和口令，不需要知道也不需要填任何地址。

服务器有两种，**选错了连不上**：

| | 什么时候用 | 怎么编 |
| --- | --- | --- |
| **直连** | 你家能被外面连到（有公网 IP，或者有 IPv6 且防火墙放行） | `--server 你的地址:端口` |
| **中继** | 家里连不进来（宽带在运营商大内网里、或者光猫的 IPv6 防火墙关不掉） | `--server-relay 公网机器:9000` |

```bash
# 家里能被连到：把域名编进去
python packaging/build.py --mode gui --server yourname.dynv6.net:50001 --label "给小明的"

# 家里连不进来：得先有台公网机器跑中继（见[跨网段](#跨网段起一台中继)）
python packaging/build.py --mode gui --server-relay 1.2.3.4:9000 --relay-token 口令 --label "给小明的"

python tools/make_release.py --label "给小明的"
```

最后一条产出 `release/lanlink-给小明的.zip` —— 里面是 exe 加一份按情况
生成的 `使用说明.txt`。直接发过去就行。

**中继模式的好处**：你自己完全不需要能被外部访问，两边都是主动连出去的，
什么防火墙都拦不住。这也是 CGNAT 宽带唯一走得通的路。

地址变了不用重新打包：

```bash
lanlink server set 你的域名:50001                    # 改成直连
lanlink server set 1.2.3.4:9000 --relay --token 口令  # 改成中继
```

#### 自动打包

推个标签就出包，产物挂在 Release 里：

```bash
git tag v1.0 && git push --tags
```

或者在 GitHub 的 **Actions → 打包 exe → Run workflow** 手动跑，填上服务器
地址和"给谁的"，跑完在页面底部下载 zip。

工作流会依次做：跑测试 → 打包 → 把 exe 当黑盒验证 → 收拾成 zip → 上传。
**先跑测试再打包**是有意的：打一个跑不起来的包比不打包更糟，对面拿到手才发现，
你还得重新走一遍发文件的流程。

### 方式二：图形界面（从源码）

```bash
python -m lanlink gui
```

### 方式三：装成命令 / 当库用

```bash
pip install .
lanlink --help
```

也可以直接把 `lanlink/` 目录拷进项目里当库用，不需要安装。

### 先自检一下环境

**跨机器连不上，十次里有九次不是代码问题，而是防火墙**。所以在折腾之前先跑：

```bash
python -m lanlink doctor        # 或者装好之后直接 lanlink doctor
```

它会逐条查本机 IP、网络类别、防火墙放行情况、发现端口占用、TCP 监听能力，
并真的起一间房做一次收发自测，最后给出能照着做的修复命令：

```
[通过] 本机 IP：192.168.1.6
[通过] 防火墙：Python 已放行：Public，当前网络类别 Public
[通过] 发现端口：UDP 47777 可用
[通过] TCP 监听：可以绑定（试了 0.0.0.0:51056）
[通过] 房间发现：广播和探测都通（本机自测）
[通过] 收发回环：连接、发送、接收都正常
```

**两台机器上都要跑一次。** 连不上时，问题几乎总是在监听的那一端。

### 命令行试玩

开两个终端。先开房：

```bash
python -m lanlink host --room 我的房间
```

会打印出房间号和地址：

```
房间名：我的房间
房间号：3f2a1b9c
局域网地址：192.168.1.10:50001
```

另一个终端（同局域网的另一台机器，或本机）：

```bash
python -m lanlink list          # 先扫一下有哪些房间
python -m lanlink join --room 3f2a1b9c
```

连上后直接打字回车就是广播，`/w 2 内容` 私聊 #2，`/who` 看有谁。

### 跨网段：起一台中继

找一台有公网 IP 的机器：

```bash
python -m lanlink relay --port 9000 --token 你的口令
```

主机挂上去：

```bash
python -m lanlink host --room 我的房间 --relay 1.2.3.4:9000 --relay-token 你的口令
```

外面的人连进来：

```bash
python -m lanlink join --room 3f2a1b9c --relay 1.2.3.4:9000 --relay-token 你的口令
```

---

## 端口转发隧道：让朋友连上你本机的服务

**这是另一个用法**：你本机跑着一个现成的服务（Minecraft 服务器、远程桌面、
网页、数据库……），想让不在同一局域网的人连进来。

问题是：你家大概率没有公网 IP（很多宽带是 CGNAT），对方**直接连不上**。
而中继服务器虽然能当中转站，但它只搬 lanlink 自己的协议帧，不认识
Minecraft 的协议 —— 把 Minecraft 客户端指向中继是连不上的。

`lanlink tunnel` 在房间之上铺了一层流复用，把房间变成一条虚拟网线：

```bash
# 你这边（Minecraft 服务跑在 25565）
python -m lanlink tunnel --room 我的世界 --to 127.0.0.1:25565 \
    --relay 你的公网IP:9000 --relay-token 口令

# 朋友那边
python -m lanlink tunnel --room 我的世界 --listen 25565 \
    --relay 你的公网IP:9000 --relay-token 口令
```

然后朋友在 Minecraft 里连 **`127.0.0.1:25565`** 就相当于连到了你家的 25565。

**任何 TCP 服务都能穿**，不限 Minecraft。换个端口就是远程桌面、网页、SSH。

### 连不上时看错误信息

隧道建立失败会告诉你**连的是哪个地址、是超时还是被拒绝、该去查什么**，
而不是甩一个裸的 `timed out`：:

    连接中继 1.2.3.4:9000 超时（等了 10 秒还没连上）。
    排查方向：中继地址/端口是否写对、中继服务是否在运行、
    服务器防火墙和云安全组是否放行了这个端口。

区分「超时」和「拒绝连接」很重要，两者原因完全不同：

| 现象 | 含义 | 先去查 |
|---|---|---|
| **超时** | 包发出去了，没有任何回应 | 地址是否写对、防火墙/安全组是否放行、对端是否在跑 |
| **拒绝连接** | 地址是通的，但那个端口没服务 | 端口号写错了，或者服务没启动 |

### 图形界面里也能用

主界面点「端口转发隧道」，选角色、填地址和房间名、点启动就行。
运行中会实时显示活跃流数和上下行速度，换页面会自动停掉隧道。

命令行和界面是同一套逻辑（都走 `lanlink/tunnel.py`），用哪个都行。

### 三种连法，按需选

**① 同一局域网** —— 广播自动搜索，什么都不用配：

```bash
# 服务端
python -m lanlink tunnel --room 我的世界 --to 127.0.0.1:25565

# 客户端
python -m lanlink tunnel --room 我的世界 --listen 25565
```

**② 不同局域网，但能直连** —— 用 `--addr` 填对方地址，**不需要中继**：

```bash
# 服务端（照常起，不用挂中继）
python -m lanlink tunnel --room x --to 127.0.0.1:25565 --port 50001

# 客户端：直接填对方地址
python -m lanlink tunnel --listen 25565 --addr <对方地址>:50001
```

什么时候能直连？两种常见情况：

| 情况 | 对方地址填什么 |
|---|---|
| 装了 **Tailscale / ZeroTier** 之类的虚拟局域网 | 对方的虚拟 IP（Tailscale 是 `100.x.x.x`） |
| 对方有**公网 IP** 并且路由器上做了端口映射 | 对方的公网 IP |

这两条路都不经过中继，延迟最低。

**没有服务器、也不想折腾？用 Tailscale 最省事**（免费，不需要你有任何服务器）：

1. 两台机器都装上 [Tailscale](https://tailscale.com/)，登录同一个账号
2. 跑 `lanlink doctor` —— 它会直接把虚拟局域网地址挑出来告诉你：
   ```
   [警告] 虚拟局域网：发现虚拟局域网地址：100.101.102.103（Tailscale）
          这是跨网段联机最省事的办法 —— 两台机器装同一个工具并登录，
          然后用「端口转发隧道」的「对方地址」直接填这个虚拟 IP，
          既不需要中继，也不需要公网 IP / 端口映射。
   ```
3. 服务端照常起，客户端在「对方地址」里填**对方的**那个虚拟 IP

同一招对 **ZeroTier / Hamachi / Radmin VPN** 也适用，doctor 都会认出来。

**③ 不同局域网，但你有公网 IP** —— 在路由器上做个端口映射，**对面也不用装任何东西**：

```bash
# 路由器上：外部 50001 → 你的内网 192.168.1.6:50001

# 服务端
python -m lanlink tunnel --room x --to 127.0.0.1:25565 --port 50001

# 客户端（对面只跑这个）
python -m lanlink tunnel --listen 25565 --addr 你的公网IP:50001
```

先跑 `lanlink doctor` 确认一下自己有没有公网 IP —— 它会直接问路由器：

```
[通过] 公网可达：路由器 WAN 口是 113.87.1.1 —— 是公网地址
        好消息：你有公网 IP。
        在路由器上把某个端口映射到本机（比如外部 50001 → 本机 50001），
        然后让对面用「端口转发隧道」的「对方地址」填 <你的公网IP>:50001。
        对面只跑 lanlink.exe 就行，不需要中继、也不用装别的东西。
```

如果是**运营商大内网（CGNAT）**，自检会直接说清楚这条路走不通：

```
[失败] 公网可达：路由器 WAN 口是 100.64.0.7 —— 运营商大内网（CGNAT）
        你的宽带没有公网 IP，外面的人路由不到你，端口映射也救不了。三条出路：
          · 打运营商客服申请公网 IP（电信/联通有时能给，移动基本不给）
          · 两边都装 Tailscale 之类的虚拟局域网，然后直连
          · 找台有公网 IP 的机器跑中继
```

判断依据是问路由器「你的 WAN 口 IP 是多少」（UPnP），**不依赖任何外部服务**。
路由器没开 UPnP 的话问不到，自检会退回来告诉你怎么手动看。

**④ 不同局域网，直连不了** —— 只能走中继：

```bash
python -m lanlink tunnel --room 我的世界 --listen 25565     --relay 你的公网IP:9000 --relay-token 口令
```

为什么直连不了就得用中继？因为两台机器都在 NAT 后面时，**谁也主动连不上谁** ——
这跟软件无关，是 TCP 的性质。必须有第三方在中间搭桥（中继），
或者通过端口映射 / 虚拟局域网把其中一方变得"可直接到达"。

### 参数说明

| 参数 | 说明 |
|---|---|
| `--to 地址:端口` | **服务在这边**。隧道会把流量转发到这个本地地址 |
| `--listen [地址:]端口` | **服务在对面**。本地开这个端口给本机程序连 |
| `--room` | 房间名（局域网模式）/ 中继上的房间号（中继模式）。两端必须一致 |
| `--addr 地址:端口` | **客户端**：直接连这个地址，跳过局域网搜索。虚拟局域网 / 公网 IP 用这条，不需要中继 |
| `--relay` / `--relay-token` | 走公网中继 |
| `--password` | 房间密码，防止别人蹭你的隧道 |

`--listen` 只写端口时默认绑 **`127.0.0.1`** —— 只给本机程序连。写成
`0.0.0.0:25565` 才会对同网段开放，那等于把服务暴露给整个局域网，别不小心。

### 多个人同时连

一条隧道能同时跑多条流，每个人一个 Minecraft 连接互不干扰。房间里也可以有
多个人同时用（流是按人 + 流号区分的）。

### 和"联机框架"是什么关系

同一个中继、同一条连接、同一套口令，只是上面跑的东西不同：

```
┌─ 你的程序 ──── lanlink 房间消息（send / broadcast / on("data")）
│
└─ 现成的服务 ── lanlink 隧道（tunnel --to / --listen）
                    ↑ 都是走同一条连接和中继
```

隧道帧就是普通的房间应用数据加 9 字节头（`"LLTK" + 流号 + 操作码`），所以心跳、
掉线检测、中继转发、二进制完整性这些全都自动复用现成的 ——
`protocol.py` / `link.py` / `relay.py` 一行没改。

### 已知限制

- **只支持 TCP**。UDP 转发没做（Minecraft Java 版是 TCP，基岩版走 UDP，穿不了）。
- 所有流共用一条连接，某条流把发送缓冲撑满时会短暂影响其他流。同时跑几十条
  大流量流才会明显，一两条连接的场景完全够用。
- 服务端（`--to` 那侧）固定当房间主机，所以要能主动连出去（有中继的话只需要出网）。

---

## 部署中继到 Linux 服务器

中继是纯标准库的 TCP 服务，不依赖 tkinter，也不需要图形环境 —— 任何能跑
Python 3.8+ 的 Linux 都能当。**跑在服务器上的时候，用单文件二进制最省事**，
目标机器连 Python 都不用装。

### 一、在 Linux 上构建单文件二进制

PyInstaller **不支持交叉编译**，所以这一步必须在 Linux 机器上做（或者在一台
同架构的 Linux 上做好再拷过去）：

```bash
git clone <你的仓库> && cd 局域网联机工具
python3 -m pip install pyinstaller
python3 packaging/build.py --mode relay
```

产物是 `dist/lanlink-relay`，约 9 MB，拿走就能用：

```bash
./lanlink-relay --port 9000 --token-file /etc/lanlink/token
```

> 不想打包也行，直接 `python3 -m lanlink relay --port 9000` 一样跑，
> 只要那台机器有 Python 3.8+。

### 二、装成 systemd 服务

```bash
# 1. 建一个专用的非特权账号
sudo useradd --system --no-create-home --shell /usr/sbin/nologin lanlink

# 2. 放二进制和配置目录
sudo install -d -o lanlink -g lanlink /opt/lanlink /etc/lanlink
sudo install -o lanlink -g lanlink -m 755 lanlink-relay /opt/lanlink/

# 3. 写口令（别用 --token，那会出现在 ps 输出里）
sudo sh -c 'echo "换成你自己的口令" > /etc/lanlink/token'
sudo chown lanlink:lanlink /etc/lanlink/token
sudo chmod 600 /etc/lanlink/token

# 4. 装服务并启动
sudo cp packaging/systemd/lanlink-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lanlink-relay

# 5. 看状态和日志
systemctl status lanlink-relay
journalctl -u lanlink-relay -f
```

单元文件里已经配好了这些（见 `packaging/systemd/lanlink-relay.service` 的注释）：

| 配置 | 为什么 |
|---|---|
| `Restart=always` | 崩了自动拉起来 |
| `RestartPreventExitStatus=2` | 端口被占这类配置错误不再无限重试 —— 重试一万次也没用，只会刷满日志 |
| `LimitNOFILE=65536` | 每个客户端一个连接，默认的 1024 很快就撞墙 |
| `KillSignal=SIGTERM` + `TimeoutStopSec=10` | 优雅退出：中继把房间里的连接正常关掉再退 |
| `User=lanlink` | 不用 root 跑（9000 是高位端口，不需要特权） |
| `ProtectSystem=strict` 等 | 中继只搬字节不写文件，能锁多紧锁多紧 |

### 三、放行端口

```bash
sudo ufw allow 9000/tcp                    # Ubuntu / Debian
sudo firewall-cmd --permanent --add-port=9000/tcp && sudo firewall-cmd --reload
                                           # CentOS / RHEL / Fedora
```

**云服务器还要单独配安全组** —— 这是最容易漏的一步，本机防火墙放行了但安全组
没放，照样连不上。

### 四、验证它真的能用

在服务器上跑自检。它会自己在进程内起中继 + 主机 + 两个客户端（一个直连、
一个走中继），把收发、广播、二进制完整性、进出房间都验一遍：

```bash
python3 tools/relay_selftest.py --port 9000
```

```
  [通过] 中继启动 —— 0.0.0.0:9000
  [通过] 主机已挂上中继
  [通过] 经中继加入成功 —— 我是 #1000000
  [通过] 中继 -> 主机（关键路径）
  [通过] 100 KB 二进制经中继完整送达 —— 收到 102400 字节
  [通过] 主机关闭后中继清掉房间
  ...
  中继自检全部通过 —— 这台机器上跑中继没问题。
```

### 五、日常运维

```bash
journalctl -u lanlink-relay -n 100        # 最近 100 行日志
journalctl -u lanlink-relay -f            # 实时跟踪
systemctl restart lanlink-relay           # 重启
ss -tlnp | grep 9000                      # 确认在监听
```

日志级别默认 `INFO`，房间上下线、客户端进出都会记一条。想更详细：

```bash
sudo systemctl edit lanlink-relay
# 在打开的编辑器里写：
#   [Service]
#   ExecStart=
#   ExecStart=/opt/lanlink/lanlink-relay --port 9000 --token-file /etc/lanlink/token --log-level DEBUG
```

看当前有哪些房间：

```bash
./lanlink-relay list --relay 127.0.0.1:9000
```

### 常见问题

**`端口 9000 起不来`** —— 服务会以退出码 2 停下（不会无限重启）。用
`ss -tlnp | grep 9000` 看是谁占着，或者换个端口。

**`自检通过，但外面的客户端连不上`** —— 顺序排查：云安全组 → 本机防火墙 →
`ss -tlnp` 确认监听的是 `0.0.0.0` 而不是 `127.0.0.1`。

**`systemctl status` 显示 `code=exited, status=2`** —— 就是上面那条：配置错误，
人工处理完再 `systemctl start`。

**客户端提示"房间不存在"** —— 中继本身没问题，是主机还没挂上来，或者挂的时候
用的房间号和客户端填的不一致。用 `list --relay` 看看中继上实际有哪些房间。

---

## 在代码里用

### 主机

```python
from lanlink import Host

host = Host(
    "我的房间",             # 房间名，别人看到的就是这个
    name="房主",            # 自己的昵称
    port=0,                # 0 = 让系统挑空闲端口
    password="",           # 房间密码，空 = 不设
    max_players=16,
).start()

print(f"房间号 {host.room_id}，地址 {host.address}:{host.port}")

@host.on("peer_join")
def _(peer):
    print(f"{peer} 来了")
    host.broadcast(f"欢迎 {peer.name}".encode(), source=0)

@host.on("peer_leave")
def _(peer, reason):
    print(f"{peer} 走了：{reason}")

@host.on("data")
def _(source, data, is_json):
    # source 是发送者的 peer_id，0 表示主机自己
    host.broadcast(data, source=source, exclude=source)   # 转发给别人

host.serve_forever()   # 阻塞运行
```

### 客户端

```python
from lanlink import Client, scan

# 方式一：自动搜索
rooms = scan(timeout=2.0)
client = Client.connect(rooms[0].address, rooms[0].port, name="小明")

# 方式二：直接指定地址
client = Client.connect("192.168.1.10", 50001, name="小明", password="")

# 方式三：走中继
client = Client.join_via_relay("1.2.3.4", 9000, "3f2a1b9c", name="小明", token="口令")

print(f"我是 #{client.peer_id}")

client.send(b"只给主机")                    # 主机本人收
client.send_to(2, b"只给 2 号")             # 请主机转发给 2 号
client.broadcast(b"给所有人")               # 请主机转发给其他人

client.send_json({"动作": "移动", "x": 10})  # JSON 也直接发

@client.on("data")
def _(source, data, is_json):
    print(f"来自 #{source}：{data}")

client.close()
```

### 发结构化数据

数据帧带一个 JSON 标志位，收端不用自己去猜：

```python
client.broadcast_json({"t": "chat", "text": "你好"})

@host.on("data")
def _(source, data, is_json):
    if is_json:
        msg = json.loads(data)
    else:
        msg = data          # 原始字节，游戏同步包走这条
```

---

## 完整例子

`examples/` 下有两个可以直接跑的：

- **`minimal.py`** —— 最短的收发演示，先看这个
  ```bash
  python examples/minimal.py host
  python examples/minimal.py client 192.168.1.10:50001
  ```

- **`game_state_sync.py`** —— 主机权威模式：20Hz 广播世界状态、客户端上报输入、
  自带 RTT 测量。想做联机游戏的话这个结构可以直接抄
  ```bash
  python examples/game_state_sync.py host
  python examples/game_state_sync.py client 192.168.1.10:50001
  ```

`tools/` 下是维护用的脚本：

- **`soak_test.py`** —— 长时间稳定性压测（见[长时间稳定性](#长时间稳定性)）
- **`version_matrix.py`** —— 跨 Python 版本跑测试（见[兼容性](#兼容性)）
- **`exe_smoke_test.py`** —— 把打包好的 exe 当黑盒跑一遍，确认它真的能用
- **`make_release.py`** —— 把 exe 和一份使用说明收拾成可以直接发给对面的 zip
- **`relay_selftest.py`** —— 中继服务器自检，部署到 Linux 后在那台机器上跑
- **`tunnel_e2e_test.py`** —— 隧道的端到端验证（三个真实进程 + 中继）

`packaging/` 下是打包相关：

- **`build.py`** —— 一键打包（`--mode gui|cli|relay|both|all`；`--server` 编直连地址，`--server-relay` 编中继地址）
- **`launcher_gui.py`** / **`launcher_cli.py`** / **`launcher_relay.py`** —— 三个入口
- **`systemd/lanlink-relay.service`** —— Linux 服务单元（见[部署到 Linux](#部署中继到-linux-服务器)）

---

## 设计

### 分层

```
lanlink/
├── protocol.py    线协议：帧格式 + 路由头 + 编解码
├── link.py        一条 TCP 链路：收线程、发锁、心跳、掉线判定
├── discovery.py   UDP 广播发现：Beacon（主机侧）/ Scanner（客户端侧）
├── node.py        Host / Client / RelayAttachment —— 房间逻辑都在这里
├── relay.py       公网中继服务器
├── tunnel.py      端口转发隧道：在房间之上做流复用
├── doctor.py      环境自检（公网可达性 / 防火墙 / 网段 / 端口 / 收发自测）
├── upnp.py        问路由器要 WAN IP、自动开端口（纯标准库实现）
├── text.py        文本编码兜底（终端 GBK / 孤立代理字符）
├── cli.py         命令行
└── gui/           图形界面（tkinter）
    ├── app.py     主窗口、线程安全队列、各对话框
    ├── pages.py   起始页 / 房间页 / 中继页 / 自检弹窗
    ├── tunnel_page.py  端口转发隧道页
    └── widgets.py 聊天区、成员表、房间列表、字体与 DPI 适配
```

只有 `node.py` 里的三个类是给使用者用的，底下几层想换随时能换。

### 线协议

TCP 上流动的都是这种帧：

```
+-------------+-----------+----------------------+
| length (4)  | kind (1)  | payload (length - 1) |
+-------------+-----------+----------------------+
```

`length` 是大端 uint32，含 `kind` 字节本身。`kind` 有四种：

| kind | 用途 |
|---|---|
| `KIND_JSON` | 握手 / 心跳 / 房间管理 |
| `KIND_APP` | 应用数据，payload 前面有 6 字节路由头 |
| `KIND_RELAY_CTRL` | 中继控制，只在主机 ↔ 中继之间 |
| `KIND_RELAY_DATA` | 中继转发，只在主机 ↔ 中继之间 |

应用数据的路由头是 `route(1) + flags(1) + peer_id(4)`。`route` 决定这条数据怎么走：

| route | 方向 | 含义 |
|---|---|---|
| `ROUTE_DIRECT` | 客户端 → 主机 | 只交给主机本人 |
| `ROUTE_PEER` | 客户端 → 主机 | 请主机转发给 `peer_id` |
| `ROUTE_BCAST` | 客户端 → 主机 | 请主机转发给除我之外所有人 |
| `ROUTE_DELIVER` | 主机 → 客户端 | 投递，`peer_id` 标明来源（0 = 主机） |

客户端之间不直接连，所有消息都过主机。这样转发策略、权限、防作弊都只有一个
地方要管 —— 对回合制和小规模实时游戏完全够用；真要 mesh 直连的话把 `link.py`
复用起来另接一套拓扑就行。

### 中继是怎么接进来的

主机和中继之间**只有一条 TCP 连接**，所有中继客户端都复用它，靠帧里的 `peer_id`
区分。客户端的连接对中继来说是透明的字节流：

```
客户端 ──TCP──> 中继 ──TCP──> 主机
```

而主机看到的每个中继客户端都是一个"虚拟 peer"，接口跟直连 peer 一模一样
（`_Peer` 这个类就是干这个的），所以业务代码里没有任何 `if 是不是中继` 的分支。

中继分配的 `peer_id` 从 `RELAY_ID_BASE`（1000000）起，跟主机自己发的 1、2、3…
天然错开，不会撞。

### 掉线怎么判定

三层心跳，都在 `Link` 里自动处理，上层看不到心跳帧：

1. 每 10 秒发一个 `ping`，收到 `ping` 自动回 `pong`；
2. 35 秒收不到任何东西就判定掉线，主动关闭；
3. 中继模式下，客户端断开会由中继通知主机（`leave` 消息）。

第 2 条很关键：局域网里拔网线时 TCP 连接会一直"看起来正常"，不靠空闲超时
根本发现不了对端已经没了。

### 房间发现为什么发两种包

主机每 2 秒广播一次 `announce`；客户端平时被动听，主动扫描时额外发 `query`。
主机收到 `query` 立刻单播回一份 `announce`。

两条路都留着是因为很多交换机、AP 隔离模式或者 Windows 防火墙会吃掉广播包，
但单播往往还通。所以"广播听不见"的网络里，主动探测照样能发现房间。

扫描器还特意用了两个 socket（临时端口发探测 + 约定端口收广播）—— 因为同一台
机器上同时开房和扫描时，如果两个 socket 绑同一个端口，回包会被随机投递，
扫描就会时灵时不灵。这个坑有回归测试守着。

### 图形界面的线程安全

tkinter 只能在主线程里碰，而网络事件全都来自后台线程。所以定了一条铁律：

```python
# 后台线程里 —— 错
self.chat.add("小明", "你好")

# 后台线程里 —— 对
app.post(self.chat.add, "小明", "你好")
```

`post` 把调用塞进队列，主线程每 40ms 取一次。直接跨线程动控件会出各种玄学
问题 —— 有时候能跑、有时候卡死、有时候在别人机器上才崩，非常难查。
`tests/test_gui.py` 里有专门验证这条队列的测试（多线程灌 200 条更新，一条都不能丢）。

**踩过的两个真坑**，都是同一个根因，值得单独记一笔：

| 错误写法 | 后果 |
|---|---|
| 后台线程里 `self.app.nickname.get()` | `RuntimeError: main thread is not in main loop` |
| 后台线程里 `self.after(0, callback)` | 同上 |

`StringVar.get()` 和 `after()` 看着不像"动控件"，但**它们都会进 Tcl**，
所以照样受限。实测这三种写法在后台线程里**全都会抛异常**：

```python
app.nickname.get()      # RuntimeError: main thread is not in main loop
app.after(0, fn)        # 同上
app.set_status("x")     # 同上
```

规矩很简单：**后台线程里除了 `app.post(...)`，什么都别碰。**
需要读输入框的值，就在主线程读好、当参数传进去。这两个坑现在都有回归测试守着
（`tests/test_gui_tunnel.py`）。

### 布局的坑：控件建在了错误的 parent 上

同一个隧道页还踩过一个**功能测试完全查不出来**的问题：6 个输入框全压在了
「我是哪一边」的单选按钮上面。

根因是 tkinter 的一条规则：

> ``widget.grid()`` 永远用控件**自己的 parent** 当几何主。

所以「先把控件 new 出来，再让辅助函数把它摆进某个容器」这种写法**行不通** ——
控件会跑进它自己 parent 的格子里：

```python
def _labeled(self, parent, row, label, widget):   # widget 的 parent 是 form
    holder = ttk.Frame(parent)
    holder.grid(row=row, ...)
    widget.grid(row=0, column=1)   # ← 进了 form 的 (0,1)，不是 holder 的
```

正确的做法是**让辅助函数自己创建控件**，parent 传 holder：

```python
def _labeled(self, parent, row, label, var):
    holder = ttk.Frame(parent)
    holder.grid(row=row, ...)
    entry = ttk.Entry(holder, textvariable=var)   # parent 是 holder
    entry.grid(row=0, column=1)                   # ← 正确落位
```

这种 bug 功能测试全绿（控件都在、值也对、事件也正常），只有肉眼能看出来。
所以加了个几何检查 `tests/test_gui_layout.py`：遍历所有页面和弹窗，确认
没有两个控件抢同一个网格格子。它自己有测试守着（故意制造冲突，确认能被检出）。

### 打包成 exe 踩的坑

PyInstaller 打出来的东西在编码上跟源码运行不一样，两个坑都值得记：

1. **exe 忽略 `PYTHONIOENCODING`**。被管道/重定向时它按系统 ANSI 代码页
   （中文 Windows 是 GBK）输出，别人拿到的就是乱码。解法是在 launcher 里
   调 `force_utf8_when_piped()` —— 只在"不是终端"的时候切成 UTF-8，
   直接连终端时不动（Python 在 Windows 控制台上走的是 Unicode 通道，本来就正常）。
2. **孤立代理字符会让程序崩**。管道里喂进来的 UTF-8 被按 GBK 解码，会产生
   `\udc80` 这类"孤立代理字符"，它们能存能传，但 `.encode("utf-8")` 会直接抛
   `UnicodeEncodeError`。所以发出去之前统一走 `text.encode_text()`，
   用 `surrogateescape` 原路还原成字节。

另外 `pyproject.toml` 里的 `packages` 必须显式写上 `lanlink.gui` ——
只写 `lanlink` 的话子包不会被打进 wheel，装完跑 `lanlink gui` 会 ImportError，
而从源码目录跑却是好的，特别容易漏过去。

---

## 测试

```bash
python -m unittest discover -s tests -v
```

245 个测试，全部是真的起 socket、真的连、真的发数据，包括中继和混合组网场景。
GUI 那部分用 `app.update()` 手动驱动事件循环，把界面当普通对象来断言（会有一个
窗口出现，见 `tests/test_gui.py` 开头的说明）。

```bash
python -m unittest tests.test_protocol      # 协议编解码（含半包/粘包/超长帧）
python -m unittest tests.test_integration   # 端到端（直连/中继/混合/发现）
python -m unittest tests.test_lifecycle     # 资源回收 / 引用环 / 幽灵成员
python -m unittest tests.test_doctor        # 环境自检的解析逻辑
python -m unittest tests.test_gui           # 图形界面（需要一个可见的桌面）
python -m unittest tests.test_packaging     # 打包配置 / systemd 单元 / 入口透传
python -m unittest tests.test_tunnel        # 端口转发隧道（真起 TCP 服务验证）
python -m unittest tests.test_gui_tunnel    # 隧道页面 + 自检弹窗（需要可见桌面）
python -m unittest tests.test_gui_layout    # 布局检查：控件不能互相盖住
python -m unittest tests.test_errors        # 错误信息必须能照着排查
python -m unittest tests.test_upnp          # UPnP 解析与 CGNAT 判定
```

### 兼容性

全部测试在以下版本上实跑通过（用 `uv` 拉取的隔离解释器，未改动系统环境）：

| Python | 3.8.6 | 3.9.25 | 3.10.20 | 3.11.15 | 3.12.13 | 3.13.13 | 3.14.5 |
|---|---|---|---|---|---|---|---|
| 245 项测试 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

```bash
python tools/version_matrix.py        # 自己再跑一遍这个矩阵
```

`pip install .` 也在 3.8（pip 20.3.1 升级后）和 3.12（pip 25.0.1）上验证过，
`lanlink` 命令从任意目录都能正常调用。

代码里没有超出 3.8 的语法，也没有用到任何已废弃的 API。

### 长时间稳定性

单元测试证明不了"跑几小时会不会漏"，所以另有一个压测脚本：

```bash
python tools/soak_test.py --minutes 30
```

它会持续高频收发、让客户端周期性进出，并全程采样线程数、句柄数、内存，
最后断言成员表清空、无幽灵成员、数据无错乱、资源回落到基线。

实测 30 分钟：7 人在线（主机 + 3 局域网 + 3 中继），10 条/秒持续收发，
每分钟换掉一批客户端，共 59 次采样。

| 指标 | 结果 |
|---|---|
| 消息 | 收到 97,173 条，畸形 **0** 条 |
| 内存 | 19.8 → 20.1 MB（基本持平） |
| 线程 | 全程恒为 32；收尾后回到 1 |
| 成员表 | 全程恒为 7 人；每轮更替后人数准确；全部断开后只剩主机 |
| 句柄 | 在 456 ~ 582 之间震荡，**后半段 59 个采样里上涨 15 次、下跌 14 次** |

句柄那一项是判断有无泄漏的关键：泄漏的特征是**单调上涨**，而实测是有涨有跌
且会回落（例如 25.2 分钟时从 582 掉回 490），收尾后 352 —— 比运行中的基线还低。
所以是回收步调，不是泄漏。

> **这里踩过一个真坑，值得记下来。** 最初句柄数随客户端更替单调上涨（约 +37/分钟）。
> 原因是 `Link` 的回调是闭包，捕获着 `Host`，而 `Host` 又通过成员表反向持有
> `Link` —— 形成引用环，只能等循环 GC 来收；在持续高频收发的进程里，长命对象
> 会被晋升到 gen2，等不到回收，看起来就像稳定的泄漏。
>
> 修法是断开时主动摘掉回调引用（`Link._release_refs`），让引用计数就能回收干净，
> 不必赌 GC 什么时候跑。改后增速从 +37/分钟降到约 +7/分钟，再配合差分实验
> （只换本地客户端 +0.0/轮，只换中继客户端 +0.5/轮）确认剩下的只是 GC 步调。
>
> `tests/test_lifecycle.py` 守住这个行为 —— 它会断言 `Link` **不需要**
> `gc.collect()` 也能被回收。

---

## 常见问题

**扫描不到房间 / 连不上？**

先在两台机器上都跑 `lanlink doctor`。绝大多数情况它会直接告诉你原因。

最典型的一种：**Windows 防火墙的 Python 放行规则只覆盖了"公用"网络，而当前网络被归类为"专用"。**
本机上实测确实如此 —— 所有 `python.exe` 入站规则都是 `Profile = Public`，
没有一条 `Private`。如果你的网络恰好是"专用"，入站会被全部拦掉。

修复（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName 'lanlink' -Direction Inbound `
  -Program (Get-Command python).Source -Action Allow -Profile Any
```

或者手动到「设置 → 网络和 Internet → Windows 防火墙 → 允许应用通过防火墙」，
找到 Python，把**"专用"和"公用"两栏都勾上**。

其他排查顺序：主机是否已启动 → 两台机器是否同一网段（`ipconfig` 看前三段是否一致）
→ 试试直接指定地址 `--addr 192.168.1.10:50001` 绕开发现环节。

**非 Windows（Linux / macOS）？**
`doctor` 会跳过防火墙检查（那部分依赖 PowerShell），其余项照常。
放行端口用 `ufw allow 50001/tcp` 或对应平台的防火墙工具。

**主机端口是多少？**
`--port 0`（默认）会让系统随机挑一个空闲端口，启动后打印在"局域网地址"那一行。
想固定就用 `--port 50001`。发现机制会自动带上真实端口，不用手动同步。

**中继连接失败？**
确认中继那台机器的安全组/防火墙放行了端口；如果 `--token` 设了，主机和客户端
两边必须填一致。

**想限制只有特定的人能进？**
用 `--password`（房间密码，主机校验）或 `--relay-token`（中继口令，中继校验）。
两个是独立的：口令挡住的是"用你的中继"，密码挡住的是"进你的房间"。

**传输有大小限制吗？**
单帧上限 8 MB（`protocol.MAX_FRAME`），超了会被拒绝。要传大文件建议自己在应用层
分片。

---

## 已知边界

- 同一台机器上开多个房间会共用发现端口，广播回包可能互相抢（同一台机器一般也
  不需要开多个房）。
- 客户端之间不直连，大房间（几十人以上）时主机上行会成为瓶颈。
- 消息不保证送达顺序之外的东西 —— TCP 保证有序，但没有应用层重传/确认机制，
  需要可靠投递的话得自己做 ACK。
- 中继只做字节搬运，不加密。要过不可信网络请自行套 TLS 或用 VPN。

---

## License

MIT
