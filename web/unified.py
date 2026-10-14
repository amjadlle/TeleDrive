"""Single-process Web UI + path-style S3, routed by socket-local TCP port."""
import argparse
import socket
import uvicorn
from web.app import app as web_app
from web.s3 import app as s3_app

class UnifiedApplication:
    def __init__(self, web_port=8080, s3_port=9000):
        if not all(1 <= value <= 65535 for value in (web_port,s3_port)) or web_port == s3_port:
            raise ValueError("Distinct valid Web and S3 ports required")
        self.web_port, self.s3_port = web_port, s3_port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await web_app(scope, receive, send)
            return
        if scope["type"] not in ("http","websocket"):
            raise RuntimeError("Unsupported ASGI scope")
        server = scope.get("server")
        if not server or len(server) < 2:
            raise RuntimeError("Missing local listener identity")
        if server[1] == self.web_port:
            await web_app(scope, receive, send)
        elif server[1] == self.s3_port:
            await s3_app(scope, receive, send)
        elif scope["type"] == "http":
            from starlette.responses import PlainTextResponse
            await PlainTextResponse("Unknown TeleDrive listener",status_code=421)(scope,receive,send)
        else:
            await send({"type":"websocket.close","code":1008})

app = UnifiedApplication()

def _bind(host, port):
    sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        sock.bind((host,port))
        sock.listen(2048)
        sock.set_inheritable(False)
        return sock
    except BaseException:
        sock.close()
        raise

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--host",default="0.0.0.0")
    parser.add_argument("--web-port",type=int,default=8080)
    parser.add_argument("--s3-port",type=int,default=9000)
    args=parser.parse_args()
    service=UnifiedApplication(args.web_port,args.s3_port)
    listeners=[]
    try:
        listeners=[_bind(args.host,service.web_port),_bind(args.host,service.s3_port)]
        uvicorn.Server(uvicorn.Config(service,host=args.host,port=args.web_port,
                                     workers=1,lifespan="on",server_header=False)).run(sockets=listeners)
    finally:
        for listener in listeners:
            listener.close()

if __name__=="__main__":
    main()
