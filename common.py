import struct, threading
from enum import IntEnum
import time
import FDFTPsocket as FDFTPsocket
import asyncio

LOG_LEVEL = 0
struct_format = f'HHLL'
MSS = 1400

class TCPPacketFlags(IntEnum):
    SYN = 1
    ACK = 2
    FIN = 4
    COMMAND = 8


class TCPPacket:
    def __init__(self, stream_id: int, flags: int, seq, ackseq, payload: bytes) -> None:
        self.seq = seq
        self.ackseq = ackseq
        self.flags = flags
        self.payload = payload
        self.stream_id = stream_id
    
    def encode(self) -> bytes:
        assert len(self.payload) <= MSS
        # encode the packet into bytes
        header = struct.pack(struct_format, self.stream_id, self.flags, self.seq, self.ackseq)
        bytes = header + self.payload
        return bytes

    @staticmethod
    def size(payload_size = MSS) -> int:
        return struct.calcsize(struct_format) + payload_size

    @staticmethod
    def decode(bytes) -> 'TCPPacket':
        # decode the bytes into a TCPPacket
        stream_id, flags, seq, ackseq = struct.unpack(struct_format, bytes[:struct.calcsize(struct_format)])
        payload = bytes[struct.calcsize(struct_format):]
        return TCPPacket(stream_id, flags, seq, ackseq, payload)


class TCPListener:
    def __init__(self, socket, handler) -> None:
        self.socket = socket
        self.buffer_size = 1500
        self.handlers = [handler]

    def open(self, deamon=True) -> None:
        # start listener daemon
        self.listener = threading.Thread(target=self.listen, daemon=deamon)
        self.listener.start()
    
    def add_handler(self, handler):
        self.handlers.append(handler)
    
    def listen(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            data, client_addr = self.socket.recvfrom(self.buffer_size)
            for handler in self.handlers:
                loop.run_until_complete(handler(data=data, client_addr=client_addr))


class RUDPConnection:
    def __init__(self) -> None:
        self.socket = None
        self.udpTask = FakeTask()
        self.lastByteAcked = 0
        self.cwnd = 0
        self.manager = FakeManager()
    
    def handler(self, data, client_addr):
        raise NotImplementedError
    
    def timeoutHandler(self, packet, client_addr):
        # called when retransmission timer expires
        # packet will be automatically retransmitted
        # maybe reset cwnd, ssthresh, etc.
        raise NotImplementedError


# FDFTP Task Compatibility Layer
class FakeTask():
    def sendto(self,s,data,addr):
        s.sendto(data,addr)
        
    def finish(self):
        pass

class FakeManager():
    def __init__(self):
        self.packets_lost = 0
        self.packets_sent = 0


class RetransmitTimer:
    def __init__(self, connection: RUDPConnection, timeout = 1.0) -> None:
        self.timeout = timeout
        self.connection = connection
        self.ackEvent = threading.Event()

        # Dictionary of packet seq to their timers
        self.timers = {}

        # Dictionary of packet expected ackseq to their send timestamps
        self.send_timestamps = {}
    
    def sendto(self, packet: TCPPacket, client_addr, is_retransmit = False):
        # Send the packet to the client
        if is_retransmit:
            # Remove timestamp of previous send to prevent estimation of RTT
            try:
                del self.send_timestamps[packet.seq + len(packet.payload)]
            except KeyError:
                pass
        else:
            self.send_timestamps[packet.seq + len(packet.payload)] = time.time()
        self.connection.udpTask.sendto(self.connection.socket, packet.encode(), client_addr)
        self.connection.manager.packets_sent += 1
        # Start a timer for the packet
        self.timers[packet.seq] = threading.Timer(self.timeout, self.retransmit, args=(packet, client_addr))
        self.timers[packet.seq].start()

    def retransmit(self, packet: TCPPacket, client_addr, due_to_timeout = True):
        self.connection.manager.packets_lost += 1
        while packet.seq > self.connection.lastByteAcked + self.connection.cwnd:
            log(20, f'retransmit: Waiting for cwnd to open up')
            self.ackEvent.wait(timeout=2)
            self.ackEvent.clear()
        if due_to_timeout:
            self.connection.timeoutHandler(packet=packet, client_addr=client_addr)
        self.sendto(packet, client_addr, is_retransmit=True)
        
    def resend_immediately(self, seq):
        self.timers[seq].cancel()
        self.timers[seq].function(*self.timers[seq].args, due_to_timeout=False)

    def wait_until_all_acked(self):
        while len(self.timers) > 0:
            self.ackEvent.wait()
            self.ackEvent.clear()

    def cancel(self, seq):
        s = self.timers[seq]
        del self.timers[seq]
        s.cancel()
        self.ackEvent.set()

    def cancelAll(self, ackseq):
        set_to_delete = set()
        for seq in self.timers:
            if seq < ackseq:
                set_to_delete.add(seq)
        for seq in set_to_delete:
            self.timers[seq].cancel()
            del self.timers[seq]
        
        set_to_delete = set()
        for seq in self.send_timestamps:
            if seq < ackseq:
                set_to_delete.add(seq)
        for seq in set_to_delete:
            del self.send_timestamps[seq]
        
        self.ackEvent.set()


class RTTEstimator:
    # Estimate RTT with exponential moving average
    def __init__(self, alpha = 0.125, init = 1) -> None:
        self.alpha = alpha
        self.estimatedRTT = init
    
    def update(self, sampleRTT):
        self.estimatedRTT = (1 - self.alpha) * self.estimatedRTT + self.alpha * sampleRTT
        log(25, f'Estimated RTT: {self.estimatedRTT}')

    def get(self):
        return self.estimatedRTT

def log(level, *msg):
    if LOG_LEVEL >= level:
        print(*msg)