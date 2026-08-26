"""ModbusTcpAdapter: register grouping/slicing/decoding and comm-fail behaviour,
using a fake pymodbus client (no sockets)."""
import math
import struct
from datetime import datetime, timezone

from aqms_logger.adapters.modbus_tcp import ModbusTcpAdapter
from aqms_logger.config import DeviceConfig, RegisterSpec
from aqms_logger.models import STATUS_COMM_FAIL, STATUS_VALID


def _words(value: float) -> tuple[int, int]:
    hi, lo = struct.unpack(">HH", struct.pack(">f", value))
    return hi, lo


# mg/m3 source values for the PM channels; engineering (×1000) expectations in ug/m3.
# The adapter reads the whole contiguous span 37..66 in one request, so the fake
# register memory must cover every address in that range (gaps default to 0).
FIDAS_MEM = {a: 0 for a in range(36, 70)}
_VALUES = {
    37: 12345.0, 39: 0.005, 41: 0.01234, 43: 0.02, 45: 0.045,
    47: 0.05, 49: 28.5, 51: 1008.2, 53: 65.3, 61: 3.6, 63: 270.0, 65: 0.0,
}
for _addr, _val in _VALUES.items():
    FIDAS_MEM[_addr], FIDAS_MEM[_addr + 1] = _words(_val)


class _Resp:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self._error = error

    def isError(self):
        return self._error


class _FakeClient:
    def __init__(self, mem=None, fail=False):
        self.mem = mem or {}
        self.fail = fail

    def connect(self):
        return True

    def close(self):
        pass

    def read_input_registers(self, address, **kwargs):
        if self.fail:
            return _Resp(error=True)
        count = kwargs["count"]
        try:
            return _Resp([self.mem[address + i] for i in range(count)])
        except KeyError:
            return _Resp(error=True)

    read_holding_registers = read_input_registers


def _fidas_device():
    regs = [
        RegisterSpec("Cn", 37, unit="P/cm3"),
        RegisterSpec("PM1", 39, scale=1000, unit="ug/m3"),
        RegisterSpec("PM2_5", 41, scale=1000, unit="ug/m3"),
        RegisterSpec("PM4", 43, scale=1000, unit="ug/m3"),
        RegisterSpec("PM10", 45, scale=1000, unit="ug/m3"),
        RegisterSpec("TSP", 47, scale=1000, unit="ug/m3"),
        RegisterSpec("TEMP", 49, unit="degC"),
        RegisterSpec("PRESSURE", 51, unit="hPa"),
        RegisterSpec("HUMIDITY", 53, unit="%"),
        RegisterSpec("WSP", 61, unit="km/h"),
        RegisterSpec("WDIR", 63, unit="deg"),
        RegisterSpec("PRECIP", 65, unit="l/m2/h"),
    ]
    return DeviceConfig(name="fidas200", type="modbus_tcp", host="x", port=502,
                        unit_id=1, word_order="ABCD", registers=regs)


def test_decodes_all_channels():
    adapter = ModbusTcpAdapter(_fidas_device(), "TEST")
    adapter.client = _FakeClient(FIDAS_MEM)
    out = {r.parameter: r for r in adapter.read(datetime.now(timezone.utc))}

    assert all(r.status == STATUS_VALID for r in out.values())
    assert math.isclose(out["PM2_5"].value, 12.34, rel_tol=1e-4)   # 0.01234 mg × 1000
    assert math.isclose(out["PM10"].value, 45.0, rel_tol=1e-4)
    assert math.isclose(out["TEMP"].value, 28.5, rel_tol=1e-4)
    assert math.isclose(out["PRESSURE"].value, 1008.2, rel_tol=1e-4)
    assert out["PM2_5"].unit == "ug/m3"


def test_comm_failure_marks_all_gap():
    adapter = ModbusTcpAdapter(_fidas_device(), "TEST")
    adapter.client = _FakeClient(fail=True)
    out = adapter.read(datetime.now(timezone.utc))

    assert len(out) == 12
    assert all(r.value is None and r.status == STATUS_COMM_FAIL for r in out)
