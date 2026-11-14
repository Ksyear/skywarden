import socket
import time

import pytest

from adapters.tello.client import HardwareDisarmed, TelloClient, TelloTimeout, hardware_enabled
from adapters.tello.mock_tello import MockTello
from adapters.tello.protocol import ProtocolError, format_rc, format_state, parse_state

ALL_COMMANDS = ["command", "streamon", "takeoff", "land", "emergency", "rc 0 0 0 30", "cw 90", "battery?", "mon"]


class CountingSocket:
    def __init__(self):
        self.sent = 0

    def sendto(self, *a):
        self.sent += 1

    def settimeout(self, *_):
        pass

    def recvfrom(self, *_):
        raise socket.timeout

    def close(self):
        pass


@pytest.mark.parametrize("env", [{}, {"HARDWARE_ARMED": "true"}, {"DRY_RUN": "false"}, {"HARDWARE_ARMED": "TRUE", "DRY_RUN": "true"}])
def test_real_drone_address_gets_zero_packets_unless_fully_armed(env):
    sock = CountingSocket()
    c = TelloClient(("192.168.10.1", 8889), sock=sock, env=env)
    assert not c.simulated and not hardware_enabled(env)
    for cmd in ALL_COMMANDS:
        with pytest.raises(HardwareDisarmed):
            c.command(cmd, timeout=0.01)
    with pytest.raises(HardwareDisarmed):
        c.rc(0, 0, 0, 20)
    assert sock.sent == 0 and c.sent == []


