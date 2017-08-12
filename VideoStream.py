import os,sys
import struct
import binascii
import base64
import socket
from bitstring import BitArray, BitStream

def parseSps(s,h):
    #pic_order_cnt_type
    nal_unit_type=s.read('uint:8')&0x1f
    profile_idc=s.read('uint:8')
    h['profile_idc']=profile_idc
    s.read('uint:8')
    level_idc=s.read('uint:8')
    h['level_idc']=level_idc
    seq_parameter_set_id=s.read('ue')
    h['separate_colour_plane_flag']=0
    if  profile_idc in [100,110,122,244,44,83,86]:
        print "profile_idc:",profile_idc
        chroma_format_idc=s.read('ue')
        if chroma_format_idc == 3:
            separate_colour_plane_flag=s.read('uint:1')
            h['separate_colour_plane_flag']=separate_colour_plane_flag
        bit_depth_luma_minus8=s.read('ue')
        bit_depth_chroma_minus8 =s.read('ue')
        qpprime_y_zero_transform_bypass_flag=s.read('uint:1')
        seq_scaling_matrix_present_flag=s.read('uint:1')
        if seq_scaling_matrix_present_flag:
            seq_scaling_list_present_flag=[]
            for i in range( (chroma_format_idc != 3 and 8 or 12) ): #= 0; i < ( ( chroma_format_idc != 3 ) ? 8 : 12 ); i++ ) {
                seq_scaling_list_present_flag.append(s.read('uint:1'))
                if seq_scaling_list_present_flag[-1]:
                    if i < 6 :
                        pass #scaling_list( ScalingList4x4[ i ], 16,UseDefaultScalingMatrix4x4Flag[ i ])  0
                    else:
                        pass #scaling_list( ScalingList8x8[ i - 6 ], 64,UseDefaultScalingMatrix8x8Flag[ i - 6 ] ) 0

    log2_max_frame_num_minus4 =s.read('ue')
    h['log2_max_frame_num_minus4']=log2_max_frame_num_minus4
    pic_order_cnt_type =s.read('ue')
    h['pic_order_cnt_type']=pic_order_cnt_type
    #print "pic_order_cnt_type:",pic_order_cnt_type
    if pic_order_cnt_type == 0 :
        log2_max_pic_order_cnt_lsb_minus4 =s.read('ue')
        h['log2_max_pic_order_cnt_lsb_minus4']=log2_max_pic_order_cnt_lsb_minus4
        #print "log2_max_pic_order_cnt_lsb_minus4:",log2_max_pic_order_cnt_lsb_minus4
    elif pic_order_cnt_type == 1:
        delta_pic_order_always_zero_flag =s.read('uint:1')
        h['delta_pic_order_always_zero_flag']=delta_pic_order_always_zero_flag
        offset_for_non_ref_pic=s.read('se')
        offset_for_top_to_bottom_field=s.read('se')
        num_ref_frames_in_pic_order_cnt_cycle=s.read('ue')
        offset_for_ref_frame=[]
        for i in range(num_ref_frames_in_pic_order_cnt_cycle):
            offset_for_ref_frame.append(s.read('se'))
    
    num_ref_frames=s.read('ue')
    gaps_in_frame_num_value_allowed_flag=s.read('uint:1')
    pic_width_in_mbs_minus1=s.read('ue')
    pic_height_in_map_units_minus1=s.read('ue')
    frame_mbs_only_flag=s.read('uint:1')
    h['frame_mbs_only_flag']=frame_mbs_only_flag

def parsePps(s,h):
    nal_unit_type=s.read('uint:8')&0x1f
    pic_parameter_set_id=s.read('ue')
    seq_parameter_set_id=s.read('ue')
    entropy_coding_mode_flag=s.read('uint:1')
    pic_order_present_flag =s.read('uint:1')
    h['pic_order_present_flag']=pic_order_present_flag
    num_slice_groups_minus1=s.read('ue') 


class VideoStream:
	def __init__(self, filename='last10.dat'):
		self.ip=socket.gethostbyname(socket.getfqdn(socket.gethostname()))
		self.sps=None 
		self.pps=None
		self.sdp=None
		self.h={}
		self.profile_level_id=None

		self.filename = filename
		self.pkts=[] #data,ts
		self.frames=[] #[ts,[pkt,...]]
		#try:
		if True:
			data = open(filename, 'rb').read()
			dataLen=len(data)
			idx=0
			while idx<dataLen:
				a,b,blen=struct.unpack('>BBH',data[idx:idx+4])
				if a==0xaa and b==0x55:
					idx+=4
					d=data[idx:idx+blen]
					ts = struct.unpack(">I",d[4:8])
					self.pkts.append(d)
					if len(self.frames)==0:
						self.frames.append([ts,[d]])
					else:
						frame=self.frames[-1]
						if frame[0]==ts:
							frame[1].append(d)
						else:
							self.frames.append([ts,[d]])
					if self.sps==None or self.pps==None:
						rtp=d[12:]
						ntype=(struct.unpack('B',rtp[0])[0])&0x1f
						if ntype==7:
							self.sps=rtp
							s=BitStream(bytes=self.sps)
							parseSps(s,self.h)
							self.profile_level_id="%02x%02x%02x"%(self.h['profile_idc'],0,self.h['level_idc'])
						if ntype==8:
							self.pps=rtp

					
					idx+=blen			
		#except Exception,e:
		#	print "IOError:",e
		#	raise IOError

		self.frameNum = len(self.frames)
		self.curIdx=0
		
	def nextFrame(self):
		if self.curIdx>=self.frameNum or self.curIdx<0:
			return None
		frame=self.frames[self.curIdx]
		self.curIdx+=1
		return frame 

	def frameNbr(self):
		"""Get frame number."""
		return self.frameNum

	def getSdp(self):	
		if self.sdp==None:
			sdp="v=0\r\n"
			sdp+="o=xueys 20082 3450082 IN IP4 %s\r\n"%(self.ip)
			sdp+="s=ys.xue\r\n"
			sdp+="c=IN IP4 0.0.0.0\r\n"
			sdp+="t=0 0\r\n"
			sdp+="b=AS:441\r\n"
			sdp+="m=video 0 RTP/AVP 96\r\n"
			sdp+="a=rtpmap:96 H264/90000\r\n"
			sdp+="a=control:trackID=1\r\n"
			sdp+="a=fmtp:96 profile-level-id=%s; packetization-mode=1; sprop-parameter-sets=%s,%s\r\n"%(self.profile_level_id,
				base64.b64encode(self.sps),base64.b64encode(self.pps))
			sdp+="a=framesize:96 448-256\r\n"
			self.sdp=sdp
		return self.sdp

	
if __name__ == "__main__":
	v=VideoStream()
	print v.frameNbr()
	print v.getSdp()
	'''
	pid='640015'
	sps='Z2QAFazZQcCGhAAAAwAEAAADAMg8WLZY'
	pps='aOvjyyLA'
	print binascii.hexlify(binascii.a2b_hex(pid))
	print base64.b64encode(base64.b64decode(sps))
	print binascii.hexlify(base64.b64decode(pps))
	'''