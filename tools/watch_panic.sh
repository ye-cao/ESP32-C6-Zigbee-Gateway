#!/usr/bin/env bash
# add-on 日志只回最后 100 行，崩溃转储 ~90 行会把它整个冲出窗口。
# 这里 0.6 s 采一次，把增量累积到 build/live_log.txt，命中 rst:0x 就单独存一份。
TOKEN=$(cat _ha_token.tmp)
OUT=build/live_log.txt
PANIC=build/panic_latest.txt
: > "$OUT"
prev=""
for i in $(seq 1 900); do
  cur=$(curl -s -m 6 -H "Authorization: Bearer $TOKEN" \
        "http://192.168.101.5:8123/api/hassio/addons/local_esp32c6_gateway/logs")
  # 去掉时间戳前缀做对齐（同一条日志每次行首时间戳相同，其实可直接比对）
  if [ "$cur" != "$prev" ]; then
    printf '%s\n' "$cur" >> "$OUT"
    printf -- '---- sample %s ----\n' "$(date +%H:%M:%S)" >> "$OUT"
    if printf '%s' "$cur" | grep -q "rst:0x"; then
      printf '%s\n' "$cur" > "$PANIC"
      echo "PANIC CAPTURED at $(date +%H:%M:%S)"
    fi
    prev="$cur"
  fi
  sleep 0.6
done
