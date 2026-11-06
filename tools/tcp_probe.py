"""TCP 通道探针：在本机当「假网关」，验证 ESP32-C6 的 Wi-Fi 通路。

用途（2026-11-06）：
  板子刚插上电脑时，只能靠串口下发 net_cfg；一旦 C6 切到 Wi-Fi/TCP，
  后续所有命令都得从 socket 走。这个脚本同时开两个口：

    0.0.0.0:12345   给 C6 连进来，收到的每一行原样打印
    127.0.0.1:12346 给本机用的「遥控器」——往里发一行，就转发给 C6

  这样一个进程就能既看上行、又发下行，不必反复重启监听。

用法：
    python tools/tcp_probe.py [--port 12345] [--ctl-port 12346]

  另开一个终端发命令：
    python -c "import socket;s=socket.create_connection(('127.0.0.1',12346));\
s.sendall(b'{\"cmd\":\"net_status\"}\\n');s.close()"
"""
import argparse
import socket
import threading
import time

LOG = []


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


def handle_c6(conn, peer):
    """一路 C6 连接：把收到的字节切行打印。"""
    log(f"C6 已连入，来自 {peer[0]}:{peer[1]}")
    buf = b""
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                log("C6 断开连接")
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    log(f"← {line.decode('utf-8', 'replace')}")
    except Exception as e:                              # noqa: BLE001
        log(f"接收异常：{e}")
    finally:
        try:
            conn.close()
        except Exception:                               # noqa: BLE001
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12345, help="C6 连进来的端口")
    ap.add_argument("--ctl-port", type=int, default=12346, help="本机遥控端口")
    args = ap.parse_args()

    cur = {"conn": None}

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.port))
    srv.listen(1)
    log(f"监听 0.0.0.0:{args.port} 就绪，等 C6 连入")

    ctl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ctl.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ctl.bind(("127.0.0.1", args.ctl_port))
    ctl.listen(4)
    log(f"遥控 127.0.0.1:{args.ctl_port} 就绪，往里发一行即转发给 C6")

    def ctl_loop():
        while True:
            c, _ = ctl.accept()
            try:
                data = c.recv(4096).strip()
                if not data:
                    continue
                if cur["conn"] is None:
                    log(f"✗ 还没有 C6 连接，丢弃：{data.decode('utf-8', 'replace')}")
                    continue
                cur["conn"].sendall(data + b"\n")
                log(f"→ {data.decode('utf-8', 'replace')}")
            except Exception as e:                      # noqa: BLE001
                log(f"转发失败：{e}")
            finally:
                try:
                    c.close()
                except Exception:                       # noqa: BLE001
                    pass

    threading.Thread(target=ctl_loop, daemon=True).start()

    while True:
        conn, peer = srv.accept()
        cur["conn"] = conn
        try:
            handle_c6(conn, peer)
        finally:
            cur["conn"] = None


if __name__ == "__main__":
    main()
