"""RTSP 服务入口：接受连接，为每个客户端启动一个 ServerWorker。"""

import argparse
import os
import socket
import sys

from ServerWorker import ServerWorker


def main(argv=None):
    parser = argparse.ArgumentParser(description="H.264 RTSP server (RTP/UDP)")
    parser.add_argument("port", type=int, help="RTSP TCP port, e.g. 8554")
    parser.add_argument("--media", default="video.dat", help="framed RTP data file")
    args = parser.parse_args(argv)
    if not 0 < args.port < 65536:
        parser.error("port must be between 1 and 65535")

    media = args.media
    if not os.path.isfile(media):
        media = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.media)

    workers = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("", args.port))
    server.listen(5)
    print("RTSP server listening on port %d (media: %s)" % (args.port, media))

    try:
        while True:
            conn, addr = server.accept()
            print("rtsp socket:", conn, addr)
            worker = ServerWorker(
                {"rtspSocket": (conn, addr)}, args.port, mediaFile=media
            )
            workers = [w for w in workers if not w._closed]
            workers.append(worker)
            worker.run()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        for worker in workers:
            worker.close()
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
