# MariaDB 持久化层

`gateway_service.py` 把所有传感器数据写进 MariaDB。表结构一共四张：两张**登记表**
（UPSERT 写入）、两张**时间序列表**（只 INSERT）。

```
连接：-h 192.168.178.218 -u gh -p wagodb
建表：mysql -h 192.168.178.218 -u gh -p wagodb < schema.sql
```

> ⚠️ **历史文档**：这是树莓派上 `gateway_service.py` 那套旧部署的库设计。
> 现行 add-on 默认不连数据库（`DB_HOST` 为空 = 禁用），历史数据交给 HA recorder。
> add-on 里仍保留可选的 `DB_HOST` 支持，用于沿用这张表。

---

## 表关系总览

```
esp32_gateways          esp32_zigbee_devices
┌──────────────┐        ┌──────────────────────────┐
│ mac  PK      │◄──┐    │ mac+addr  PK             │◄──┐
│ status       │   │    │ ieee                     │   │
│ last_seen    │   │    │ name                     │   │
└──────────────┘   │    │ last_seen                │   │
                   │    └──────────────────────────┘   │
esp32_vitals        │    esp32_zigbee_data              │
┌──────────────┐   │    ┌──────────────────────────┐   │
│ id  PK AI    │   │    │ id  PK AI                │   │
│ mac          │───┘    │ mac                      │───┘
│ ts  (ms)     │        │ addr                     │
│ bpm          │        │ cluster                  │
│ rpm          │        │ ts  (ms)                 │
│ bpm_cat      │        │ value  (scaled)          │
│ rpm_cat      │        │ raw_json                 │
│ radar_status │        └──────────────────────────┘
└──────────────┘
```

---

## esp32_gateways

**用途：** 每块物理 ESP32-C6 网关一行。每收到一条 `vital-gw-XXXX/status` MQTT 消息写入。

**写入方式：** `INSERT … ON DUPLICATE KEY UPDATE`（UPSERT）

| 字段 | 类型 | 说明 |
|---|---|---|
| `mac` | `CHAR(8)` PK | WiFi MAC 的后 4 字节（十六进制），如 `a1b2c3d4`。同时也是该网关发布的全部 MQTT 话题的唯一前缀。 |
| `status` | `VARCHAR(16)` | `"online"` 或 `"offline"`。TCP 断开时由 broker 的遗嘱（LWT）自动置为 `"offline"`。 |
| `last_seen` | `DATETIME` | 最后一次收到状态消息的服务端时间（秒精度）。 |

**常用查询：**

```sql
-- 所有网关及其最后在线时间
SELECT mac, status, last_seen FROM esp32_gateways ORDER BY last_seen DESC;

-- 掉线超过 10 分钟的网关
SELECT mac, last_seen
FROM esp32_gateways
WHERE status = 'offline' OR last_seen < NOW() - INTERVAL 10 MINUTE;
```

---

## esp32_vitals

**用途：** MR60BHA2 雷达测量（心率 + 呼吸率）的时间序列。每条 `vital-gw-XXXX/mr60bha1`
消息写入一行。

**写入方式：** 只 `INSERT`（行永不修改）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `INT UNSIGNED` PK AI | 代理主键。 |
| `mac` | `CHAR(8)` | 网关 MAC → `esp32_gateways.mac`。 |
| `ts` | `DATETIME(3)` | 服务端时间戳，**毫秒**精度（`NOW(3)`）。 |
| `bpm` | `SMALLINT UNSIGNED` | 心率（次/分）。传感器初始化期间为 `NULL`。 |
| `rpm` | `SMALLINT UNSIGNED` | 呼吸率（次/分）。传感器初始化期间为 `NULL`。 |
| `bpm_cat` | `TINYINT UNSIGNED` | 心率质量分类：`0`=无 `1`=正常 `2`=偏快 `3`=偏慢。 |
| `rpm_cat` | `TINYINT UNSIGNED` | 呼吸率质量分类：编码同上。 |
| `radar_status` | `TINYINT UNSIGNED` | 传感器工作状态：`0`=初始化 `1`=校准中 `2`=测量中。 |

**索引：** `(mac, ts)` —— 用于按网关查时间区间。

**常用查询：**

