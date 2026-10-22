"""Experiment and Fault-Injection Controller (component 8).

    python -m testbed.controller down              # take the DNS server down
    python -m testbed.controller up                # bring it back
    python -m testbed.controller move lab.college.test 172.28.0.99
    python -m testbed.controller delete gone.college.test
    python -m testbed.controller status
"""
import socket
import sys


def send(command: str, host="127.0.0.1", port=5302, timeout=3.0) -> str:
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall((command + "\n").encode())
        return s.makefile().readline().strip()


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "move":
        line = f"set {argv[1]} {argv[2]}" + (f" {argv[3]}" if len(argv) > 3 else "")
    elif cmd == "delete":
        line = f"del {argv[1]}"
    else:
        line = " ".join(argv)
    print(send(line))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