def test_default_environment_is_unarmed(monkeypatch):
    monkeypatch.delenv("HARDWARE_ARMED", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert hardware_enabled() is False


def test_state_roundtrip_and_validation():
    s = {"mid": 3, "x": 10, "y": -20, "z": 80, "mpry": (0, 1, 2), "pitch": 0, "roll": 1, "yaw": -45, "vgx": 0, "vgy": 0,
         "vgz": 0, "templ": 60, "temph": 63, "tof": 50, "h": 80, "bat": 71, "baro": 120.5, "time": 12, "agx": 1.0,
         "agy": 0.0, "agz": -999.0}
    p = parse_state(format_state(s).encode())
    assert p["yaw"] == -45 and p["mpry"] == (0, 1, 2) and p["mission_pad_valid"] is True
    assert parse_state(format_state({**s, "mid": -1}))["mission_pad_valid"] is False
    for bad in [b"bat:x;yaw:0;", b"yaw:0;", b"garbage", b"\xff\xfe"]:
        with pytest.raises((ProtocolError, UnicodeDecodeError)):
            parse_state(bad)


def test_rc_values_are_clamped():
    assert format_rc(-300, 5.4, 0, 101) == "rc -100 5 0 100"


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_mock_tello_command_flow_and_state_stream():
    port = free_udp_port()
    with MockTello(state_port=port, battery=55) as mock:
        c = TelloClient(mock.addr, state_port=port)
        c.start_state_listener()
        assert c.simulated
        with pytest.raises(TelloTimeout):  # like the real SDK: ignored until "command" enables SDK mode
            c.command("battery?", 0.3)
        assert c.command("command") == "ok"
        assert c.command("battery?") == "55"
        assert c.command("takeoff") == "ok"
        c.rc(0, 0, 0, 40)
        time.sleep(0.6)
        st = c.latest_state()
        assert st is not None and st.data["h"] == 80 and st.data["yaw"] > 5
        assert c.command("flip x") .startswith("error")
        assert c.command("land") == "ok"
        c.close()
        assert mock.received[:3] == ["battery?", "command", "battery?"]


def test_dropped_reply_raises_timeout():
    with MockTello(state_port=free_udp_port(), drop_prob=1.0) as mock:
        c = TelloClient(mock.addr)
        with pytest.raises(TelloTimeout):
            c.command("command", timeout=0.3)
        c.close()


def test_late_reply_is_not_taken_as_answer_to_next_command():
    with MockTello(state_port=free_udp_port(), latency_s=0.4, battery=63) as mock:
        c = TelloClient(mock.addr)
        with pytest.raises(TelloTimeout):
            c.command("command", timeout=0.15)  # its "ok" arrives later
        time.sleep(0.5)
        assert c.command("battery?", timeout=1.5) == "63"
        c.close()


def test_mock_integrates_small_yaw_rates():
    port = free_udp_port()
    with MockTello(state_port=port) as mock:
        c = TelloClient(mock.addr, state_port=port)
        c.start_state_listener()
        assert c.command("command") == "ok" and c.command("takeoff") == "ok"
        c.rc(0, 0, 0, 5)  # 5 deg/s: below one degree per state tick
        t0 = time.monotonic()
        time.sleep(1.2)
        while c.latest_state().data["yaw"] < 4 and time.monotonic() - t0 < 5:  # slow runners deliver ticks late
            time.sleep(0.05)
        assert c.latest_state().data["yaw"] >= 4
        assert c.latest_state().data["yaw"] <= 5 * (time.monotonic() - t0) + 1  # integrated, not jumped
        c.close()


def test_mock_altitude_profile_and_telemetry_recorder(tmp_path):
    from adapters.tello.telemetry import TelemetryRecorder, height_at, read_telemetry
    port = free_udp_port()
    profile = [(0.0, 0.8), (0.6, 1.2), (1.2, 1.2)]
    with MockTello(state_port=port, altitude_profile=profile, tof_offset_cm=7) as mock:
        c = TelloClient(mock.addr, state_port=port)
        c.start_state_listener()
        rec = TelemetryRecorder(c, tmp_path / "telemetry.jsonl").start()
        assert c.command("command") == "ok" and c.command("takeoff") == "ok"
        time.sleep(1.0)
        t_mid = mock.takeoff_t + 0.3
        rec.stop()
        c.close()
        assert mock.true_height_m(t_mid) == pytest.approx(1.0)
    rows = read_telemetry(tmp_path / "telemetry.jsonl")
    assert rows == rec.samples and len(rows) >= 5
    flying = [r for r in rows if r["h"] > 0]
    assert all(r["tof"] == r["h"] + 7 for r in flying)
    assert max(r["h"] for r in flying) == 120 and min(r["h"] for r in flying) >= 80
    assert height_at(rows, rows[-1]["t"] + 1.0) is None  # never extrapolated


def test_height_scale_recovers_known_scale_and_stays_a_cross_check(tmp_path):
    import json

    import numpy as np

    from adapters.tello.height_scale_check import TRUE_MPU, synthetic_map
    from mapping import metric_scale
    heights = np.linspace(0.8, 1.2, 30)
    m = synthetic_map(heights, seed=4)
    plane = metric_scale.ground_plane_ransac(m["points"], seed=0)
    assert 0.75 <= plane["inlier_ratio"] <= 0.9  # floor found, clutter rejected
    tel = np.round(heights * 100) / 100  # whole-cm telemetry
    res = metric_scale.scale_from_height(m["centres"], tel, plane)
    assert abs(res["metres_per_unit"] / TRUE_MPU - 1) <= 0.01
    # stored only as a cross-check: alone it gives no scale, next to a measurement it only reports the difference
    sd = tmp_path / "s"
    sd.mkdir()
    (sd / "trajectory.json").write_text(json.dumps({"poses": []}))
    s = metric_scale.set_cross_check(sd, "TELLO_TELEMETRY", res["metres_per_unit"], note="MOCK")
    assert s["status"] == "NONE" and s["metres_per_unit"] is None
    s = metric_scale.add_two_point(sd, [0, 0, 0], [10, 0, 0], 10 * TRUE_MPU * 1.2, "fit", note="tape")
    assert s["status"] == "MEASURED_UNVERIFIED" and s["primary_method"] == "TWO_POINT_TAPE"
    assert s["metres_per_unit"] == pytest.approx(TRUE_MPU * 1.2)
    tc = next(c for c in s["cross_checks"] if c["method"] == "TELLO_TELEMETRY")
    assert tc["diff_vs_primary_pct"] == pytest.approx(-16.67, abs=0.1) and "TELEMETRY_DISAGREES" in s["flags"]


def test_height_flow_sends_nothing_off_loopback(tmp_path):
    from adapters.tello.height_scale_check import SendAudit, fly_profile
    with SendAudit() as audit:
        flight = fly_profile([(0.0, 0.8), (0.5, 1.0)], tmp_path / "t.jsonl", settle_s=0.1)
        sock = CountingSocket()
        real = TelloClient(("192.168.10.1", 8889), sock=sock, env={})
        with pytest.raises(HardwareDisarmed):
            real.command("takeoff", timeout=0.01)
    assert flight["telemetry"] and audit.loopback > 0
    assert audit.other == [] and sock.sent == 0
