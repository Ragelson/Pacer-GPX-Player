import socket

PORT = 5554
TOKEN = "подставь сюда токен"

def read_until_ok(sock, timeout=5):
    sock.settimeout(timeout)
    buf = b""
    while b"OK" not in buf and b"KO" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="ignore")

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect(("localhost", PORT))
print("Приветствие:", read_until_ok(s))

s.sendall(f"auth {TOKEN}\n".encode())
print("Auth:", read_until_ok(s))

s.sendall(b"geo fix 56.03659 54.775728\n")
print("Geo:", read_until_ok(s))

s.close()
print("Готово")
