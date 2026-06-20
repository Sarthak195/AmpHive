import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from tapo import ApiClient

# Configuration
TAPO_EMAIL = "sjgotnfts1@gmail.com"
TAPO_PASSWORD = "Nitya@2001"
PLUG_IP = "192.168.1.4"
PORT = 8000

class RelayHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        
        try:
            if path == "/health":
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')
            elif path == "/info":
                info = asyncio.run(self.get_info())
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(info).encode())
            elif path == "/on":
                asyncio.run(self.turn_on())
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "on"}')
            elif path == "/off":
                asyncio.run(self.turn_off())
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "off"}')
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    async def get_client(self):
        client = ApiClient(TAPO_EMAIL, TAPO_PASSWORD)
        return await client.p110(PLUG_IP)

    async def get_info(self):
        device = await self.get_client()
        info = await device.get_device_info()
        return info.to_dict()

    async def turn_on(self):
        device = await self.get_client()
        await device.on()

    async def turn_off(self):
        device = await self.get_client()
        await device.off()

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), RelayHandler)
    print(f"Relay Server running on http://0.0.0.0:{PORT}")
    print(f"The cloud can now control the plug at {PLUG_IP} through this relay!")
    print("Keep this window open while testing.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
