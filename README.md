# Reliable Transmission over UDP (RUDP)
A simple Reliable File Transfer Protocol over UDP implemented in Python. Class Project of COMP130136H, Honor Course of Computer Networking at Fudan University.

## Features
- Multithreaded connection
- Progress bar and speed estimation
- Speed & goodput & packet loss rate statistics
- File integrity check (MD5 checksum)
- Capable of handling multiple clients at the same time

## User Manual
- Setup your environment with `requirements.txt`
- Make sure your firewall allows new connections. The program dynamically allocates new ports during execution.
- Entry point is `main.py`, running without any arguments prints the help message
```
usage: main.py [-h] [-s] [-d filepath] [-u filepath] [-p port number] [-a server address] [-c connection count]

FDFTP Protocol Implementation

optional arguments:
  -h, --help            show this help message and exit
  -s, --server          Run in Server mode
  -d filepath, --download filepath
                        Download file [filepath] from server
  -u filepath, --upload filepath
                        Upload file [filepath] to server
  -p port number, --port port number
                        Port number (default 52849)
  -a server address, --address server address
                        Server IP address (default 8.218.117.184)
  -c connection count, --connection-count connection count
                        Number of connections to server (default 16)
```
Example usage:
- To start the server, run `python main.py -s`
- To upload a file, run `python main.py -u <filepath>`
- To download a file, run `python main.py -d <filepath>`

Client by default connects to the server at `8.218.117.184:52849` with 8 connections. You can change these settings with `-a` and `-p` and `-c` arguments. 

Full example:
`python main.py -u test.pdf -a 8.218.117.184 -p 52849 -c 16`

## Implementation Details
### Protocol
The protocol, unlike TCP, keeps an independent timer for each packet it sends. The timer is set to the RTT of the link + 0.2 seconds, the former of which is estimated with exponential moving average. Since RTT varies over time, the extra 0.2 seconds is added to ensure that no packet is retransmitted too early (this modification effectively reduced packet loss rate from 30% to 0.06% in our testing). Because of this change, in our implementation, the estimated RTT is not multiplied by 2 when a packet loss is detected (as in TCP). When a packet is acknowledged, all timers for packets with sequence number smaller than the acknowledged packet are cancelled. However, in addition to acknowledging in-order packets like GBN and TCP, the server also acknowledges out-of-order packets like SR. Each acknowledgement packet contains the sequence number of the next expected packet and the sequence number of the current received packet. The timer for the current received packet is also cancelled, slightly reducing retransmission costs. Out-of-order packets are buffered in the server and wrote to file in order.

We use a TCP Reno-like congestion control algorithm for our protocol, with support for all three stages: slow start, congestion avoidance and fast recovery. Due to our use of independent timer, however, the algorithm has to be tweaked so that congestion state is not updated multiple times upon packet loss. The implememtation is very much like TCP Reno. I'll skip the details.

### File Transfer
2 channels are created between the client and the server, the command channel and the data channel. Command channel is used to transmit command and error messages, while data channel is used to transfer file. Channels use different port. Only command channel port needs to be specified at startup. Data channel port is dynamically allocated and negotiated over command channel. Each channel is established through a TCP-style handshake before any data is actually sent through it.

Data channel also carries multiple streams, identified by `stream-id` at the packet header. This is used for multithreaded connection to speed up file transfer. The server can handle arbitary amount of streams at the same time. The client can specify the number of streams to use with `-c` argument. The default value is 16.

## Performance
As a (not very official) benchmark, we compared our protocol with standard file transfer over SSH (scp) over a typical home Internet connection:
| Speed  | SCP (standard TCP) | RUDP (1 connection) | RUDP (8 connections) | RUDP (16 connections) |
|--------|--------------------|---------------------|----------------------| ----------------------|
| Upload | 120.5 KB/s         | 58.3 KB/s           | 768 KB/s             | 1.57 MB/s             |
|Download| 1.50 MB/s          | 1.23 MB/s           | 1.94 MB/s            | 2.50 MB/s             |

Degradation is expected because Python's performance is certainly not as good as C. However, our use of multiple connections still resulted in a significant speedup. 16 connections is chosen because it is a typical value. We have yet to test the performance of our protocol with more connections.

## Notes
The code probably needs refactoring, but it works anyway.
