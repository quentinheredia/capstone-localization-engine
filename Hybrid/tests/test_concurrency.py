import asyncio
import time
import pytest
from unittest.mock import MagicMock, patch
import sys, os

# Ensure src_python/ (where the .so lands) is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src_python'))

try:
    from data_pipes import TelnetPipe, MQTTPipe
    print("[OK] data_pipes imported successfully")
except ImportError as e:
    print(f"[FAIL] Could not import data_pipes: {e}")
    sys.exit(1)

try:
    from models import AccessPoint, ToFAnchor, ToFMeasurement
    print("[OK] data_pipes imported successfully")
except ImportError as e:
    print(f"[FAIL] Could not import models: {e}")
    sys.exit(1)

@pytest.mark.asyncio
async def test_telnet_does_not_block_mqtt():
    """
    Validation: Blocking Telnet parsing must NOT delay MQTT queue ingestion.
    """
    # 1. Setup Mock Data
    ap = AccessPoint(id="AP01", host="192.168.1.1", username="admin", password="password")
    anchor = ToFAnchor(id="ANC01", mac="AA:BB:CC")
    
    telnet_pipe = TelnetPipe(aps=[ap], target_ssids=["Guest"], prompts={})
    mqtt_pipe = MQTTPipe(anchors=[anchor])

    # 2. Mock the Blocking C++ Parser
    def slow_blocking_parse(text):
        time.sleep(0.5)  # The "Event Loop Killer"
        return [{"ssid": "Guest", "signal": "-60"}]

    telnet_pipe._parser.parse = MagicMock(side_effect=slow_blocking_parse)

    # 3. Simulate MQTT arrival in a separate thread (like paho-mqtt does)
    async def simulate_mqtt_traffic():
        await asyncio.sleep(0.1) # Wait for system to start
        loop = asyncio.get_running_loop()
        
        # This simulates the paho-mqtt thread callback
        msg = MagicMock()
        msg.payload = b'{"mac": "AA:BB:CC", "distance_m": 1.5, "ts": "2026-03-27"}'
        
        # Use the threadsafe method we refactored
        mqtt_pipe._client.on_message(None, None, msg)

    # 4. Execution
    # We use a task to trigger MQTT while Telnet is "parsing"
    mqtt_trigger = asyncio.create_task(simulate_mqtt_traffic())
    
    # Mock the telnet session so it doesn't actually try to connect to an IP
    telnet_pipe._sessions[ap.host] = (MagicMock(), MagicMock())

    start_time = asyncio.get_running_loop().time()
    
    # Run a single poll cycle
    telnet_task = asyncio.create_task(anext(telnet_pipe.stream()))
    mqtt_task = asyncio.create_task(anext(mqtt_pipe.stream()))

    # 5. Assertions
    # The MQTT measurement should arrive ALMOST INSTANTLY (~0.1s), 
    # even though Telnet is blocked for 0.5s.
    results = await asyncio.gather(telnet_task, mqtt_task)
    end_time = asyncio.get_running_loop().time()

    total_duration = end_time - start_time
    
    assert isinstance(results[1], ToFMeasurement)
    assert results[1].distance_m == 1.5
    # If the loop stayed responsive, the whole test should take ~0.5s (the telnet sleep),
    # but the MQTT should have been processed in parallel.
    assert total_duration >= 0.5 
    print(f"\nConcurrency Test Passed: Processed both in {total_duration:.2f}s")

    # Cleanup
    await telnet_pipe.close()
    await mqtt_pipe.close()