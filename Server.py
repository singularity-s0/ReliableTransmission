import os
import threading
from FileReader import FileSegmentReader
from RUDPReceiver import RUDPReceiverDispatcher
from RUDPSender import RUDPMultiConnectionManager, RUDPSender
from RUDPServerSender import server_send
from common import FakeTask, RUDPConnection, TCPPacket, TCPListener, TCPPacketFlags, RetransmitTimer, log, MSS
import socket, asyncio
import FDFTPsocket

class ServerHandler(RUDPConnection):
    def __init__(self, port, socket, alive_timeout = 10) -> None:
        super().__init__()
        self.seq = 0
        self.ackseq = 0
        self.status = 'CLOSED'
        self.port = port
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
    
    async def handler(self, data, client_addr):
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
            # Expect: Data packet from client
            self.lastByteRcvd = max(packet.seq + 1, self.lastByteRcvd)
            log(30, "Out of Order", self.out_of_order_info)
            if packet.seq == self.ackseq:
                self.ackseq += len(packet.payload)
                # Check if there are any out-of-order packets
                i = 0
                while i < len(self.out_of_order_info):
                    if self.out_of_order_info[i][0] == self.ackseq:
                        self.ackseq += self.out_of_order_info[i][1]
                        self.out_of_order_info.pop(i)
                        i = -1
                    i += 1
            elif packet.seq > self.ackseq:
                # Out of order packet, append only if it's not a duplicate
                if not any(packet.seq == start for start, len in self.out_of_order_info):
                    self.out_of_order_info.append((packet.seq, len(packet.payload)))
            
            log(20, 'Received', packet.payload.decode('utf-8'))
            # Note that a command might be repeated several times
            if packet.flags & TCPPacketFlags.COMMAND:
                response = self.processCommand(packet.payload.decode('utf-8'), client_addr=client_addr)
            else:
                response = ''

            self.seq += 1
            ret_packet = TCPPacket(1000, 0, self.seq, self.ackseq, response.encode('utf-8'))
            self.udpTask.sendto(self.socket, ret_packet.encode(), client_addr)
        
    def timeoutHandler(self, packet, client_addr):
        log(5, 'Retransmitting packet', packet.seq, 'to', client_addr)
    
    def processCommand(self, command, client_addr) -> str:
        directive, *other = command.split('\n')
        if directive == 'PUT':
            file_name, file_size, expected_md5, segment_size, segment_count = other
            segment_size = int(segment_size)
            segment_count = int(segment_count)
            # Server should receive file from client, spawn RUDPReceiver
            # Bind RUDPReceiver to another open port
            ftp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ftp_socket.bind(('', 0))
            port = ftp_socket.getsockname()[1]
            print(f"File upload command received. Assigning port {port} for data transfer.")
            # Spawn RUDPReceiver
            ftp_receiver = RUDPReceiverDispatcher(ftp_socket, file_name=file_name, expected_md5=expected_md5, file_size=file_size, segment_count=segment_count, segment_size=segment_size)
            threading.Thread(target=ftp_receiver.listen).start()
            # Send port number to client
            return f"READY\n{port}"
        elif directive == 'GET':
            print('File download command received')
            ftp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ftp_socket.bind(('', 0))
            port = ftp_socket.getsockname()[1]
            self.sender_file_name, segment_count = other
            segment_count = int(segment_count)
            # See if we can open the file
            try:
                fp = open(self.sender_file_name, 'rb')
                size = os.path.getsize(self.sender_file_name)
                fp.close()
            except OSError as e:
                return f"ERROR {e}"
            # Return file name, file size and MD5
            reader = FileSegmentReader(self.sender_file_name, segment_count=segment_count)
            self.segments, self.md5 = reader.read()
            threading.Thread(target=server_send, args=(ftp_socket, self.sender_file_name, self.segments)).start()
            return f"READY\n{self.sender_file_name}\n{size}\n{self.md5}\n{reader.segment_size}\n{len(self.segments)}\n{port}"
        return 'UNKNOWN COMMAND'


class ServerDispatcher:
    def __init__(self, port) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.buffer_size = 1500
        self.port = port

        self.clients = {} # client_addr -> ServerHandler
    
    def listen(self) -> None:
        self.socket.bind(('0.0.0.0', self.port))
        while True:
            data, client_addr = self.socket.recvfrom(self.buffer_size)
            # Unavailable in python 3.6
            # asyncio.run(self.onReceive(data=data,client_addr=client_addr))
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self.onReceive(data=data,client_addr=client_addr))

    async def onReceive(self, data, client_addr):
        handler = self.clients.get(client_addr)
        if not handler:
            # New client
            handler = ServerHandler(port=self.port, socket=self.socket)
            self.clients[client_addr] = handler
        await handler.handler(data=data, client_addr=client_addr)