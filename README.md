# fakeRtspServer

A simple H.264 RTSP server (Python 3) based on https://github.com/grantfree035/videoStreaming .
It replays a pre-framed RTP stream over RTSP/RTP-UDP, looping when the end is reached.

## Data format

`video.dat` is a sequence of records:

```
0xAA 0x55 | uint16_be length | RTP packet (12-byte header + H.264 payload)
```

The server reads the file and sends the RTP packets to the client.

## Run

```
python Server.py 8554
python Server.py 8554 --media other.dat
```

Default media file is `video.dat` (1920x1080, 30fps, 10s, x264 High profile).

## Make your own data

```
python make_dat.py input.264 output.dat --fps 30 --mtu 1400
```

The input must be an Annex B H.264 elementary stream. The converter splits access
units, fragments large NALs into FU-A packets, and assigns RTP timestamps.

## Test

```
python -m unittest -v test_fake_rtsp
```
