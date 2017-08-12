fakeRtspServer is a simple rtsp server based on https://github.com/grantfree035/videoStreaming 
the last10.dat 's format is 

0xaa 0x55 lenhigh lenlow  |  rtp 

the program will read the file and send it to client.

this program's function is to replay the rtp flow. maybe the rtp packets can be send by winpcap's developer package, it can send the 
rtp just as the original time diff.