```sql
-- 网关 a1b2c3d4 最近 10 条生命体征
SELECT ts, bpm, rpm, bpm_cat, rpm_cat, radar_status
FROM esp32_vitals
WHERE mac = 'a1b2c3d4'
ORDER BY ts DESC LIMIT 10;

-- 按分钟平均心率（最近一小时）
SELECT
    DATE_FORMAT(ts, '%Y-%m-%d %H:%i:00') AS minute,
    ROUND(AVG(bpm), 1)  AS avg_bpm,
    ROUND(AVG(rpm), 1)  AS avg_rpm
FROM esp32_vitals
WHERE mac = 'a1b2c3d4'
  AND ts > NOW() - INTERVAL 1 HOUR
  AND radar_status = 2        -- 只统计“测量中”的帧
GROUP BY minute
ORDER BY minute;

-- 每天的行数（数据量）
SELECT DATE(ts) AS day, COUNT(*) AS rows
FROM esp32_vitals
GROUP BY day ORDER BY day DESC;
```

---

## esp32_zigbee_devices

**用途：** 所有入过网的 Zigbee 终端设备的登记表，一台一行。在
`vital-gw-XXXX/zigbee/0xADDR/status` 的入网事件时写入，收到新数据时更新。

**写入方式：** `INSERT … ON DUPLICATE KEY UPDATE`（UPSERT）

| 字段 | 类型 | 说明 |
|---|---|---|
| `mac` | `CHAR(8)` | 该设备所属网关。复合主键的一部分。 |
| `addr` | `VARCHAR(8)` | Zigbee 16 位短地址，如 `0x1a2b`。入网时由协调器分配，**重新入网可能改变**。复合主键的一部分。 |
| `ieee` | `VARCHAR(24)` | Zigbee 64 位 IEEE（扩展）地址，如 `0x00124b001234abcd`。**全局唯一**，跨重新入网稳定。 |
| `name` | `VARCHAR(64)` | 人类可读名称。通过网页（`POST /api/device`）或 `devices.json` 设置；未命名时为空串。 |
| `last_seen` | `DATETIME` | 每次向其写入 `esp32_zigbee_data` 时更新。 |

**主键：** `(mac, addr)` —— 同一个短地址可能出现在不同网关上。跨网关唯一标识设备请用 `ieee`。

**常用查询：**

```sql
-- 所有已配对设备及其最后活动时间
SELECT mac, addr, ieee, name, last_seen
FROM esp32_zigbee_devices
ORDER BY last_seen DESC;

-- 静默超过 1 小时的设备（可能掉线）
SELECT mac, addr, name, last_seen
FROM esp32_zigbee_devices
WHERE last_seen < NOW() - INTERVAL 1 HOUR;

-- 还没有起名的设备
SELECT mac, addr, ieee FROM esp32_zigbee_devices WHERE name = '';
```

---

## esp32_zigbee_data

**用途：** 所有 Zigbee 传感器读数的时间序列。任何
`vital-gw-XXXX/zigbee/0xADDR/<cluster>` 话题上的每条消息写一行。

**写入方式：** 只 `INSERT`（行永不修改）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `INT UNSIGNED` PK AI | 代理主键。 |
| `mac` | `CHAR(8)` | 网关 MAC。 |
| `addr` | `VARCHAR(8)` | 上报设备的 Zigbee 短地址。 |
| `cluster` | `VARCHAR(32)` | ZCL 簇名：`temperature` · `humidity` · `illuminance` · `on_off` · `occupancy` · `raw` |
| `ts` | `DATETIME(3)` | 服务端时间戳，**毫秒**精度。 |
| `value` | `DOUBLE` | 换算后的数值 —— 见下表。未知（`raw`）簇为 `NULL`。 |
| `raw_json` | `TEXT` | MQTT 上的原始 JSON，原样保存，便于追溯与重算。 |

### 各簇的 `value` 换算

| `cluster` | `value` 公式 | 示例 |
|---|---|---|
| `temperature` | `raw / 100.0` → °C | `2150` → `21.50` |
| `humidity` | `raw / 100.0` → % RH | `5500` → `55.00` |
| `illuminance` | 原始勒克斯（整数） | `320` → `320` |
| `on_off` | `1` = 开，`0` = 关 | |
| `occupancy` | `1` = 有人，`0` = 无人 | |
| `raw` | `NULL`（未知簇） | 只存在 `raw_json` 里 |

