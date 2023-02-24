import asyncio
import threading
import time
from tqdm import tqdm
from common import RUDPConnection, TCPPacket, TCPListener, TCPPacketFlags, RetransmitTimer, RTTEstimator, log, MSS
import socket, io
import FDFTPsocket as FDFTPsocket

cwnd_start = 1 * MSS
ssthresh_start = 5 * MSS

class RUDPSender(RUDPConnection):
    def __init__(self, bytes: io.BytesIO, udpTask: FDFTPsocket.Task, stream_id, segment_start_offset, manager, s = None) -> None:
        self.segment_id = stream_id
        self.seq = segment_start_offset
        self.ackseq = 0
        self.status = 'CLOSED'
        self.connStateChanged = threading.Event()
        self.bytesStream = bytes
        self.udpTask = udpTask
        self.socket = s
        if not self.socket:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listener = None
        self.retransmitTimer = RetransmitTimer(self)
        self.addr = None

        self.lastByteSent = 0
        self.lastByteAcked = segment_start_offset
        self.ssthresh = ssthresh_start
        self.cwnd = cwnd_start
        self.dupAckCount = 0
        self.congestionState = 'slow_start'
        self.ackEvent = threading.Event()

        self.manager = manager
        self.rttEstimator = RTTEstimator()
    
    async def handler(self, packet, client_addr):
        if packet.flags & TCPPacketFlags.ACK and packet.flags & TCPPacketFlags.FIN:
            log(5, 'Connection closed')
            self.status = 'CLOSED'
            self.connStateChanged.set()
        elif packet.flags & TCPPacketFlags.ACK or packet.flags & TCPPacketFlags.SYN:
            # Retransmitted packet, just ack them
            retpacket = TCPPacket(self.segment_id, TCPPacketFlags.ACK, self.seq, self.ackseq, b'')
            self.udpTask.sendto(self.socket, retpacket.encode(), client_addr)
            return
        # Update RTT
        prev_ts = self.retransmitTimer.send_timestamps.get(packet.ackseq)
        if prev_ts:
            self.rttEstimator.update(time.time() - prev_ts)
            self.retransmitTimer.timeout = self.rttEstimator.get() + 0.2
        try:
            a_seq = int(packet.payload.decode('utf-8'))
            if a_seq in self.retransmitTimer.timers:
                self.retransmitTimer.cancel(a_seq)
        except:
            pass

        log(20, f"lastByteAcked {self.lastByteAcked}, cwnd {self.cwnd/MSS:.4f} ssthresh {self.ssthresh/MSS:.4f}")
        if packet.ackseq > self.lastByteAcked:
            self.manager.progressBar.update(packet.ackseq - self.lastByteAcked)
            self.retransmitTimer.cancelAll(packet.ackseq)
            self.lastByteAcked = packet.ackseq
            self.dupAckCount = 0
            self.ackEvent.set()
        else:
            self.dupAckCount += 1
            if self.dupAckCount == 3:
                try:
                    self.retransmitTimer.resend_immediately(self.lastByteAcked)
                except KeyError:
                    log(5, '3 dup acks: Packet not sent yet')
                    self.dupAckCount = 0
                    return
                self.ssthresh = self.cwnd / 2
                self.cwnd = self.ssthresh + 3 * MSS
                self.congestionState = 'fast_recovery'
        self.ackseq = packet.seq + 1
        
        self.updateCongestionControl()
        log(20, "Received ackseq", packet.ackseq, "lastByteAcked", self.lastByteAcked, "acking", packet.payload.decode('utf-8'))
    
    def updateCongestionControl(self):
        if self.congestionState == 'slow_start':
            self.cwnd += MSS
            if self.cwnd >= self.ssthresh:
                self.congestionState = 'congestion_avoidance'
        elif self.congestionState == 'congestion_avoidance':
            self.cwnd += MSS * MSS / self.cwnd
        elif self.congestionState == 'fast_recovery':
            self.cwnd = self.ssthresh
            self.congestionState = 'congestion_avoidance'
        else:
            raise Exception('Invalid congestion state')

    def timeoutHandler(self, packet, client_addr):
        log(10, "Timeout, retransmitting packet with seq", packet.seq)
        self.ssthresh = self.cwnd / 2
        self.cwnd = cwnd_start #max(cwnd_start, min(self.ssthresh, self.ssthresh / (2/(self.cwnd/MSS+0.0001)+0.0001)))
        self.congestionState = 'slow_start'
        #self.rttEstimator.update(self.rttEstimator.get() * 2)

    def send(self, server_addr):
        self.addr = server_addr
        # Send data
        while True:
            if self.lastByteSent < self.lastByteAcked + self.cwnd:
                self.ackEvent.clear()
                data = self.bytesStream.read(MSS)
                if not data:
                    break
                packet = TCPPacket(self.segment_id, 0, self.seq, self.ackseq, data)
                self.retransmitTimer.sendto(packet=packet, client_addr=self.addr)
                self.seq += len(data)
                self.lastByteSent = self.seq
            else:
                self.ackEvent.wait(timeout=2)
                self.ackEvent.clear()
        log(5, "Finished sending data, awaiting acks")
        self.retransmitTimer.wait_until_all_acked()
        finish_packet = TCPPacket(self.segment_id, TCPPacketFlags.FIN, self.seq, self.ackseq, b'')
        self.retransmitTimer.sendto(packet=finish_packet, client_addr=self.addr)
        while self.status != 'CLOSED':
            self.connStateChanged.wait()
            self.connStateChanged.clear()

class RUDPMultiConnectionManager:
    def __init__(self, udpTask, addr, socket):
        self.udpTask = udpTask
        self.threads = []
        self.senders = []
        self.addr = addr
        self.socket = socket

        self.progressBar = tqdm(total=int(self.udpTask.file_size), unit='B', unit_scale=True, desc=f"Sending")
        self.packets_lost = 0
        self.packets_sent = 0

    def setup(self):
        threading.Thread(target=self.listen, daemon=True).start()

    def listen(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            data, client_addr = self.socket.recvfrom(1500)
            loop.run_until_complete(self.onReceive(data, client_addr))
            
    def send(self, segments):
        # Spawn RUDPSenders
        for i in range(len(segments)):
            sender = RUDPSender(segments[i].data, udpTask=self.udpTask, segment_start_offset=segments[i].seq, stream_id=i, manager=self, s=self.socket)
            self.senders.append(sender)
            t = threading.Thread(target=sender.send, args=(self.addr,))
            t.start()
            self.threads.append(t)
        for t in self.threads:
            t.join()
        self.progressBar.close()
        self.udpTask.finish()
        print(f"Transfer Complete\nAverage Speed: {self.udpTask.file_size / (time.time()-self.udpTask.start_time) / 1000:.2f} KB/s\nAverage Loss: {self.packets_lost / self.packets_sent * 100:.2f}%\n")

    async def onReceive(self, data, addr):
        packet = TCPPacket.decode(data)
        stream_id = packet.stream_id
        await self.senders[stream_id].handler(packet, addr)