import threading
import time
from RUDPReceiver import RUDPReceiverDispatcher
from RUDPSender import RUDPMultiConnectionManager, RUDPSender, cwnd_start, ssthresh_start
from common import RUDPConnection, TCPPacket, TCPListener, TCPPacketFlags, RetransmitTimer, RTTEstimator, log, MSS
import socket
from common import FakeTask
import FDFTPsocket
from FileReader import FileSegmentReader

class RUDPServerSender(RUDPConnection):
    def __init__(self, socket) -> None:
        super().__init__()
        self.seq = 0
        self.ackseq = 0
        self.status = 'CLOSED'
        self.socket = socket
        self.udpTask = FakeTask()
        self.retransmitTimer = RetransmitTimer(self)

        self.file_name = None
        self.file_size = None
        self.expected_md5 = None
        self.file_start_seq = 0 # seq of the first byte of the file
        self.fp = None

        self.lastByteRcvd = 0
        self.lastByteRead = 0

        # Format: tuple of (start_seq, len)
        self.out_of_order_info = []

        # Sender session info
        self.segments = None
        self.md5 = None
        self.sender_port = None
        self.sender_filename = None
    
    def close(self, reason):
        print('Closing connection due to', reason)
        self.status = 'CLOSED'
        if self.fp:
            self.fp.close()
            self.fp = None
        self.lastByteRcvd = 0
        self.lastByteRead = 0
        self.file_start_seq = 0
        self.out_of_order_info = []
    
    def handler(self, data, client_addr):
        packet = TCPPacket.decode(data)
        self.retransmitTimer.cancelAll(packet.ackseq)

        def send_syn_ack():
            self.ackseq = packet.seq + 1
            self.seq = 0
            ret_packet = TCPPacket(1000, TCPPacketFlags.SYN | TCPPacketFlags.ACK, self.seq, self.ackseq, b'')
            self.udpTask.sendto(self.socket, ret_packet.encode(), client_addr)
            self.seq += 1

        log(15, "Received seq", packet.seq, "expecting", self.ackseq)
        if self.status == 'CLOSED':
            # Expect: SYN packets from client
            if packet.flags & TCPPacketFlags.SYN:
                self.status = 'SYN_RCVD'
                send_syn_ack()
                log(5, 'Handshake with', client_addr)
            else:
                print('Unexpected packet in CLOSED state')
        elif self.status == 'SYN_RCVD':
            # Expect: ACK packet from client
            if packet.flags & TCPPacketFlags.ACK:
                self.status = 'ESTABLISHED'
                print('Connection established')
                self.file_start_seq = packet.seq + 1
                self.ackseq = packet.seq + 1
            else:
                # ACK or SYN-ACK lost, resend SYN/ACK
                send_syn_ack()
                print('Unexpected packet in SYN_RCVD state, SYN/ACK resent')
        elif self.status == 'ESTABLISHED':
            if packet.flags & TCPPacketFlags.FIN:
                # FIN received
                self.status = 'FIN_WAIT_1'
                ret_packet = TCPPacket(1000, TCPPacketFlags.ACK | TCPPacketFlags.FIN, self.seq, packet.seq + 1, b'')
                self.udpTask.sendto(self.socket, ret_packet.encode(), client_addr)
                self.seq += 1
                self.close('FIN received')
                # TODO: handle FIN
                return
            elif packet.flags & TCPPacketFlags.ACK:
                # Probably an ACK for a retransmitted packet
                log(10, 'ACK in ESTABLISHED state, probably from retransmission')
                return
    
    def wait_established(self):
        client_addr = None
        while self.status != 'ESTABLISHED':
            data, client_addr = self.socket.recvfrom(1500)
            self.handler(data, client_addr)
        return client_addr
        
    def timeoutHandler(self, packet, client_addr):
        log(5, 'Retransmitting packet', packet.seq, 'to', client_addr)


class Handshaker(RUDPConnection):
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
    
    def handler(self, data, client_addr):
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

        self.ackseq = packet.seq + 1
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

    def timeoutHandler(self, packet, client_addr):
        pass

    def handshakeListener(self):
        while self.status != 'ESTABLISHED':
            data, client_addr = self.socket.recvfrom(1500)
            self.handler(data, client_addr)

    def connect(self, server_addr, server_port):
        assert self.status == 'CLOSED'
        self.addr = (server_addr, server_port)
        # Start Listener
        threading.Thread(target=self.handshakeListener, daemon=True).start()
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


def server_send(s, file_name, segments):
    svrSender = RUDPServerSender(s)
    print('Waiting for data connection...')
    client_addr = svrSender.wait_established()
    udpTask = FDFTPsocket.Task(file_path=file_name)
    conn = RUDPMultiConnectionManager(udpTask=udpTask, addr=(client_addr[0], client_addr[1]), socket=s)
    conn.setup()
    conn.send(segments)