"""协议编解码的单元测试 —— 这部分出问题最难查，所以测细一点。"""

import socket
import struct
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink.protocol import (  # noqa: E402
    FLAG_JSON,
    KIND_APP,
    KIND_JSON,
    MAX_FRAME,
    ROUTE_BCAST,
    ROUTE_DELIVER,
    ProtocolError,
    decode_frames,
    pack_app,
    pack_frame,
    pack_json,
    pack_relay_body,
    recv_frame,
    split_relay_data,
    unpack_app,
    unpack_json,
)


class TestFrame(unittest.TestCase):
    def test_roundtrip(self):
        blob = pack_frame(KIND_APP, b"hello")
        frames, rest = decode_frames(blob)
        self.assertEqual(frames, [(KIND_APP, b"hello")])
        self.assertEqual(rest, b"")

    def test_empty_payload(self):
        blob = pack_frame(KIND_JSON)
        self.assertEqual(len(blob), 5)
        frames, rest = decode_frames(blob)
        self.assertEqual(frames, [(KIND_JSON, b"")])
        self.assertEqual(rest, b"")

    def test_header_layout(self):
        blob = pack_frame(KIND_APP, b"abc")
        length, kind = struct.unpack("!IB", blob[:5])
        self.assertEqual(kind, KIND_APP)
        self.assertEqual(length, 4)  # kind 1 字节 + payload 3 字节

    def test_incremental_decode(self):
        """喂一半不该吐出帧，剩下一半到了才吐。"""
        blob = pack_frame(KIND_APP, b"abcdef")
        frames, rest = decode_frames(blob[:4])
        self.assertEqual(frames, [])
        self.assertEqual(rest, blob[:4])

        frames, rest = decode_frames(rest + blob[4:])
        self.assertEqual(frames, [(KIND_APP, b"abcdef")])
        self.assertEqual(rest, b"")

    def test_two_frames_one_buffer(self):
        blob = pack_frame(KIND_APP, b"one") + pack_frame(KIND_JSON, b"two")
        frames, rest = decode_frames(blob)
        self.assertEqual(frames, [(KIND_APP, b"one"), (KIND_JSON, b"two")])
        self.assertEqual(rest, b"")

    def test_oversized_frame_rejected(self):
        evil = struct.pack("!IB", MAX_FRAME + 10, KIND_APP)
        with self.assertRaises(ProtocolError):
            decode_frames(evil)

    def test_zero_length_rejected(self):
        evil = struct.pack("!IB", 0, KIND_APP)
        with self.assertRaises(ProtocolError):
            decode_frames(evil)


class TestAppRoutes(unittest.TestCase):
    """``pack_app`` 只出 payload，帧头由 ``pack_frame`` 加 —— 这个约定不能破。"""

    def test_pack_app_returns_bare_payload(self):
        body = pack_app(ROUTE_DELIVER, 42, b"payload", is_json=True)
        self.assertEqual(len(body), 6 + len(b"payload"))  # 路由头 6 字节

    def test_roundtrip(self):
        blob = pack_frame(KIND_APP, pack_app(ROUTE_DELIVER, 42, b"payload", is_json=True))
        kind, payload = decode_frames(blob)[0][0]
        self.assertEqual(kind, KIND_APP)
        route, peer_id, is_json, data = unpack_app(payload)
        self.assertEqual(route, ROUTE_DELIVER)
        self.assertEqual(peer_id, 42)
        self.assertTrue(is_json)
        self.assertEqual(data, b"payload")

    def test_binary_flag_off(self):
        blob = pack_frame(KIND_APP, pack_app(ROUTE_BCAST, 0, b"\x00\x01\x02"))
        _, _, is_json, data = unpack_app(decode_frames(blob)[0][0][1])
        self.assertFalse(is_json)
        self.assertEqual(data, b"\x00\x01\x02")

    def test_truncated_header(self):
        with self.assertRaises(ProtocolError):
            unpack_app(b"\x01\x02")

    def test_peer_id_wraps_to_uint32(self):
        blob = pack_frame(KIND_APP, pack_app(ROUTE_BCAST, 1 << 33 | 7, b""))
        _, peer_id, _, _ = unpack_app(decode_frames(blob)[0][0][1])
        self.assertEqual(peer_id, 7)


class TestJson(unittest.TestCase):
    def test_roundtrip_unicode(self):
        obj = {"t": "hello", "name": "小明", "n": 3}
        payload = decode_frames(pack_json(obj))[0][0][1]
        self.assertEqual(unpack_json(payload), obj)

    def test_bad_json(self):
        with self.assertRaises(ProtocolError):
            unpack_json(b"{not json")


class TestRelayBody(unittest.TestCase):
    def test_roundtrip(self):
        body = pack_relay_body(1000001, KIND_APP, b"inner")
        peer_id, kind, payload = split_relay_data(body)
        self.assertEqual(peer_id, 1000001)
        self.assertEqual(kind, KIND_APP)
        self.assertEqual(payload, b"inner")

    def test_too_short(self):
        with self.assertRaises(ProtocolError):
            split_relay_data(b"\x00\x00")


class TestRecvFrameOverSocket(unittest.TestCase):
    """真的过一遍 socket，确认 recv 循环处理半包/粘包没问题。"""

    def setUp(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.addr = self.server.getsockname()

    def tearDown(self):
        self.server.close()

    def _serve(self, chunks, delay=0.02):
        def worker():
            conn, _ = self.server.accept()
            with conn:
                for chunk in chunks:
                    conn.sendall(chunk)
                    threading.Event().wait(delay)
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return t

    def test_split_frame(self):
        """把一帧拆成三块发，接收端要能拼回来。"""
        blob = pack_frame(KIND_APP, b"hello world")
        self._serve([blob[:3], blob[3:8], blob[8:]])
        sock = socket.create_connection(self.addr)
        with sock:
            self.assertEqual(recv_frame(sock), (KIND_APP, b"hello world"))

    def test_two_frames_coalesced(self):
        """两帧挤在一次 send 里也要分得开。"""
        blob = pack_frame(KIND_JSON, b"a") + pack_frame(KIND_APP, b"bb")
        self._serve([blob])
        sock = socket.create_connection(self.addr)
        with sock:
            self.assertEqual(recv_frame(sock), (KIND_JSON, b"a"))
            self.assertEqual(recv_frame(sock), (KIND_APP, b"bb"))

    def test_clean_close_returns_none(self):
        self._serve([])
        sock = socket.create_connection(self.addr)
        with sock:
            self.assertIsNone(recv_frame(sock))

    def test_close_mid_frame_returns_none(self):
        """帧发一半就断开，应该返回 None 而不是死等或抛奇怪的错。"""
        blob = pack_frame(KIND_APP, b"0123456789")
        self._serve([blob[:6]])
        sock = socket.create_connection(self.addr)
        with sock:
            self.assertIsNone(recv_frame(sock))


if __name__ == "__main__":
    unittest.main(verbosity=2)
