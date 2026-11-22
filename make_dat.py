"""把 Annex B H.264 文件封装成 fakeRtspServer 的 .dat 格式。

用法：python make_dat.py video.264 [video.dat] [--fps 30] [--mtu 1400]

输出格式：每条记录为 0xAA55 + uint16_be(长度) + RTP 包(12 字节头 + H264 payload)。
NAL 按 access unit 分帧（同一帧共用 timestamp），大 NAL 切成 FU-A，
SPS/PPS/SEI 放在所在帧的最前面，便于 VideoStream 首包就能提取参数。
"""

import argparse
import os
import struct
from random import randint

VCL_TYPES = frozenset((1, 5))
FU_A = 28


def split_annex_b(data):
    """返回 [(nal_type, nal_bytes_with_header), ...]"""
    starts = []
    i = 0
    while i < len(data) - 3:
        if data[i:i + 4] == b"\x00\x00\x00\x01":
            starts.append((i, 4))
            i += 4
        elif data[i:i + 3] == b"\x00\x00\x01":
            starts.append((i, 3))
            i += 3
        else:
            i += 1
    if not starts:
        raise ValueError("no Annex B start code found")
    nals = []
    for index, (pos, prefix) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(data)
        nal = data[pos + prefix:end]
        # 去掉 Annex B 允许的尾部 trailing_zero 字节
        nal = nal.rstrip(b"\x00")
        if nal:
            nals.append((nal[0] & 0x1f, nal))
    return nals


def group_frames(nals):
    """按 VCL NAL 切分 access unit；非 VCL 归到紧随其后的 VCL 帧。"""
    frames = []
    pending = []
    for ntype, nal in nals:
        if ntype in VCL_TYPES:
            pending.append((ntype, nal))
            frames.append(pending)
            pending = []
        else:
            pending.append((ntype, nal))
    if pending:
        frames.append(pending)
    return frames


def fragment(nal, mtu):
    """大 NAL 切 FU-A；小 NAL 直接返回单包。"""
    if len(nal) <= mtu:
        return [nal]
    indicator = (nal[0] & 0xe0) | FU_A
    original_type = nal[0] & 0x1f
    body = nal[1:]
    chunks = []
    offset = 0
    # FU header 占 2 字节，每个分片 payload 最多 mtu
    while offset < len(body):
        part = body[offset:offset + mtu - 2]
        offset += len(part)
        chunks.append(part)
    packets = []
    for i, part in enumerate(chunks):
        fu_header = (0x80 if i == 0 else 0) | (0x40 if i == len(chunks) - 1 else 0) | original_type
        packets.append(bytes([indicator, fu_header]) + part)
    return packets


def convert(src, dst, fps, mtu):
    data = open(src, "rb").read()
    nals = split_annex_b(data)
    frames = group_frames(nals)
    if not frames:
        raise ValueError("no frames found")

    timestamp_step = int(round(90000.0 / fps))
    timestamp = randint(0, 0xffffffff)
    seq = randint(0, 65535)
    ssrc = randint(1, 0xffffffff)
    packet_count = 0

    with open(dst, "wb") as out:
        for frame in frames:
            payloads = []
            for ntype, nal in frame:
                payloads.extend(fragment(nal, mtu))
            for index, payload in enumerate(payloads):
                marker = 1 if index == len(payloads) - 1 else 0
                if len(payload) + 12 > 65535:
                    raise ValueError("RTP packet too large for uint16 length field")
                rtp = struct.pack(">BBHII", 0x80, 96 | (marker << 7), seq, timestamp, ssrc) + payload
                out.write(b"\xaa\x55")
                out.write(struct.pack(">H", len(rtp)))
                out.write(rtp)
                seq = (seq + 1) & 0xffff
                packet_count += 1
            timestamp = (timestamp + timestamp_step) & 0xffffffff
    return len(frames), packet_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", help="Annex B H.264 file")
    parser.add_argument("dst", nargs="?", default=None, help="output .dat file")
    parser.add_argument("--fps", type=float, default=30.0, help="frame rate (default 30)")
    parser.add_argument("--mtu", type=int, default=1400, help="max RTP payload size (default 1400)")
    args = parser.parse_args()
    dst = args.dst or os.path.splitext(args.src)[0] + ".dat"
    frames, packets = convert(args.src, dst, args.fps, args.mtu)
    print("%s -> %s" % (args.src, dst))
    print("frames: %d, RTP packets: %d, size: %d bytes" % (
        frames, packets, os.path.getsize(dst)))


if __name__ == "__main__":
    main()
