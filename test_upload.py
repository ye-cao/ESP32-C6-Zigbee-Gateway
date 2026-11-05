#!/usr/bin/env python3
"""测固件上传用的 multipart 解析器（不起 HTTP 服务）。"""
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "host", "addon", "esp32c6_gateway", "serial_gateway.py")
src = open(SRC, encoding="utf-8").read()
i = src.index("def _save_upload")
j = src.index("def _fw_ota_start", i)

ns = {"os": os}
exec(src[i:j], ns)

B = b"----WebKitFormBoundaryABC123"
payload = bytes(range(256)) * 4          # 1 KiB 二进制数据，含 CRLF 字节

body = (
    b"--" + B + b"\r\n"
    b'Content-Disposition: form-data; name="file"; filename="fw.bin"\r\n'
    b"Content-Type: application/octet-stream\r\n\r\n"
    + payload + b"\r\n"
    b"--" + B + b"--\r\n"
)

dst = ns["_save_upload"]("multipart/form-data; boundary=" + B.decode(), body)
print("gespeichert:", dst)
saved = open(dst, "rb").read()
print("字节数：", len(saved))

ok = True
if saved != payload:
    print("FAIL Inhalt weicht ab")
    ok = False
    # 显示第一处差异
    for k in range(min(len(saved), len(payload))):
        if saved[k] != payload[k]:
            print(f"  第一处差异在第 {k} 字节：{saved[k]} != {payload[k]}")
            break
else:
    print("OK  二进制数据一比一还原（含 0x00/0x0d/0x0a）")

# 错误分支
if ns["_save_upload"]("text/plain", body) is not None:
    print("FAIL：没有 boundary 时不该存任何东西")
    ok = False
else:
    print("OK  没有 boundary → None")

if ns["_save_upload"]("multipart/form-data; boundary=" + B.decode(),
                      b"--" + B + b"\r\nContent-Disposition: form-data; name=\"x\"\r\n\r\n" +
                      b"--" + B + b"--\r\n") is not None:
    print("FAIL：没有 filename 的 part 不该存任何东西")
    ok = False
else:
    print("OK  没有 filename 的 part → None")

os.remove(dst)
sys.exit(0 if ok else 1)
