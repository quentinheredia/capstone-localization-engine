import asyncio
import logging

import data_pipes
from models import AccessPoint

log = logging.getLogger(__name__)


DEFAULT_IP = "192.168.1.1"
DEFAULT_LOGIN = {"username": "admin", "password": "admin"}

AP_MODELS = ["EAP350", "EWS360", "EAP1250"]


async def write_command(command, session):
    """Write a single CLI command and wait for the main prompt."""
    reader, writer = session
    try:
        writer.write(command.encode() + b"\n")
        await writer.drain()
        await _read_until(reader, _PROMPT_MAIN.encode(), 3.0)
    except Exception as exc:
        log.warning("write_command: failed to send %r: %s", command, exc)


async def connect(aps: list[AccessPoint]) -> dict:
    """Open Telnet sessions to all configured APs concurrently.

    Returns a dict mapping ap.host -> (reader, writer) session tuple.
    """
    sessions = {}
    tasks = [_open_session(ap, sessions) for ap in aps]
    await asyncio.gather(*tasks, return_exceptions=True)
    return sessions


async def _open_session(ap: AccessPoint, sessions: dict) -> None:
    """Establish a Telnet session with a single AP and store it in sessions."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ap.host, 23), timeout=5.0
        )
        await _read_until(reader, b"login:", 5.0)
        writer.write((ap.username + "\n").encode())
        await writer.drain()

        await _read_until(reader, b"Password:", 5.0)
        writer.write((ap.password + "\n").encode())
        await writer.drain()

        await _read_until(reader, _PROMPT_MAIN.encode(), 5.0)
        sessions[ap.host] = (reader, writer)
        log.info("TelnetPipe: connected to %s", ap.host)
    except Exception as exc:
        log.warning("TelnetPipe: could not connect to %s: %s", ap.host, exc)


async def configure(session) -> None:
    """Send provisioning commands to an already-connected AP."""
    await write_command("wless2", session)
    # TODO: add further configuration commands as needed


async def close(sessions: dict) -> None:
    """Close all open Telnet sessions."""
    for host, (reader, writer) in list(sessions.items()):
        try:
            writer.close()
            await writer.wait_closed()
        except Exception as exc:
            log.warning("TelnetPipe: error closing session for %s: %s", host, exc)
    sessions.clear()


def provision(model, ip=DEFAULT_IP, login=DEFAULT_LOGIN):
    """Provision an EnGenius AP at the given IP address."""
    pipe = data_pipes.EnGeniusPipe(ip, login["username"], login["password"])
    # TODO: connect, configure, and close using the async helpers above


# ── Internal helpers ──────────────────────────────────────────────────────────

_PROMPT_MAIN = "#"


async def _read_until(reader, pattern: bytes, timeout: float) -> bytes:
    """Read from reader until pattern is found, with a timeout."""
    buf = b""
    async def _read():
        nonlocal buf
        while pattern not in buf:
            chunk = await reader.read(1024)
            if not chunk:
                break
            buf += chunk
        return buf
    return await asyncio.wait_for(_read(), timeout=timeout)
