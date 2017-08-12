import re
from random import randint
import sys, traceback, threading, socket

from VideoStream import VideoStream



class ServerWorker:
	OPTIONS = 'OPTIONS'
	DESCRIBE = 'DESCRIBE'
	SETUP = 'SETUP'
	PLAY = 'PLAY'
	PAUSE = 'PAUSE'
	TEARDOWN = 'TEARDOWN'

	INIT = 0
	READY = 1
	PLAYING = 2
	state = INIT

	OK_200 = 0
	FILE_NOT_FOUND_404 = 1
	CON_ERR_500 = 2

	clientInfo = {}

	def __init__(self, clientInfo,serverPort=554):
		self.clientInfo = clientInfo
		self.ip=socket.gethostbyname(socket.getfqdn(socket.gethostname()))
		self.port=serverPort
		self.udpPort=-1
		self.serverName="Protruly RTSP Server"

	def run(self):
		threading.Thread(target=self.recvRtspRequest).start()

	def recvRtspRequest(self):
		"""Receive RTSP request from the client."""
		connSocket = self.clientInfo['rtspSocket'][0]
		while True:
			try:
				data = connSocket.recv(256)
				if data:
					print "Data received:\n" + data
					self.processRtspRequest(data)
			except Exception,e:
				print "recv exception:",e
				break

	def processRtspRequest(self, data):
		"""Process RTSP request sent from the client."""
		# Get the request type
		request = data.split('\n')
		#line1 = request[0].split(' ')
		#requestType = line1[0]
		requestType,url,ver=re.split(r'\s+',request[0].strip())
		# Get the media file name
		#filename = line1[1]
		hdr={}
		for r in request[1:]:
			if len(r.strip())==0:
				continue
			arr=r.split(":")
			if len(arr)==2:
				hdr[arr[0].strip().upper()]=arr[1].strip()
			else:
				print "not ok:",r,arr

		# Get the RTSP sequence number
		#Cseq=hdr['CSEQ']

		# Process SETUP request
		if requestType == self.OPTIONS:
			if self.state == self.INIT:
				print "processing OPTIONS\n"
				self.replyOption(self.OK_200, hdr)
		elif requestType == self.DESCRIBE:
			if self.state == self.INIT:
				print "processing DESCRIBE\n"
				# Generate a randomized RTSP session ID
				self.clientInfo['session'] = randint(100000, 999999)
				v=VideoStream()
				self.clientInfo['videoStream'] = v
				sdp=v.getSdp()
				self.replyDescribe(self.OK_200, hdr,sdp)    	
				print "processing DESCRIBE Done\n"	
		elif requestType == self.SETUP:
			if self.state == self.INIT:
				# Update state
				print "processing SETUP\n"
				self.state = self.READY
				s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
				s.bind(('',0))
				self.udpPort=s.getsockname()[1]
				self.clientInfo["rtpSocket"] = s 
				self.clientInfo['rtpPort'] = int(hdr['TRANSPORT'].split(';')[2].split('=')[1].split("-")[0])
				# Send RTSP reply
				self.replySetup(self.OK_200, hdr)
				print "processing SETUP Done\n"
		# Process PLAY request
		elif requestType == self.PLAY:
			if self.state == self.READY:
				print "processing PLAY\n"
				self.state = self.PLAYING

				# Create a new socket for RTP/UDP
				#self.clientInfo["rtpSocket"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

				self.replyPlay(self.OK_200, hdr)
				print "processing PLAY Done\n"
				# Create a new thread and start sending RTP packets
				self.clientInfo['event'] = threading.Event()
				self.clientInfo['worker']= threading.Thread(target=self.sendRtp)
				self.clientInfo['worker'].start()

		# Process PAUSE request
		elif requestType == self.PAUSE:
			if self.state == self.PLAYING:
				print "processing PAUSE\n"
				self.state = self.READY
				self.clientInfo['event'].set()
				self.replyPause(self.OK_200, hdr)
				print "processing PAUSE Done\n"
		# Process TEARDOWN request
		elif requestType == self.TEARDOWN:
			print "processing TEARDOWN\n"
			self.clientInfo['event'].set()
			self.replyTeardown(self.OK_200, hdr)
			print "processing TEARDOWN Done\n"
			# Close the RTP socket
			self.clientInfo['rtpSocket'].close()

	def sendRtp(self):
		"""Send RTP packets over UDP."""
		print "sendRtp start......"
		while True:
			self.clientInfo['event'].wait(0.04)
			# Stop sending if request is PAUSE or TEARDOWN
			if self.clientInfo['event'].isSet():
				break
			frame = self.clientInfo['videoStream'].nextFrame()
			if frame:
				try:
					for pkt in frame[1]:
						address = self.clientInfo['rtspSocket'][1][0]
						port = self.clientInfo['rtpPort']
						self.clientInfo['rtpSocket'].sendto(pkt,(address,port))
				except:
					print "Connection Error"
					#print '-'*60
					#traceback.print_exc(file=sys.stdout)
					#print '-'*60
			else:
				break

	def sendToClient(self,newhdr,data=None):
		reply=newhdr
		if data and len(data)>0:
			reply+='Content-Length: %d\r\n'%(len(data)+2)
			reply+='\r\n'
			reply+=data+"\r\n"	
		else:
			reply+='Content-Length: 0\r\n\r\n'
		#print "SendToClient [%s] by connect %s"%(reply,self.clientInfo['rtspSocket'])
		connSocket = self.clientInfo['rtspSocket'][0]

		ret=connSocket.send(reply) 
		print "sendToClient need send %s real send %s"%(len(reply),ret)

	def replyOption(self,code,hdr,data=None):
		"""Send RTSP reply to the client."""
		if code == self.OK_200:
			reply = 'RTSP/1.0 200 OK\r\n'
			reply += 'Server: %s\r\n'%(self.serverName)
			reply += 'CSeq: ' + hdr['CSEQ'] + '\r\n'		
			reply += 'Public: DESCRIBE,SETUP,TEARDOWN,PLAY,PAUSE\r\n'
			self.sendToClient(reply)
		else:
			pass

	def replyDescribe(self,code,hdr,sdp=None):
		"""Send RTSP reply to the client."""
		if code == self.OK_200:
			reply = 'RTSP/1.0 200 OK\r\n'
			reply += 'Server: %s\r\n'%(self.serverName)
			reply += 'CSeq: ' + hdr['CSEQ'] + '\r\n'
			reply += 'Content-Base: %s:%s\r\n'%(self.ip,self.port)
			reply += 'Content-type: application/sdp\r\n'
			reply += 'Session: ' + str(self.clientInfo['session']) + '\r\n'
			self.sendToClient(reply,sdp)
		else:
			pass

	def replySetup(self,code,hdr,data=None):
		"""Send RTSP reply to the client."""
		if code == self.OK_200:
			reply = 'RTSP/1.0 200 OK\r\n'
			reply += 'Server: %s\r\n'%(self.serverName)
			reply += 'CSeq: ' + hdr['CSEQ'] + '\r\n'
			reply += 'Session: ' + str(self.clientInfo['session']) + '\r\n'
			clientPort=self.clientInfo['rtpPort']
			port=self.udpPort
			reply += 'Transport: RTP/AVP;unicast;client_port=%s-%s;server_port=%d-%d\r\n'%(
				clientPort,clientPort+1,port,port+1
			)
			#reply += 'Cache-Control: no-cache\r\n'
			self.sendToClient(reply,data)
		else:
			pass

	def replyPlay(self,code,hdr,data=None):
    		"""Send RTSP reply to the client."""
		if code == self.OK_200:
			reply = 'RTSP/1.0 200 OK\r\n'
			reply += 'Server: %s\r\n'%(self.serverName)
			reply += 'CSeq: ' + hdr['CSEQ'] + '\r\n'
			#reply += ''
			reply += 'Session: ' + str(self.clientInfo['session']) + '\r\n'
			self.sendToClient(reply,data)
		else:
			pass

	def replyPause(self,code,hdr,data=None):
    		"""Send RTSP reply to the client."""
		if code == self.OK_200:
			reply = 'RTSP/1.0 200 OK\r\n'
			reply += 'Server: %s\r\n'%(self.serverName)
			reply += 'CSeq: ' + hdr['CSEQ'] + '\r\n'
			reply += 'Content-Base: 192.168.11.2:554\r\n'
			reply += 'Content-type: application/sdp\r\n'
			reply += 'Session: ' + str(self.clientInfo['session']) + '\r\n'
			self.sendToClient(reply,data)
		else:
			pass

	def replyTeardown(self,code,hdr,data=None):
    		"""Send RTSP reply to the client."""
		if code == self.OK_200:
			reply = 'RTSP/1.0 200 OK\r\n'
			reply += 'Server: %s\r\n'%(self.serverName)
			reply += 'CSeq: ' + hdr['CSEQ'] + '\r\n'
			reply += 'Content-Base: 192.168.11.2:554\r\n'
			reply += 'Content-type: application/sdp\r\n'
			reply += 'Session: ' + str(self.clientInfo['session']) + '\r\n'
			self.sendToClient(reply,data)
		else:
			pass

	def replyRtsp(self, code, hdr,data=None):
		"""Send RTSP reply to the client."""
		if code == self.OK_200:
			#print "200 OK"
			reply = 'RTSP/1.0 200 OK\r\n'
			reply += 'CSeq: ' + hdr['CSEQ'] + '\r\n'
			reply += 'Session: ' + str(self.clientInfo['session']) + '\r\n'
			self.sendToClient(reply,data)

		# Error messages
		elif code == self.FILE_NOT_FOUND_404:
			print "404 NOT FOUND"
		elif code == self.CON_ERR_500:
			print "500 CONNECTION ERROR"

if __name__ == "__main__":
	data='''SETUP 192.168.11.2:554/trackID=65536 RTSP/1.0 
CSeq: 3 
User-Agent: stream Play (LIVE555 Streaming Media v2016.11.28) 
Transport: RTP/AVP;unicast;client_port=59088-59089'''
	request = data.split('\n')
	hdr={}
	print "method:",re.split(r'\s+',request[0].strip())
	for r in request[1:]:
		arr=r.split(":")
		hdr[arr[0].strip().upper()]=arr[1].strip()
	print hdr['TRANSPORT'].split(';')[2].split('=')[1].split("-")[0]
