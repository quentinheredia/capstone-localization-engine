# firmware/common/

Shared conventions across anchor and tag firmware.

## device_config.h

Generated automatically by `platform/backend/api/esp_flash.py` before each
`idf.py build`. Do **not** edit this file manually — it will be overwritten.

Each ESP-IDF project includes it via:

```c
#if __has_include("device_config.h")
    #include "device_config.h"
#else
    #warning "device_config.h not found -- using built-in defaults"
    ...
#endif
```

## MQTT topic scheme

| Device  | Topic                              |
|---------|------------------------------------|
| Anchor  | `capstone/tof/anchor/<id>/range`   |
| Tag     | `capstone/tof/tag/<id>/range`      |

The engine subscribes to `capstone/tof/+/+/range` (wildcard).

## JSON payload schema

```json
{
  "device_id":   "anchor_4",
  "target_id":   "tag_0",
  "distance_m":  2.50,
  "rtt_ps":      166667,
  "rssi":        -65,
  "confidence":  0.85,
  "scan_number": 42,
  "uptime_ms":   123456
}
```

`uptime_ms` is milliseconds-since-boot (no RTC battery).
The engine stamps each received message with server-side UTC.
