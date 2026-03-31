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
        log.info("[TelnetPipe] Connecting to %d AP(s): %s",
                 len(self._aps), [ap.host for ap in self._aps])
        tasks = [self._open_session(ap) for ap in self._aps]
        await asyncio.gather(*tasks, return_exceptions=True)
        connected = list(self._sessions.keys())
        log.info("[TelnetPipe] Connect phase done — %d/%d sessions up: %s",
                 len(connected), len(self._aps), connected)

    async def _open_session(self, ap: AccessPoint) -> None:
        log.debug("[Telnet][%s] Opening TCP connection to %s:23 …", ap.id, ap.host)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ap.host, 23), timeout=5.0
            )
            log.debug("[Telnet][%s] TCP connected — waiting for 'login:' prompt", ap.id)

            banner = await self._read_until(reader, b"login:", 5.0)
            log.debug("[Telnet][%s] ← banner/login prompt (%d B): %r", ap.id, len(banner), banner[-120:])

            log.debug("[Telnet][%s] → sending username %r", ap.id, ap.username)
            writer.write((ap.username + "\n").encode())
            await writer.drain()

            log.debug("[Telnet][%s] Waiting for 'Password:' prompt …", ap.id)
            pw_prompt = await self._read_until(reader, b"Password:", 5.0)
            log.debug("[Telnet][%s] ← password prompt (%d B): %r", ap.id, len(pw_prompt), pw_prompt[-80:])

            log.debug("[Telnet][%s] → sending password", ap.id)
            writer.write((ap.password + "\n").encode())
            await writer.drain()

            log.debug("[Telnet][%s] Waiting for main prompt %r …", ap.id, self._prompt_main)
            shell = await self._read_until(reader, self._prompt_main.encode(), 5.0)
            log.debug("[Telnet][%s] ← shell ready (%d B): %r", ap.id, len(shell), shell[-120:])

            self._sessions[ap.host] = (reader, writer)
            log.info("[Telnet][%s] ✓ Session established at %s:23", ap.id, ap.host)
        except Exception as exc:
            log.warning("[Telnet][%s] ✗ Could not connect to %s:23 — %s: %s", ap.id, ap.host, type(exc).__name__, exc)

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
        if not self._aps:
            log.error("[Stream] No APs configured — cannot stream. Returning immediately.")
            return

        cycle_num = 0
        while True:
            cycle_num += 1
            cycle_start = asyncio.get_running_loop().time()
            log.debug("[Stream] ── Cycle %d — polling %d AP(s) ──", cycle_num, len(self._aps))

            tasks = [self._poll_one(ap) for ap in self._aps]
            results_list = await asyncio.gather(*tasks, return_exceptions=True)

            rssi_map: RSSIMap = {}
            for ap, result in zip(self._aps, results_list):
                if isinstance(result, Exception):
                    log.warning("[Stream] Cycle %d: poll raised for %s: %s", cycle_num, ap.id, result)
                    continue
                if result:
                    rssi_map[ap.id] = result
                    log.debug("[Stream] Cycle %d: AP %s contributed: %s", cycle_num, ap.id, result)
                else:
                    log.debug("[Stream] Cycle %d: AP %s returned no matching targets", cycle_num, ap.id)

            elapsed = asyncio.get_running_loop().time() - cycle_start
            if rssi_map:
                log.debug("[Stream] Cycle %d done (%.2f s) — yielding combined map: %s", cycle_num, elapsed, rssi_map)
                yield rssi_map
            else:
                log.debug("[Stream] Cycle %d done (%.2f s) — no data this cycle, not yielding", cycle_num, elapsed)

            await asyncio.sleep(max(0.0, self._poll_interval - elapsed))

    async def _reset_to_root(self, ap_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        Send exit commands until we reach the root prompt.
        Safe to call from any navigation depth (wless2/network>, wless2>, root).
        Each exit times out silently if we over-shoot past root.
        """
        for _ in range(3):  # at most 3 levels deep
            writer.write(b"exit\n")
            await writer.drain()
            buf = await self._read_until(reader, self._prompt_main.encode(), 2.0)
            if self._prompt_main.encode() in buf:
                log.debug("[Poll][%s] Back at root prompt", ap_id)
                return
        log.debug("[Poll][%s] Could not confirm root prompt after exits (non-fatal)", ap_id)

    async def _poll_one(self, ap: AccessPoint) -> Optional[Dict[str, int]]:
        for attempt in range(2):
            session = self._sessions.get(ap.host)
            if not session:
                log.debug("[Poll][%s] No existing session — opening new one (attempt %d)", ap.id, attempt + 1)
                await self._open_session(ap)
                session = self._sessions.get(ap.host)
            if not session:
                log.warning("[Poll][%s] Session unavailable — skipping this AP", ap.id)
                return None

            reader, writer = session
            try:
                # ── ensure we are at root before navigating ───────────────
                # Probe the current prompt by sending a blank line.
                writer.write(b"\n")
                await writer.drain()
                probe = await self._read_until(reader, b">", 2.0)
                probe_text = probe.decode(errors="ignore")
                log.debug("[Poll][%s] Prompt probe: %r", ap.id, probe_text[-80:])

                # If we are not at root (e.g. still in wless2/network> from a
                # previous cycle), navigate back before proceeding.
                if self._prompt_sub.encode() in probe:
                    log.debug("[Poll][%s] Session in sub-context — resetting to root first", ap.id)
                    await self._reset_to_root(ap.id, reader, writer)
                elif self._prompt_main.encode() not in probe:
                    # Unknown context — try to exit back to root anyway
                    log.debug("[Poll][%s] Unknown context, attempting reset to root", ap.id)
                    await self._reset_to_root(ap.id, reader, writer)

                # ── navigate: root → wless2 ──────────────────────────────
                log.debug("[Poll][%s] → wless2", ap.id)
                writer.write(b"wless2\n")
                await writer.drain()
                nav1 = await self._read_until(reader, b">", 3.0)
                log.debug("[Poll][%s] ← wless2 nav (%d B): %r", ap.id, len(nav1), nav1[-120:])

                # ── navigate: wless2 → network ───────────────────────────
                log.debug("[Poll][%s] → network", ap.id)
                writer.write(b"network\n")
                await writer.drain()
                nav2 = await self._read_until(reader, self._prompt_sub.encode(), 3.0)
                log.debug("[Poll][%s] ← network nav (%d B): %r", ap.id, len(nav2), nav2[-120:])

                # ── run apscan ────────────────────────────────────────────
                log.debug("[Poll][%s] → apscan (waiting up to 8 s for table …)", ap.id)
                writer.write(b"apscan\n")
                await writer.drain()
                raw = await self._read_until(reader, self._prompt_sub.encode(), 8.0)
                raw_text = raw.decode(errors="ignore")

                log.debug(
                    "[Poll][%s] ← apscan raw response (%d B):\n%s",
                    ap.id, len(raw_text),
                    raw_text if raw_text.strip() else "(empty)"
                )

                # ── navigate back to root before next cycle ───────────────
                # Do this before parsing/returning so the session is always
                # left at root regardless of how we exit this code path.
                await self._reset_to_root(ap.id, reader, writer)

                if len(raw_text) < 50:
                    raise ValueError(f"Insufficient data ({len(raw_text)}B) — raw: {repr(raw_text)}")

                # ── parse ─────────────────────────────────────────────────
                log.debug("[Poll][%s] Parsing apscan table (targets=%s) …", ap.id, sorted(self._targets))
                loop = asyncio.get_running_loop()
                rows = await loop.run_in_executor(
                    self._executor,
                    self._parser.parse,
                    raw_text
                )
                log.debug("[Poll][%s] Parser returned %d row(s): %s", ap.id, len(rows), rows)

                results: Dict[str, int] = {}
                for row in rows:
                    ssid   = row.get("ssid", "")
                    signal = row.get("signal", "?")
                    if ssid in self._targets:
                        try:
                            results[ssid] = int(signal)
                            log.debug("[Poll][%s] ✓ Target SSID %r → signal %s dBm", ap.id, ssid, signal)
                        except (KeyError, ValueError):
                            log.warning("[Poll][%s] Could not cast signal %r to int for SSID %r", ap.id, signal, ssid)
                    else:
                        log.debug("[Poll][%s]   (skip) SSID %r signal %s — not a target", ap.id, ssid, signal)

                log.debug("[Poll][%s] Poll complete — matched targets: %s", ap.id, results)
                return results

            except Exception as exc:
                log.warning("[Poll][%s] Attempt %d failed (%s: %s) — dropping session", ap.id, attempt + 1, type(exc).__name__, exc)
                try:
                    self._sessions[ap.host][1].close()
                except Exception:
                    pass
                self._sessions.pop(ap.host, None)
                if attempt == 0:
                    log.debug("[Poll][%s] Sleeping 0.5 s before retry …", ap.id)
                    await asyncio.sleep(0.5)

        log.warning("[Poll][%s] Both attempts exhausted — returning empty result", ap.id)
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