**索引：** `(mac, addr, cluster, ts)` —— 用于按设备+簇查时间区间。

**常用查询：**

```sql
-- 每台设备最新的一次温度
SELECT z.name, d.addr, d.value, d.ts
FROM esp32_zigbee_data d
JOIN esp32_zigbee_devices z USING (mac, addr)
WHERE d.cluster = 'temperature'
  AND d.ts = (
      SELECT MAX(ts) FROM esp32_zigbee_data
      WHERE mac = d.mac AND addr = d.addr AND cluster = 'temperature'
  )
ORDER BY d.ts DESC;

-- 单台设备按小时平均温度（最近 24 小时）
SELECT
    DATE_FORMAT(ts, '%Y-%m-%d %H:00') AS hour,
    ROUND(AVG(value), 2)              AS avg_temp_c,
    MIN(value)                        AS min_temp_c,
    MAX(value)                        AS max_temp_c
FROM esp32_zigbee_data
WHERE mac    = 'a1b2c3d4'
  AND addr   = '0x1a2b'
  AND cluster = 'temperature'
  AND ts > NOW() - INTERVAL 24 HOUR
GROUP BY hour
ORDER BY hour;

-- 今天的全部开关事件
SELECT d.ts, z.name, d.addr, d.value AS state, d.raw_json
FROM esp32_zigbee_data d
JOIN esp32_zigbee_devices z USING (mac, addr)
WHERE d.cluster = 'on_off'
  AND DATE(d.ts) = CURDATE()
ORDER BY d.ts;

-- 每天每个簇的数据量
SELECT DATE(ts) AS day, cluster, COUNT(*) AS rows
FROM esp32_zigbee_data
GROUP BY day, cluster
ORDER BY day DESC, rows DESC;
```

---

## Python 层（`gateway_service.py`）

### 连接与重连

```python
# 配置（可用环境变量覆盖）
DB_HOST = os.getenv("DB_HOST", "192.168.178.218")
DB_USER = os.getenv("DB_USER", "gh")
DB_PASS = os.getenv("DB_PASS", "a12345")
DB_NAME = os.getenv("DB_NAME", "wagodb")
```

`_db_cursor()` 在每条语句前调用 `ping(reconnect=True)`，这样瞬时网络错误或
idle-timeout 断连都能自动恢复。

### 线程安全

所有数据库调用都在同一把 `threading.Lock`（`_db_lock`）下执行，因为
`gateway_service.py` 是多线程的（MQTT 回调、HTTP 处理、看门狗）；`pymysql` 的连接
默认不是线程安全的。

### 函数

| 函数 | 目标表 | 方式 | 触发 |
|---|---|---|---|
| `db_upsert_gateway(mac, status)` | `esp32_gateways` | UPSERT | `vital-gw-XXXX/status` |
| `db_insert_vitals(mac, data)` | `esp32_vitals` | INSERT | `vital-gw-XXXX/mr60bha1` |
| `db_upsert_zigbee_device(mac, addr, ieee, name)` | `esp32_zigbee_devices` | UPSERT | Zigbee 入网事件 |
| `db_insert_zigbee_data(mac, addr, cluster, raw, value)` | `esp32_zigbee_data` + 更新 `last_seen` | INSERT | 任意 Zigbee 数据 |

### 初始化

`_db_init()` 在 `main()` 启动时调用一次，跑完四条 `CREATE TABLE IF NOT EXISTS`，
首次运行自动建表，不需要手工执行 DDL。

---

## Grafana / BI 接入

这些表可以直接给 Grafana 的 MySQL 数据源用：

```sql
-- Grafana 时序面板：最近 6 小时心率
SELECT
    ts          AS "time",
    bpm         AS "心率 (BPM)",
    rpm         AS "呼吸率 (/min)"
FROM esp32_vitals
WHERE mac = 'a1b2c3d4'
  AND ts BETWEEN $__timeFrom() AND $__timeTo()
  AND radar_status = 2
ORDER BY ts;
```
