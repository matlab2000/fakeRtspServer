"""H.264/RTP 媒体源。

数据文件格式：每条记录为 ``0xAA 0x55 + uint16_be 长度 + RTP 包``。
本模块只负责解析、生成 SDP 和按帧输出 RTP payload，不依赖第三方库。
"""

import base64
import struct

MAGIC = b"\xaa\x55"
CLOCK_RATE = 90000
HIGH_PROFILES = frozenset((100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135))


class BitReader:
    """按位读取 RBSP，解析 Exp-Golomb 时用。越界立即报错，避免死循环。"""

    def __init__(self, data):
        self._data = data
        self._pos = 0

    @property
    def bits_left(self):
        return len(self._data) * 8 - self._pos

    def read(self, n):
        if n < 0 or n > 32 or self._pos + n > len(self._data) * 8:
            raise ValueError("SPS truncated at bit %d (need %d)" % (self._pos, n))
        value = 0
        for _ in range(n):
            byte = self._data[self._pos >> 3]
            value = (value << 1) | ((byte >> (7 - (self._pos & 7))) & 1)
            self._pos += 1
        return value

    def ue(self):
        zeros = 0
        while True:
            if self.bits_left < 1:
                raise ValueError("SPS truncated while reading ue(v)")
            if self.read(1):
                break
            zeros += 1
            if zeros > 31:
                raise ValueError("invalid ue(v): too many leading zeros")
        return (1 << zeros) - 1 + (self.read(zeros) if zeros else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def ebsp_to_rbsp(ebsp):
    """去掉 H.264 emulation prevention byte (0x03)。"""
    out = bytearray()
    i = 0
    while i < len(ebsp):
        if i + 2 < len(ebsp) and ebsp[i] == 0 and ebsp[i + 1] == 0 and ebsp[i + 2] == 3:
            out += ebsp[i:i + 2]
            i += 3
        else:
            out.append(ebsp[i])
            i += 1
    return bytes(out)


def _skip_scaling_list(br, size):
    last, nxt = 8, 8
    for _ in range(size):
        if nxt != 0:
            nxt = (last + br.se() + 256) % 256
        last = nxt if nxt else last


def parse_sps(nal):
    """从 SPS NAL 解析出 SDP 需要的字段（profile/level/分辨率）。"""
    if len(nal) < 4 or (nal[0] & 0x1f) != 7:
        raise ValueError("not an SPS NAL")
    br = BitReader(ebsp_to_rbsp(nal[1:]))
    info = {"nal_unit_type": 7}
    info["profile_idc"] = br.read(8)
    info["profile_compatibility"] = br.read(8)
    info["level_idc"] = br.read(8)
    br.ue()  # seq_parameter_set_id

    chroma = 1
    if info["profile_idc"] in HIGH_PROFILES:
        chroma = br.ue()
        if chroma == 3:
            br.read(1)  # separate_colour_plane_flag
        br.ue()  # bit_depth_luma_minus8
        br.ue()  # bit_depth_chroma_minus8
        br.read(1)  # qpprime_y_zero_transform_bypass_flag
        if br.read(1):  # seq_scaling_matrix_present_flag
            for i in range(8 if chroma != 3 else 12):
                if br.read(1):
                    _skip_scaling_list(br, 16 if i < 6 else 64)

    br.ue()  # log2_max_frame_num_minus4
    pic_order_cnt_type = br.ue()
    if pic_order_cnt_type == 0:
        br.ue()  # log2_max_pic_order_cnt_lsb_minus4
    elif pic_order_cnt_type == 1:
        br.read(1)  # delta_pic_order_always_zero_flag
        br.se()  # offset_for_non_ref_pic
        br.se()  # offset_for_top_to_bottom_field
        cycle = br.ue()
        for _ in range(cycle):
            br.se()  # offset_for_ref_frame

    br.ue()  # max_num_ref_frames
    br.read(1)  # gaps_in_frame_num_value_allowed_flag
    width_mbs = br.ue() + 1
    height_mbs = br.ue() + 1
    frame_mbs_only = br.read(1)
    if not frame_mbs_only:
        br.read(1)  # mb_adaptive_frame_field_flag
    br.read(1)  # direct_8x8_inference_flag

    crop = (0, 0, 0, 0)
    if br.read(1):  # frame_cropping_flag
        crop = tuple(br.ue() for _ in range(4))

    sub_w, sub_h = {0: (1, 1), 1: (2, 2), 2: (2, 1), 3: (1, 1)}.get(chroma, (2, 2))
    crop_x = sub_w
    crop_y = sub_h * (2 - frame_mbs_only)
    info["chroma_format_idc"] = chroma
    info["frame_mbs_only_flag"] = frame_mbs_only
    info["width"] = width_mbs * 16 - crop_x * (crop[0] + crop[1])
    info["height"] = height_mbs * 16 * (2 - frame_mbs_only) - crop_y * (crop[2] + crop[3])
    return info


def parse_pps(nal):
    """解析 PPS 头部字段；SDP 只需要原始 NAL，这里用于完整性校验。"""
    if len(nal) < 4 or (nal[0] & 0x1f) != 8:
        raise ValueError("not a PPS NAL")
    br = BitReader(ebsp_to_rbsp(nal[1:]))
    return {
        "nal_unit_type": 8,
        "pic_parameter_set_id": br.ue(),
        "seq_parameter_set_id": br.ue(),
        "entropy_coding_mode_flag": br.read(1),
    }


def rtp_payload(packet):
    """剥掉 RTP 头（支持 CSRC 和扩展头），返回 payload。"""
    if len(packet) < 12:
        raise ValueError("RTP packet too short")
    b0 = packet[0]
    if (b0 >> 6) != 2:
        raise ValueError("unsupported RTP version")
    offset = 12 + 4 * (b0 & 0x0f)
    if b0 & 0x10:  # extension
        if len(packet) < offset + 4:
            raise ValueError("RTP extension truncated")
        offset += 4 + 4 * struct.unpack(">H", packet[offset + 2:offset + 4])[0]
    end = len(packet)
    if b0 & 0x20:  # padding
        end -= packet[-1]
    if offset >= end:
        raise ValueError("RTP payload empty")
    return packet[offset:end]


class VideoStream:
    """把封帧文件解析成帧列表，并提供 SDP。"""

    def __init__(self, filename="video.dat"):
        self.filename = filename
        self.frames = []  # [(rtp_timestamp, [payload, ...]), ...]
        self.sps = None
        self.pps = None
        self.sps_info = None
        self.pps_info = None
        self.sdp = None
        self.duration = 0.0
        self.bitrate_kbps = 0
        self._cur_idx = 0
        self._payload_bytes = 0
        self._load()

    def _load(self):
        with open(self.filename, "rb") as f:
            self._load_from_bytes(f.read())

    def _load_from_bytes(self, data):
        self.frames = []
        self.sps = self.pps = self.sps_info = self.pps_info = self.sdp = None
        self.duration = 0.0
        self.bitrate_kbps = 0
        self._cur_idx = 0
        self._payload_bytes = 0
        idx, total = 0, len(data)
        while idx < total:
            if total - idx < 4:
                raise ValueError("truncated record header at offset %d" % idx)
            if data[idx:idx + 2] != MAGIC:
                raise ValueError("bad magic at offset %d: %s" % (idx, data[idx:idx + 2].hex()))
            length = struct.unpack(">H", data[idx + 2:idx + 4])[0]
            end = idx + 4 + length
            if end > total:
                raise ValueError("truncated record payload at offset %d (length %d)" % (idx, length))
            packet = data[idx + 4:end]
            try:
                ts = struct.unpack(">I", packet[4:8])[0]
                payload = rtp_payload(packet)
            except (ValueError, struct.error) as exc:
                raise ValueError("bad RTP packet at offset %d: %s" % (idx + 4, exc)) from exc

            self._payload_bytes += len(payload)
            if self.frames and self.frames[-1][0] == ts:
                self.frames[-1][1].append(payload)
            else:
                self.frames.append((ts, [payload]))

            ntype = payload[0] & 0x1f
            if ntype == 7 and self.sps is None:
                self.sps = payload
                self.sps_info = parse_sps(payload)
            elif ntype == 8 and self.pps is None:
                self.pps = payload
                self.pps_info = parse_pps(payload)
            idx = end

        if len(self.frames) > 1:
            span = 0
            for i in range(1, len(self.frames)):
                span += (self.frames[i][0] - self.frames[i - 1][0]) & 0xffffffff
            self.duration = span / CLOCK_RATE
            if self.duration > 0:
                self.bitrate_kbps = int(self._payload_bytes * 8 / self.duration / 1000) + 1

    @property
    def frame_count(self):
        return len(self.frames)

    def next_frame(self):
        """返回 (timestamp, [payload, ...])；到末尾返回 None。"""
        if not (0 <= self._cur_idx < len(self.frames)):
            return None
        frame = self.frames[self._cur_idx]
        self._cur_idx += 1
        return frame

    def rewind(self):
        self._cur_idx = 0

    def get_sdp(self, ip="0.0.0.0"):
        if self.sps is None or self.pps is None or self.sps_info is None:
            raise RuntimeError("SPS/PPS not found in media file")
        info = self.sps_info
        sps_b64 = base64.b64encode(self.sps).decode("ascii")
        pps_b64 = base64.b64encode(self.pps).decode("ascii")
        sdp = "v=0\r\n"
        sdp += "o=- 0 0 IN IP4 %s\r\n" % ip
        sdp += "s=H264 Live Stream\r\n"
        sdp += "c=IN IP4 0.0.0.0\r\n"
        sdp += "t=0 0\r\n"
        sdp += "b=AS:%d\r\n" % max(self.bitrate_kbps, 1)
        sdp += "m=video 0 RTP/AVP 96\r\n"
        sdp += "a=rtpmap:96 H264/90000\r\n"
        sdp += "a=control:trackID=1\r\n"
        sdp += "a=fmtp:96 profile-level-id=%02x%02x%02x; packetization-mode=1; sprop-parameter-sets=%s,%s\r\n" % (
            info["profile_idc"], info["profile_compatibility"], info["level_idc"], sps_b64, pps_b64
        )
        sdp += "a=framesize:96 %d-%d\r\n" % (info["width"], info["height"])
        return sdp

    # 兼容旧调用名
    nextFrame = next_frame
    frameNbr = lambda self: self.frame_count
    getSdp = get_sdp


if __name__ == "__main__":
    import os
    v = VideoStream(os.path.join(os.path.dirname(__file__), "video.dat"))
    print("frames:", v.frame_count, "duration:", v.duration)
    print(v.get_sdp())
