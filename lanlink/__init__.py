"""lanlink —— 局域网优先的通用联机框架。

一句话：把"让几台机器互相发数据"这件事从你的项目里拿掉。

* 主机开一间房，自动在局域网里广播；客户端扫一下就能看到。
* 加入之后大家就是一个房间里的成员，能单播、能广播。
* 跨网段的话把房间挂到公网中继上，对上层代码毫无区别。

最小例子 —— 主机::

    from lanlink import Host

    host = Host("我的房间").start()
    print("房间号", host.room_id, "端口", host.port)

    @host.on("data")
    def _(source, data, is_json):
        print(source, "说：", data.decode())
        host.broadcast(data, source=source)

    host.serve_forever()

客户端::

    from lanlink import Client, scan

    for room in scan():
        print(room)

    client = Client.connect("192.168.1.10", 50001, name="小明")
    client.broadcast(b"大家好")

    @client.on("data")
    def _(source, data, is_json):
        print(source, "说：", data.decode())
"""

from .discovery import DISCOVERY_PORT, RoomInfo, Scanner, local_ip, scan
from .link import Link, LinkClosed, connect
from .node import (
    PROTOCOL_VERSION,
    RELAY_ID_BASE,
    Client,
    Host,
    Node,
    PeerInfo,
    RelayAttachment,
    default_name,
)
from .protocol import ProtocolError
from .relay import DEFAULT_RELAY_PORT, RelayRoomInfo, RelayServer, list_relay_rooms

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "DISCOVERY_PORT",
    "DEFAULT_RELAY_PORT",
    "PROTOCOL_VERSION",
    "RELAY_ID_BASE",
    "Host",
    "Client",
    "Node",
    "PeerInfo",
    "RelayAttachment",
    "RelayServer",
    "RelayRoomInfo",
    "RoomInfo",
    "Scanner",
    "Link",
    "LinkClosed",
    "ProtocolError",
    "connect",
    "default_name",
    "local_ip",
    "list_relay_rooms",
    "scan",
]
