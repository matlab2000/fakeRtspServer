"""单个 RTSP 客户端会话：请求解析、状态机、RTP/UDP 发送。"""

import re
import socket
import struct
import threading
import time
from random import randint

from VideoStream import CLOCK_RATE, VideoStream


class RtspRequest:
    def __init__(self, method, uri, version, headers, body):
        self.method = method
        self.uri = uri
        self.version = version
        self.headers = headers
        self.body = body


class RtspRequestParser:
    """按 CRLFCRLF + Content-Length 切分 TCP 上的 RTSP 请求，兼容粘包/半包。"""

    def __init__(self, max_size=65536):
        self.buffer = b""
        self.max_size = max_size

    def feed(self, data):
        self.buffer += data
        if len(self.buffer) > self.max_size:
            raise ValueError("RTSP request exceeds %d bytes" % self.max_size)
        requests = []
        while True:
            pos = self.buffer.find(b"\r\n\r\n")
            if pos < 0:
                break
            head = self.buffer[:pos].decode("ascii", "replace")
            rest = self.buffer[pos + 4:]
            lines = head.split("\r\n")
            length = 0
            for line in lines[1:]:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                if key.strip().upper() == "CONTENT-LENGTH":
                    try:
                        length = int(value.strip())
                    except ValueError as exc:
                        raise ValueError("invalid Content-Length") from exc
                    if length < 0:
                        raise ValueError("negative Content-Length")
            if len(rest) < length:
                break
            body = rest[:length]
            self.buffer = rest[length:]
            parts = lines[0].split()
            if len(parts) != 3:
                raise ValueError("invalid RTSP request line: %r" % lines[0])
            headers = {}
            for line in lines[1:]:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                headers[key.strip().upper()] = value.strip()
            requests.append(RtspRequest(parts[0].upper(), parts[1], parts[2], headers, body))
        return requests


def parse_client_port(transport):
    """从 Transport 头里取 client_port，不依赖分号位置。"""
    match = re.search(r"client_port\s*=\s*(\d+)(?:\s*-\s*(\d+))?", transport, re.I)
    if not match:
        return None
    first = int(match.group(1))
    return first, int(match.group(2)) if match.group(2) else first + 1


