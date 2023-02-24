import argparse
from Client import Client

from Server import ServerDispatcher

parser = argparse.ArgumentParser(description='FDFTP Protocol Implementation')
parser.add_argument('-s', '--server', action='store_true', help='Run in Server mode')
parser.add_argument('-d', '--download', help='Download file [filepath] from server', metavar='filepath')
parser.add_argument('-u', '--upload', help='Upload file [filepath] to server', metavar='filepath', type=argparse.FileType('rb'))
parser.add_argument('-p', '--port', help='Port number (default 52849)', metavar='port number', type=int, default=52849)
parser.add_argument('-a', '--address', help='Server IP address (default 8.218.117.184)', metavar='server address', default='8.218.117.184')
parser.add_argument('-c', '--connection-count', help='Number of connections to server (default 16)', metavar='connection count', type=int, default=16)

if __name__ == '__main__':
    args = parser.parse_args()
    if args.server:
        print('Running as server on port', args.port)
        server = ServerDispatcher(args.port)
        server.listen()
    elif args.download:
        print('Downloading', args.download)
        client = Client()
        client.connect(args.address, args.port)
        client.getFile(args.download, args.connection_count)
    elif args.upload:
        print('Uploading', args.upload.name)
        client = Client()
        client.connect(args.address, args.port)
        client.sendFile(args.upload.name, args.connection_count)
    else:
        parser.print_help()