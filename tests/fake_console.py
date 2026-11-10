"""Минимальная имитация консоли эмулятора Android для тестов (без эмулятора)."""
import socketserver
import threading


class _H(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self._run()
        except ConnectionError:
            pass

    def _run(self):
        self.wfile.write(b"Android Console: type 'help'\r\nOK\r\n")
        for raw in self.rfile:
            line = raw.decode().strip()
            self.server.received.append(line)
            if line == "quit":
                self.wfile.write(b"OK: bye\r\n")
                return
            self.wfile.write(b"OK\r\n")


class FakeConsole(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port=0):
        super().__init__(("127.0.0.1", port), _H)
        self.received = []
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self):
        self.shutdown()
        self.server_close()
