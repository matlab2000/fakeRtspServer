"""回归测试：媒体解析、RTSP 分帧、端到端会话。运行：python -m unittest -v test_fake_rtsp"""

import os
import re
import socket
import struct
import threading
import unittest

from ServerWorker import RtspRequestParser, ServerWorker, parse_client_port
from VideoStream import VideoStream, rtp_payload

MEDIA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "video.dat")


def read_response(sock):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError("connection closed before response headers")
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    lines = head.decode("ascii").split("\r\n")
    status = lines[0]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().upper()] = value.strip()
    length = int(headers.get("CONTENT-LENGTH", "0"))
    while len(rest) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    return status, headers, rest[:length]


class VideoStreamTests(unittest.TestCase):
    def test_real_media(self):
        v = VideoStream(MEDIA)
        self.assertEqual(v.frame_count, 300)
        self.assertAlmostEqual(v.duration, 9.97, places=2)
        self.assertEqual(v.sps_info["profile_idc"], 100)
        self.assertEqual(v.sps_info["profile_compatibility"], 0x00)
        self.assertEqual(v.sps_info["level_idc"], 40)
        self.assertEqual((v.sps_info["width"], v.sps_info["height"]), (1920, 1080))
        sdp = v.get_sdp()
        self.assertIn("profile-level-id=640028", sdp)
        self.assertIn("a=framesize:96 1920-1080", sdp)
        self.assertIn("sprop-parameter-sets=Z2QAKKyyAPAET8s1AQEBQAAAAwBAAAAPA8YMkg", sdp)
        self.assertTrue(sdp.endswith("\r\n"))

    def test_next_frame_and_rewind(self):
        v = VideoStream(MEDIA)
        first = v.next_frame()
        self.assertGreater(len(first[1]), 1)
        self.assertEqual(first[1][0][0] & 0x1f, 7)
        self.assertEqual(v.next_frame()[0] - first[0], 3000)
        v.rewind()
        self.assertEqual(v.next_frame(), first)

    def test_bad_magic_does_not_hang(self):
        with self.assertRaisesRegex(ValueError, "bad magic"):
            VideoStream.__new__(VideoStream)._load_from_bytes(b"\x00" * 16)

    def test_truncated_record(self):
        with self.assertRaisesRegex(ValueError, "truncated record payload"):
            VideoStream.__new__(VideoStream)._load_from_bytes(b"\xaa\x55\x00\xff\x01")

    def test_rtp_payload_with_csrc_and_extension(self):
        pkt = struct.pack(">BBHII", 0x91, 96, 1, 2, 3) + b"\xaa\xbb\xcc\xdd"
        pkt += struct.pack(">HH", 1, 1) + b"\xee\xff\x00\x11" + b"\x67\x42\x00"
        self.assertEqual(rtp_payload(pkt), b"\x67\x42\x00")


