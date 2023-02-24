from tqdm import tqdm
from common import FakeTask, RUDPConnection, TCPPacket, TCPPacketFlags, RetransmitTimer, log, MSS
import hashlib
import asyncio, io

class RUDPReceiver(RUDPConnection):
    def __init__(self, socket, segment_id, segment_size, bytesStream: io.BytesIO, dispatcher, file_start_seq = 0) -> None:
        super().__init__()
        self.stream_id = segment_id
        self.seq = 0
        self.ackseq = file_start_seq
        self.socket = socket
        self.udpTask = FakeTask()
        self.retransmitTimer = RetransmitTimer(self)
        self.dispatcher = dispatcher
        self.file_start_seq = file_start_seq # seq of the first byte of the file
        self.fp = bytesStream

        self.lastByteRcvd = file_start_seq
        self.lastByteRead = 0

        # Format: tuple of (start_seq, len)
        self.out_of_order_info = []
    
    async def handler(self, packet, client_addr):
        self.retransmitTimer.cancelAll(packet.ackseq)
        prev_ackseq = self.ackseq
        log(15, "Received seq", packet.seq, "expecting", self.ackseq)
        if packet.flags & TCPPacketFlags.FIN:
            # FIN received
            self.status = 'FIN_WAIT_1'
            ret_packet = TCPPacket(self.stream_id, (TCPPacketFlags.ACK | TCPPacketFlags.FIN), self.seq, packet.seq + 1, b'')
            self.udpTask.sendto(self.socket, ret_packet.encode(), client_addr)
            await self.dispatcher.onFinish(packet.stream_id)
            return
        # Expect: Data packet from client
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

        self.seq += 1
        self.write_file_at(packet.seq, packet.payload)
        self.dispatcher.update(self.ackseq - prev_ackseq)
        self.lastByteRcvd = max(packet.seq + 1, self.lastByteRcvd)
        # Also piggyback current seq number
        if packet.seq <= self.ackseq:
            piggyback_data = b''
        else:
            piggyback_data = str(packet.seq).encode('utf-8')
        ret_packet = TCPPacket(self.stream_id, 0, self.seq, self.ackseq, piggyback_data)
        self.udpTask.sendto(self.socket, ret_packet.encode(), client_addr)
    
    def write_file_at(self, offset, data):
        assert self.fp
        self.fp.seek(offset)
        self.fp.write(data)
        self.lastByteRead = max(self.lastByteRead, offset + len(data))
        
    def timeoutHandler(self, packet, client_addr):
        log(5, 'Retransmitting packet', packet.seq, 'to', client_addr)


class RUDPReceiverDispatcher:
    def __init__(self, socket, file_name, file_size, expected_md5, segment_count, segment_size) -> None:
        self.socket = socket
        self.buffer_size = 1500
        self.bytesStream = io.BytesIO()
        self.file_name = file_name
        self.expected_md5 = expected_md5
        self.progressBar = tqdm(total=int(file_size), unit='B', unit_scale=True, desc=f"Receiving {file_name}")
        self.finished = {}
        

        self.file_size = int(file_size)
        last_segment_size = self.file_size % segment_size

        self.receivers = []
        for i in range(segment_count):
            seg = last_segment_size if i == segment_count - 1 else segment_size
            self.finished[i] = False
            self.receivers.append(RUDPReceiver(socket=self.socket, segment_id=i, segment_size=seg, dispatcher=self, bytesStream=self.bytesStream, file_start_seq=i*segment_size))
    
    def listen(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            data, client_addr = self.socket.recvfrom(self.buffer_size)
            loop.run_until_complete(self.onReceive(data, client_addr)) # This is a compatibility layer for older code

    async def onReceive(self, data, client_addr):
        packet = TCPPacket.decode(data)
        stream_id = packet.stream_id
        await self.receivers[stream_id].handler(packet=packet,client_addr=client_addr)
    
    def update(self, n):
        self.progressBar.update(n)

    async def onFinish(self, stream_id):
        """Called when a receiver finishes"""
        self.finished[stream_id] = True
        if all(self.finished.values()):
            await asyncio.gather(*asyncio.Task.all_tasks() - {asyncio.tasks.Task.current_task()})
            #await asyncio.gather(*asyncio.all_tasks() - {asyncio.tasks.current_task()})
            self.bytesStream.flush()
            self.progressBar.close()
            self.bytesStream.seek(0)
            bytes = self.bytesStream.read()
            md5 = hashlib.md5(bytes).hexdigest()
            print('Received MD5:', md5, '\nExpected MD5:', self.expected_md5, '\nMatch:', md5 == self.expected_md5)
            if md5 != self.expected_md5:
                raise Exception('MD5 mismatch')
            print('Writing to file', self.file_name)
            # Write to file
            with open("recv_"+self.file_name, 'wb') as f:
                self.bytesStream.seek(0)
                f.write(self.bytesStream.read())
            self.bytesStream.close()
            print("Transfer complete")
            exit(0)
