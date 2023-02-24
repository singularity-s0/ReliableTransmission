import threading
import time
from RUDPReceiver import RUDPReceiverDispatcher
from RUDPSender import RUDPMultiConnectionManager, RUDPSender
from RUDPServerSender import Handshaker
from common import RUDPConnection, TCPPacket, TCPListener, TCPPacketFlags, RetransmitTimer, RTTEstimator, log, MSS
import socket
from common import FakeTask
import FDFTPsocket
from FileReader import FileSegmentReader

cwnd_start = 1 * MSS
ssthresh_start = 5 * MSS

class Client(RUDPConnection):
    def __init__(self, s=None) -> None:
        super().__init__()
        self.seq = 0
        self.ackseq = 0
        self.status = 'CLOSED'
        self.connStateChanged = threading.Event()
        self.udpTask = FakeTask()
        self.socket = s
        if not self.socket:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listener = None
        self.retransmitTimer = RetransmitTimer(self)
        self.addr = None
        self.lastByteSent = 0
        self.lastByteAcked = 0
        self.ssthresh = ssthresh_start
        self.cwnd = cwnd_start
        self.dupAckCount = 0
        self.congestionState = 'slow_start'
        self.rttEstimator = RTTEstimator()
        self.congestion_state_updated = False

        self.reply = ""
        self.replyEvent = threading.Event()
    
    async def handler(self, data, client_addr):
        packet = TCPPacket.decode(data)
        try:
            a_seq = int(packet.payload.decode('utf-8'))
            if a_seq in self.retransmitTimer.timers:
                self.retransmitTimer.cancel(a_seq)
        except:
            pass
        if packet.ackseq > self.lastByteAcked:
            self.retransmitTimer.cancelAll(packet.ackseq)
            self.lastByteAcked = packet.ackseq
            self.congestion_state_updated = False
            # Update RTT
            prev_ts = self.retransmitTimer.send_timestamps.get(packet.ackseq)
            if prev_ts:
                try:
                    del self.retransmitTimer.send_timestamps[packet.ackseq]
                except KeyError:
                    pass
                self.rttEstimator.update(time.time() - prev_ts)
                self.retransmitTimer.timeout = self.rttEstimator.get() * 1.1
            self.dupAckCount = 0
            self.reply = packet.payload.decode('utf-8')
            self.replyEvent.set()
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
        log(20, "Received ackseq", packet.ackseq, "lastByteAcked", self.lastByteAcked)

        def on_receive_syn_ack():
            self.seq = 1
            retpacket = TCPPacket(1000, TCPPacketFlags.ACK, self.seq, self.ackseq, b'')
            self.seq += 1
            # ACK does not need retransmit
            self.udpTask.sendto(self.socket, retpacket.encode(), client_addr)
            print('Connection established')
            self.status = 'ESTABLISHED'
            self.connStateChanged.set()
            self.ackseq = packet.seq + 1

        if self.status == 'CLOSED':
            # Expect: Nothing, client should not receive packet in CLOSED state
            print('Unexpected packet in CLOSED state')
        elif self.status == 'SYN_SENT':
            # Expect: SYN-ACK packet
            if packet.flags & TCPPacketFlags.SYN and packet.flags & TCPPacketFlags.ACK:
                on_receive_syn_ack()
            else:
                print('Unexpected packet in SYN_SENT state')
        elif self.status == 'ESTABLISHED':
            # Expect: ACK packets from server
            # or SYN-ACK packet from server, in case ACK packet is lost
            if packet.flags & TCPPacketFlags.ACK and packet.flags & TCPPacketFlags.SYN:
                on_receive_syn_ack()
                # TODO: should reset data sending
                self.reset_data_sending = True
                return
            elif packet.flags & TCPPacketFlags.ACK and packet.flags & TCPPacketFlags.FIN:
                log(5, 'Connection closed')
                self.status = 'CLOSED'
                self.connStateChanged.set()
    
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
        if not self.congestion_state_updated:
            self.ssthresh = self.cwnd / 2
            self.cwnd = cwnd_start
            self.congestionState = 'slow_start'
            #self.rttEstimator.update(self.rttEstimator.get() * 1.5)
            self.congestion_state_updated = True

    def connect(self, server_addr, server_port):
        print("Attempting handshake with Server...")
        assert self.status == 'CLOSED'
        self.addr = (server_addr, server_port)
        # Start Listener
        self.listener = TCPListener(self.socket, self.handler)
        self.listener.open()
        # Send SYN
        packet = TCPPacket(1000, TCPPacketFlags.SYN, self.seq, 0, b'')
        self.status = 'SYN_SENT'
        self.connStateChanged.set()
        self.retransmitTimer.sendto(packet=packet, client_addr=(server_addr, server_port))
        self.seq += 1

        # Wait until ESTABLISHED
        while self.status != 'ESTABLISHED':
            self.connStateChanged.wait()
            self.connStateChanged.clear()

    def sendCommand(self, command):
        assert self.status == 'ESTABLISHED'
        data = command.encode('utf-8')
        packet = TCPPacket(1000, TCPPacketFlags.COMMAND, self.seq, self.ackseq, data)
        self.retransmitTimer.sendto(packet=packet, client_addr=self.addr)
        self.seq += len(data)
        self.lastByteSent = self.seq
        self.retransmitTimer.wait_until_all_acked()
        self.replyEvent.wait()
        self.replyEvent.clear()
        return self.reply
    
    def sendFile(self, file_name, segment_count):
        assert self.status == 'ESTABLISHED'
        reader = FileSegmentReader(file_name, segment_count=segment_count)
        segments, md5 = reader.read()
        reply = self.sendCommand(f"PUT\n{file_name}\n{reader.file_size}\n{md5}\n{reader.segment_size}\n{len(segments)}")
        status, *args = reply.split('\n')
        ftp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ftp_socket.bind(('', 0))
        port = ftp_socket.getsockname()[1]
        if status == 'READY':
            ftp_port = int(args[0])
            print(f'Server is ready to receive file on port {ftp_port}')
            udpTask = FDFTPsocket.Task(file_path=file_name)
            conn = RUDPMultiConnectionManager(udpTask=udpTask, addr=(self.addr[0], ftp_port), socket=ftp_socket)
            conn.setup()
            conn.send(segments)
            exit(0)
        else:
            print(f'Server error {reply}, please try again')
    
    def getFile(self, file_name, segment_count):
        assert self.status == 'ESTABLISHED'
        ftp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ftp_socket.bind(('', 0))
        port = ftp_socket.getsockname()[1]
        reply = self.sendCommand(f"GET\n{file_name}\n{segment_count}")
        status, *args = reply.split('\n')
        if status == 'READY':
            file_name = args[0]
            file_size = int(args[1])
            md5 = args[2]
            segment_size = int(args[3])
            segment_count = int(args[4])
            port = int(args[5])
            print(f'Server is ready to send {file_name}, expecting MD5 {md5}')
            # Start handshake with server with ftp_socket
            handshaker = Handshaker(ftp_socket)
            handshaker.connect(self.addr[0], port)
            # Spawn RUDPReceiver
            server = RUDPReceiverDispatcher(ftp_socket, file_name, file_size, md5, segment_count, segment_size)
            threading.Thread(target=server.listen).start()
        else:
            print(f'Server: {reply}')