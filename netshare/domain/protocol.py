"""Binary WebSocket protocol for /ws/stream — 1-byte type prefix + payload."""

CFG = b"\x00"  # server -> client: UTF-8 JSON stream config
VID = b"\x01"  # server -> client: JPEG frame
AUD = b"\x02"  # server -> client: Int16 LE PCM
CLP = b"\x03"  # bidirectional:   UTF-8 clipboard text
CTL = b"\x04"  # client -> server: UTF-8 JSON remote-control event
IDL = b"\x05"  # server -> client: heartbeat, screen unchanged (no payload)
