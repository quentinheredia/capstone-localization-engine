from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterator, Dict, List, Optional

from models import AccessPoint, ToFAnchor, RSSIMap, ToFMeasurement
from engine_wrappers import TelnetParserWrapper

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TelnetPipe
# ---------------------------------------------------------------------------

class TelnetPipe:
    def __init__(
        self,
        aps: List[AccessPoint],
        target_ssids: List[str],
        prompts: Dict[str, str],
        poll_interval_s: float = 3.0,
    ) -> None:
        self._aps            = aps
        self._targets        = set(target_ssids)
        self._prompt_main    = prompts.get("main", "eap350>")
        self._prompt_sub     = prompts.get("sub",  "eap350/wless2/network>")
        self._poll_interval  = poll_interval_s
        self._sessions: Dict[str, tuple] = {}
        self._parser         = TelnetParserWrapper()
        
        # 1. Executor Integration: Offload blocking C++ parsing
        self._executor = ThreadPoolExecutor(max_workers=len(aps) or 1)

    async def connect(self) -> None:
        tasks = [self._open_session(ap) for ap in self._aps]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _open_session(self, ap: AccessPoint) -> None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ap.host, 23), timeout=5.0
            )
            await self._read_until(reader, b"login:", 5.0)
            writer.write((ap.username + "\n").encode())
            await writer.drain()

            await self._read_until(reader, b"Password:", 5.0)
            writer.write((ap.password + "\n").encode())
            await writer.drain()

            await self._read_until(reader, self._prompt_main.encode(), 5.0)
            self._sessions[ap.host] = (reader, writer)
            log.info("TelnetPipe: connected to %s", ap.host)
        except Exception as exc:
            log.warning("TelnetPipe: could not connect to %s: %s", ap.host, exc)

    async def close(self) -> None:
        # 4. Lifecycle Management: Shutdown threads
        self._executor.shutdown(wait=True)
        for host, (reader, writer) in list(self._sessions.items()):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._sessions.clear()

    async def stream(self) -> AsyncIterator[RSSIMap]:
        while True:
            cycle_start = asyncio.get_running_loop().time()

            tasks = [self._poll_one(ap) for ap in self._aps]
            results_list = await asyncio.gather(*tasks, return_exceptions=True)

            rssi_map: RSSIMap = {}
            for ap, result in zip(self._aps, results_list):
                if isinstance(result, Exception):
                    log.warning("TelnetPipe: poll failed for %s: %s", ap.id, result)
                    continue
                if result:
                    rssi_map[ap.id] = result

            if rssi_map:
                yield rssi_map

            elapsed = asyncio.get_running_loop().time() - cycle_start
            await asyncio.sleep(max(0.0, self._poll_interval - elapsed))

    async def _poll_one(self, ap: AccessPoint) -> Optional[Dict[str, int]]:
        for attempt in range(2):
            session = self._sessions.get(ap.host)
            if not session:
                await self._open_session(ap)
                session = self._sessions.get(ap.host)
            if not session:
                return None

            reader, writer = session
            try:
                writer.write(b"wless2\n")
                await writer.drain()
                await self._read_until(reader, self._prompt_main.encode(), 3.0)

                writer.write(b"network\n")
                await writer.drain()
                await self._read_until(reader, self._prompt_sub.encode(), 3.0)

                writer.write(b"apscan\n")
                await writer.drain()
                raw = await self._read_until(reader, self._prompt_sub.encode(), 8.0)
                raw_text = raw.decode(errors="ignore")

                if len(raw_text) < 50:
                    raise ValueError(f"Insufficient data ({len(raw_text)}B)")

                # 2 & 5. Offloading Logic: Awaiting executor inside loop
                loop = asyncio.get_running_loop()
                rows = await loop.run_in_executor(
                    self._executor, 
                    self._parser.parse, 
                    raw_text
                )

                results: Dict[str, int] = {}
                for row in rows:
                    if row.get("ssid") in self._targets:
                        try:
                            results[row["ssid"]] = int(row["signal"])
                        except (KeyError, ValueError):
                            pass
                return results

            except Exception as exc:
                log.warning("TelnetPipe: %s attempt %d failed: %s", ap.host, attempt + 1, exc)
                try:
                    self._sessions[ap.host][1].close()
                except Exception:
                    pass
                self._sessions.pop(ap.host, None)
                if attempt == 0:
                    await asyncio.sleep(0.5)

        return {}

    @staticmethod
    async def _read_until(reader: asyncio.StreamReader, separator: bytes, timeout: float) -> bytes:
        buf = b""
        try:
            async with asyncio.timeout(timeout):
                while separator not in buf:
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
                    buf += chunk
        except TimeoutError:
            pass
        return buf


# ---------------------------------------------------------------------------
# MQTTPipe
# ---------------------------------------------------------------------------

class MQTTPipe:
    def __init__(
        self,
        anchors: List[ToFAnchor],
        broker_host: str  = "localhost",
        broker_port: int  = 1883,
        topic_prefix: str = "capstone",
        keepalive_s: int  = 60,
    ) -> None:
        self._anchors      = anchors
        self._broker_host  = broker_host
        self._broker_port  = broker_port
        self._prefix       = topic_prefix
        self._keepalive    = keepalive_s
        self._queue: asyncio.Queue[ToFMeasurement] = asyncio.Queue()
        self._client       = None
        
        # 1. Executor for MQTT background work if needed
        self._executor = ThreadPoolExecutor(max_workers=1)

    async def connect(self) -> None:
        if not self._anchors:
            log.info("MQTTPipe: no ToF anchors configured — skipping MQTT connect")
            return

        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log.warning("MQTTPipe: paho-mqtt not installed — ToF disabled")
            return

        # Capture the loop strictly within the async context
        loop = asyncio.get_running_loop()

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                for anchor in self._anchors:
                    topic = f"{self._prefix}/{anchor.mac}/tof"
                    client.subscribe(topic)
                    log.info("MQTTPipe: subscribed to %s", topic)

        def on_message(client, userdata, msg):
            try:
                # 3. Thread Safety: MQTT runs in its own thread. 
                # We must use call_soon_threadsafe to interact with the loop's Queue.
                data = json.loads(msg.payload.decode())
                meas = ToFMeasurement(
                    mac         = data.get("mac", ""),
                    distance_m = float(data.get("distance_m", 0.0)),
                    timestamp   = data.get("ts", ""),
                )
                loop.call_soon_threadsafe(self._queue.put_nowait, meas)
            except Exception as exc:
                log.warning("MQTTPipe: bad payload: %s", exc)

        client = mqtt.Client()
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect_async(self._broker_host, self._broker_port, self._keepalive)
        client.loop_start()
        self._client = client

    async def close(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        # 4. Lifecycle Management
        self._executor.shutdown(wait=True)

    async def stream(self) -> AsyncIterator[ToFMeasurement]:
        while True:
            meas = await self._queue.get()
            yield meas