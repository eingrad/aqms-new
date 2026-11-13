# probe.py
from pymodbus.client import ModbusTcpClient
try:
    from pymodbus.framer import FramerType
    FRAMERS = {"tcp": FramerType.SOCKET, "rtu": FramerType.RTU}
except ImportError:  # older pymodbus 3.x
    from pymodbus.framer import ModbusSocketFramer, ModbusRtuFramer
    FRAMERS = {"tcp": ModbusSocketFramer, "rtu": ModbusRtuFramer}

IP = "192.168.13.123"
for fname, framer in FRAMERS.items():
    for uid in (1, 0, 2, 247):
        c = ModbusTcpClient(IP, port=502, framer=framer, timeout=2, retries=0)
        c.connect()
        for fc, fn in (("IR", c.read_input_registers), ("HR", c.read_holding_registers)):
            try:
                try:
                    r = fn(0, count=2, slave=uid)
                except TypeError:
                    r = fn(0, count=2, device_id=uid)  # pymodbus 3.10+
                print(f"{fname} uid={uid} {fc}: {r}")
            except Exception as e:
                print(f"{fname} uid={uid} {fc}: {type(e).__name__}")
        c.close()