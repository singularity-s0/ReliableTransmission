import os
import hashlib, io

class Segment:
    def __init__(self, seq, data) -> None:
        self.seq = seq
        self.data = data

class FileSegmentReader:
    '''
    Read file and split it into n segment
    each is bytes for RUDPSender to send
    '''
    def __init__(self, file_name, segment_count) -> None:
        self.file_name = file_name
        self.segment_count = segment_count
        self.file_size = os.path.getsize(file_name)
        self.segment_size = self.file_size // (segment_count)
    
    def read(self):
        with open(self.file_name, 'rb') as fp:
            segments = []
            for i in range(self.segment_count + 1):
                seq = fp.tell()
                segment = fp.read(self.segment_size)
                if segment:
                    segments.append(Segment(seq=seq, data=io.BytesIO(segment)))

            fp.seek(0)
            bytes = fp.read()
            md5 = hashlib.md5(bytes).hexdigest()
        return segments, md5