class ServerWorker:
    OPTIONS = "OPTIONS"
    DESCRIBE = "DESCRIBE"
    SETUP = "SETUP"
    PLAY = "PLAY"
    PAUSE = "PAUSE"
    TEARDOWN = "TEARDOWN"
    GET_PARAMETER = "GET_PARAMETER"

    INIT, READY, PLAYING = 0, 1, 2

    def __init__(self, clientInfo, serverPort=554, mediaFile="video.dat"):
        self.clientInfo = clientInfo
        self.ip = socket.gethostbyname(socket.getfqdn(socket.gethostname()))
        self.port = serverPort
        self.serverName = "Protruly RTSP Server"
        self.mediaFile = mediaFile

        self.state = self.INIT
        self.session = None
        self.videoStream = None
        self.rtpSocket = None
        self.rtpPort = None
        self.clientRtpPort = None
        self.event = None
        self.senderThread = None
        self._recvThread = None
        self._closed = False

    @property
    def _conn(self):
        return self.clientInfo["rtspSocket"][0]

    def run(self):
        self._recvThread = threading.Thread(
            target=self.recvRtspRequest, name="rtsp-recv", daemon=True
        )
        self._recvThread.start()

    def recvRtspRequest(self):
        """接收 RTSP 请求；对端断开后退出并释放资源。"""
        parser = RtspRequestParser()
        while not self._closed:
            try:
                data = self._conn.recv(4096)
            except OSError as exc:
                print("recv exception:", exc)
                break
            if not data:
                break
            try:
                requests = parser.feed(data)
            except ValueError as exc:
                print("bad RTSP request:", exc)
                self._reply(400, "Bad Request")
                break
            for request in requests:
                self.processRtspRequest(request)
        self.close()

    def processRtspRequest(self, request):
        method = request.method
        session = request.headers.get("SESSION", "").split(";")[0].strip()
        if (self.session is not None and session and session != str(self.session)
                and method not in (self.OPTIONS, self.DESCRIBE, self.SETUP)):
            self._reply(454, "Session Not Found", request)
            return

        if method == self.OPTIONS:
            self._reply(200, "OK", request, {
                "Public": "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER"
            })
        elif method == self.DESCRIBE:
            self._doDescribe(request)
        elif method == self.SETUP:
            self._doSetup(request)
        elif method == self.PLAY:
            self._doPlay(request)
        elif method == self.PAUSE:
            self._doPause(request)
        elif method == self.TEARDOWN:
            self._doTeardown(request)
        elif method == self.GET_PARAMETER:
            self._reply(200, "OK", request)
        else:
            self._reply(501, "Not Implemented", request)

    def _doDescribe(self, request):
        if self.videoStream is None:
            try:
                self.videoStream = VideoStream(self.mediaFile)
            except (OSError, ValueError) as exc:
                print("DESCRIBE media error:", exc)
                self._reply(500, "Internal Server Error", request)
                return
            self.session = randint(100000, 999999)
        sdp = self.videoStream.get_sdp(self.ip)
        self._reply(200, "OK", request, {
            "Content-Base": "rtsp://%s:%d/" % (self.ip, self.port),
            "Content-Type": "application/sdp",
        }, sdp.encode("utf-8"))

    def _doSetup(self, request):
        if self.state not in (self.INIT, self.READY):
            self._reply(455, "Method Not Valid in This State", request)
            return
        transport = request.headers.get("TRANSPORT", "")
        if "interleaved=" in transport.lower() or "RTP/AVP/TCP" in transport.upper():
            self._reply(461, "Unsupported Transport", request)
            return
        ports = parse_client_port(transport)
        if ports is None:
            self._reply(461, "Unsupported Transport", request)
            return
        if self.rtpSocket is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind(("", 0))
            except OSError as exc:
                print("UDP bind failed:", exc)
                sock.close()
                self._reply(500, "Internal Server Error", request)
                return
            self.rtpSocket = sock
            self.rtpPort = sock.getsockname()[1]
        client_port, client_port2 = ports
        self.clientRtpPort = client_port
        self.state = self.READY
        self._reply(200, "OK", request, {
            "Transport": "RTP/AVP;unicast;client_port=%d-%d;server_port=%d-%d" % (
                client_port, client_port2, self.rtpPort, self.rtpPort + 1
            )
        })

    def _doPlay(self, request):
        if self.state not in (self.READY, self.PLAYING) or self.videoStream is None:
            self._reply(455, "Method Not Valid in This State", request)
            return
        self.state = self.PLAYING
        self.event = threading.Event()
        self.senderThread = threading.Thread(
            target=self.sendRtp, name="rtp-send", daemon=True
        )
        self.senderThread.start()
        self._reply(200, "OK", request)

    def _doPause(self, request):
        if self.state != self.PLAYING:
            self._reply(455, "Method Not Valid in This State", request)
            return
        self.state = self.READY
        if self.event:
            self.event.set()
        self._reply(200, "OK", request)

    def _doTeardown(self, request):
        self.state = self.INIT
        if self.event:
            self.event.set()
        self._reply(200, "OK", request)
        self.close()

    def _reply(self, code, reason, request=None, extraHeaders=None, body=None):
        reply = "RTSP/1.0 %d %s\r\n" % (code, reason)
        reply += "Server: %s\r\n" % self.serverName
        if request is not None:
            reply += "CSeq: %s\r\n" % request.headers.get("CSEQ", "0")
        if self.session is not None and code != 454:
            reply += "Session: %d\r\n" % self.session
        for key, value in (extraHeaders or {}).items():
            reply += "%s: %s\r\n" % (key, value)
        reply += "Content-Length: %d\r\n\r\n" % (len(body) if body else 0)
        data = reply.encode("utf-8") + (body or b"")
        try:
            self._conn.sendall(data)
        except OSError as exc:
            print("send failed:", exc)
            self.close()

    def sendRtp(self):
        """按 RTP timestamp 间隔发帧；播完自动重头，seq/ts 保持连续。"""
        event = self.event
        stream = self.videoStream
        sock = self.rtpSocket
        if event is None or stream is None or sock is None:
            return
        addr = (self.clientInfo["rtspSocket"][1][0], self.clientRtpPort)
        seq = randint(0, 65535)
        ts = randint(0, 0xffffffff)
        ssrc = randint(1, 0xffffffff)
        prev_ts = None
        deadline = time.monotonic()

        while not event.is_set():
            frame = stream.next_frame()
            if frame is None:
                stream.rewind()
                frame = stream.next_frame()
                if frame is None:
                    break
                prev_ts = None
            frame_ts, payloads = frame
            if prev_ts is not None:
                delta = (frame_ts - prev_ts) & 0xffffffff
                now = time.monotonic()
                if deadline < now:
                    deadline = now
                deadline += delta / CLOCK_RATE
                if event.wait(max(0.0, deadline - time.monotonic())):
                    break
            else:
                delta = 0

            for index, payload in enumerate(payloads):
                marker = 1 if index == len(payloads) - 1 else 0
                packet = struct.pack(">BBHII", 0x80, 96 | (marker << 7), seq, ts, ssrc) + payload
                try:
                    sock.sendto(packet, addr)
                except OSError as exc:
                    print("RTP send error:", exc)
                    return
                seq = (seq + 1) & 0xffff
            ts = (ts + delta) & 0xffffffff
            prev_ts = frame_ts

    def close(self):
        """幂等释放：停发送线程、关 UDP socket 和 RTSP 连接。"""
        if self._closed:
            return
        self._closed = True
        if self.event is not None:
            self.event.set()
        thread = self.senderThread
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=0.5)
        if self.rtpSocket is not None:
            try:
                self.rtpSocket.close()
            except OSError:
                pass
            self.rtpSocket = None
        try:
            self._conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._conn.close()
        except OSError:
            pass


if __name__ == "__main__":
    for value in (
        "RTP/AVP;unicast;client_port=59088-59089",
        "RTP/AVP/UDP;unicast;mode=PLAY;client_port=5000-5001",
        "RTP/AVP;unicast;client_port=6000",
    ):
        print(value, "->", parse_client_port(value))
