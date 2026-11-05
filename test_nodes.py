# -*- coding: utf-8 -*-
"""serial_gateway.py 里持久化节点归属的测试。

Extracts the real code block from the shipped source (no copy!) and runs it
against a temp _NODES_FILE. Sequence mirrors the real event order:

  1. Geraet joint  -> {"t":"zigbee","sub":"join",...,"ieee":"..."}
                      dispatch ruft _ha_learn_ieee(addr, ieee)
  2. 第一次 _ha_node(addr) 会生成 Node-ID
  3. 设备带着**新**短地址回来，join 事件依然最先到
  4. _ha_node(新地址) **必须**给出旧的 Node-ID
"""
import io, json, os, re, sys, tempfile

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "host", "addon", "esp32c6_gateway", "serial_gateway.py")

with io.open(SRC, "r", encoding="utf-8") as f:
    src = f.read()

a = src.index("_ha_devices = {}")
b = src.index("def _ha_state(")
block = src[a:b]
print("extrahiert: %d Zeichen" % len(block))

tmpdir = tempfile.mkdtemp(prefix="ndtest_")
nodes_file = os.path.join(tmpdir, "ha_nodes.json")

ns = {"os": os, "json": json, "__builtins__": __builtins__,
      # 位于被截取代码块**之前**的模块级全局：
      "_noread": set(), "_ctrl": set()}
exec(compile(block, "<extract>", "exec"), ns)
ns["_NODES_FILE"] = nodes_file          # isolate from any real /data
ns["_DATA_DIR"] = tmpdir

ok = 0
total = 0


def check(label, cond, detail=""):
    global ok, total
    total += 1
    if cond:
        ok += 1
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s %s" % (label, detail))
        sys.exit(1)


IEEE = "00:12:4b:00:aa:bb:cc:dd"

print("\n1) 0x07da 入网、学到 IEEE，再生成 Node-ID")
ns["_ha_learn_ieee"]("0x07da", IEEE)                    # <- 测试之前缺的就是这一步
n1 = ns["_ha_node"]("0x07da")
print("     c6_%s" % n1)
check("Node-ID 照旧由地址生成（不迁移）", n1 == "c6_0x07da",
      "got %r" % n1)

print("\n2) 第二次调用：名字保持冻结")
n1b = ns["_ha_node"]("0x07da")
check("stabil", n1b == n1, "%r != %r" % (n1b, n1))

print("\n3) 文件已写入")
check("ha_nodes.json existiert", os.path.isfile(nodes_file))
with io.open(nodes_file, "r", encoding="utf-8") as f:
    saved = json.load(f)
check("addr_nodes 里有 0x07da", saved["addr_nodes"].get("0x07da") == n1, str(saved["addr_nodes"]))
check("ieee_nodes 里有这个 IEEE", saved["ieee_nodes"].get(IEEE) == n1, str(saved["ieee_nodes"]))
check("addr_ieee 里有这条对应", saved["addr_ieee"].get("0x07da") == IEEE, str(saved["addr_ieee"]))

print("\n4) 设备带着**新**地址 0x08a1 回来（join 事件最先到！）")
ns["_ha_learn_ieee"]("0x08a1", IEEE)
n2 = ns["_ha_node"]("0x08a1")
print("     c6_%s" % n2)
check("沿用了旧 Node-ID（没有重复实体）", n2 == n1, "%r != %r" % (n2, n1))

print("\n5) **第二台**设备不能撞名")
ns["_ha_learn_ieee"]("0x0abc", "00:12:4b:11:22:33:44:55")
n3 = ns["_ha_node"]("0x0abc")
print("     c6_%s" % n3)
check("eigene Node-ID", n3 == "c6_0x0abc", "got %r" % n3)

print("\n6) 模拟重启：重新加载文件，旧地址保住名字")
ns2 = {"os": os, "json": json, "__builtins__": __builtins__,
       "_noread": set(), "_ctrl": set()}
exec(compile(block, "<extract2>", "exec"), ns2)
ns2["_NODES_FILE"] = nodes_file
ns2["_DATA_DIR"] = tmpdir
ns2["_nodes_load"]()
n4 = ns2["_ha_node"]("0x07da")
check("重启后 Node-ID 相同", n4 == n1, "%r != %r" % (n4, n1))

print("\n7) 重启 + 设备换了地址（本次会话没有 join 事件）")
n5 = ns2["_ha_node"]("0x08a1")
check("照样是旧 Node-ID（来自文件）", n5 == n1, "%r != %r" % (n5, n1))

print("\n%d/%d Pruefungen bestanden" % (ok, total))
print("temp: %s" % nodes_file)
