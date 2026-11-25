#
# A small web view of the front panel: the LCD, the LEDs and the two
# buttons, live. Standard library only (http.server); one page, a JSON
# state endpoint, a Server-Sent Events stream and a button endpoint.
#
#   GET  /                 the panel page (panel.html next to this file)
#   GET  /api/state        current state as JSON
#   GET  /api/events       text/event-stream, one `data:` per change
#   POST /api/button/NAME  press and release enter | select
#
# Button presses need an `X-QnapLCD: 1` header. A browser only sends a
# custom header cross-origin after a CORS preflight, which this server never
# answers, so another web page can't press buttons for you. It is not
# authentication: anyone who can reach the port can use the panel, reboot
# and update prompts included. Bind it to a trusted address.
#
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'panel.html')
KEEPALIVE = 15  # seconds between SSE comments on an idle stream


class WebPanel:
    def __init__(self, address, port, get_state, press):
        """get_state() -> dict (JSON-serialisable); press(name) handles a
        button ('enter' or 'select'). Call notify() whenever the state may
        have changed."""
        self.get_state = get_state
        self.press = press
        self.version = 0
        self.changed = threading.Condition()
        with open(PAGE, 'rb') as f:
            self.page = f.read()

        panel = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'qnaplcd'

            def log_message(self, fmt, *args):
                pass  # keep the service log for the panel itself

            def send(self, code, body=b'', ctype='application/json'):
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split('?', 1)[0]
                if path in ('/', '/index.html'):
                    self.send(200, panel.page, 'text/html; charset=utf-8')
                elif path == '/api/state':
                    self.send(200, panel.state_json())
                elif path == '/api/events':
                    self.events()
                else:
                    self.send(404, b'{"error": "not found"}')

            def do_POST(self):
                path = self.path.split('?', 1)[0]
                name = path[len('/api/button/'):] if path.startswith('/api/button/') else None
                if name not in ('enter', 'select'):
                    self.send(404, b'{"error": "not found"}')
                elif self.headers.get('X-QnapLCD') != '1':
                    self.send(403, b'{"error": "missing X-QnapLCD header"}')
                else:
                    try:
                        panel.press(name)
                    except Exception as e:
                        print(f'web button {name} failed: {e!r}', file=sys.stderr)
                        self.send(500, b'{"error": "button handling failed"}')
                        return
                    self.send(204)

            def events(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                seen = -1
                try:
                    while True:
                        with panel.changed:
                            if panel.version == seen:
                                panel.changed.wait(KEEPALIVE)
                            current = panel.version
                        if current == seen:
                            self.wfile.write(b': keepalive\n\n')
                        else:
                            seen = current
                            self.wfile.write(b'data: ' + panel.state_json() + b'\n\n')
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # the browser went away

        server_class = ThreadingHTTPServer
        if ':' in address:
            class server_class(ThreadingHTTPServer):
                address_family = socket.AF_INET6
        self.server = server_class((address, port), Handler)
        self.server.daemon_threads = True

    def state_json(self):
        return json.dumps(self.get_state()).encode('utf-8')

    def notify(self):
        with self.changed:
            self.version += 1
            self.changed.notify_all()

    def start(self):
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        host, port = self.server.server_address[:2]
        host = f'[{host}]' if ':' in host else host
        print(f'web panel on http://{host}:{port}/', flush=True)