class ParserTests(unittest.TestCase):
    def test_split_and_coalesced(self):
        parser = RtspRequestParser()
        a = b"OPTIONS rtsp://x RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        b = b"PLAY rtsp://x RTSP/1.0\r\nCSeq: 2\r\n\r\n"
        self.assertEqual(parser.feed(a[:20]), [])
        reqs = parser.feed(a[20:] + b)
        self.assertEqual([r.method for r in reqs], ["OPTIONS", "PLAY"])
        self.assertEqual([r.headers["CSEQ"] for r in reqs], ["1", "2"])

    def test_body_and_colon_in_header(self):
        parser = RtspRequestParser()
        data = (b"ANNOUNCE rtsp://x RTSP/1.0\r\nCSeq: 3\r\n"
                b"Content-Base: rtsp://1.2.3.4:554/live\r\n"
                b"Content-Length: 5\r\n\r\nhello"
                b"OPTIONS rtsp://x RTSP/1.0\r\nCSeq: 4\r\n\r\n")
        reqs = parser.feed(data)
        self.assertEqual(reqs[0].headers["CONTENT-BASE"], "rtsp://1.2.3.4:554/live")
        self.assertEqual(reqs[0].body, b"hello")
        self.assertEqual(reqs[1].method, "OPTIONS")

    def test_client_port_formats(self):
        self.assertEqual(parse_client_port("RTP/AVP;unicast;client_port=59088-59089"), (59088, 59089))
        self.assertEqual(parse_client_port("RTP/AVP/UDP;unicast;mode=PLAY;client_port=5000"), (5000, 5001))
        self.assertIsNone(parse_client_port("RTP/AVP/TCP;interleaved=0-1"))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.worker = None

        def accept_loop():
            conn, addr = self.server.accept()
            self.worker = ServerWorker(
                {"rtspSocket": (conn, addr)}, self.port, mediaFile=MEDIA
            )
            self.worker.run()

        self.accepter = threading.Thread(target=accept_loop, daemon=True)
        self.accepter.start()
        self.client = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.client.settimeout(3)
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("127.0.0.1", 0))
        self.rtp.settimeout(3)
        self.cseq = 0

    def tearDown(self):
        self.rtp.close()
        self.client.close()
        if self.worker:
            self.worker.close()
        self.server.close()

    def request(self, method, headers=None):
        self.cseq += 1
        lines = ["%s rtsp://127.0.0.1:%d/trackID=1 RTSP/1.0" % (method, self.port),
                 "CSeq: %d" % self.cseq]
        for key, value in (headers or {}).items():
            lines.append("%s: %s" % (key, value))
        self.client.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        return read_response(self.client)

    def test_session_lifecycle_and_rtp(self):
        status, headers, body = self.request("OPTIONS")
        self.assertIn("200 OK", status)
        self.assertIn("DESCRIBE", headers["PUBLIC"])

        status, headers, body = self.request("DESCRIBE")
        self.assertIn("200 OK", status)
        self.assertIn("profile-level-id=640028", body.decode())
        session = headers["SESSION"]
        self.assertTrue(re.fullmatch(r"\d+", session))

        port = self.rtp.getsockname()[1]
        status, headers, body = self.request("SETUP", {
            "Session": session,
            "Transport": "RTP/AVP;unicast;mode=PLAY;client_port=%d-%d" % (port, port + 1),
        })
        self.assertIn("200 OK", status)
        self.assertIn("client_port=%d-%d" % (port, port + 1), headers["TRANSPORT"])
        self.assertIn("server_port=", headers["TRANSPORT"])

        status, _, _ = self.request("PLAY", {"Session": session})
        self.assertIn("200 OK", status)
        packets = [self.rtp.recv(2048) for _ in range(5)]
        headers = [struct.unpack(">BBHII", pkt[:12]) for pkt in packets]
        for b0, b1, seq, ts, ssrc in headers:
            self.assertEqual(b0 >> 6, 2)
            self.assertEqual(b1 & 0x7f, 96)
            self.assertIn(b1 >> 7, (0, 1))
            self.assertEqual(ts, headers[0][3])  # 同一帧内 timestamp 相同
            self.assertEqual(ssrc, headers[0][4])
        self.assertEqual(packets[0][12] & 0x1f, 7)  # 首包是 SPS
        seqs = [h[2] for h in headers]
        self.assertEqual(seqs, [(seqs[0] + i) & 0xffff for i in range(len(seqs))])

        status, _, _ = self.request("OPTIONS", {"Session": session})
        self.assertIn("200 OK", status)
        status, _, _ = self.request("PAUSE", {"Session": session})
        self.assertIn("200 OK", status)
        status, _, _ = self.request("TEARDOWN", {"Session": session})
        self.assertIn("200 OK", status)

    def test_wrong_session_rejected(self):
        self.request("DESCRIBE")
        status, _, _ = self.request("PLAY", {"Session": "1"})
        self.assertIn("454", status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
