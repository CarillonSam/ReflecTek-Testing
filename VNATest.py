import time
import numpy as np
import pyvisa

rm = None
instr = None


def _ensure_open():
    global rm, instr
    if instr is None:
        rm = pyvisa.ResourceManager()
        instr = rm.open_resource("TCPIP0::192.168.6.150::inst0::INSTR")
        instr.timeout = 100000


def check_response(label):
    _ensure_open()
    r = instr.query("*OPC?")
    print(r)
    print(f"[OK] {label}")
    print("ERRORS", instr.query("SYST:ERR?"))


def init(start, stop, points):
    _ensure_open()
    print(instr.query("*IDN?").strip())
    time.sleep(1)
    instr.write("LSB;FMB")
    time.sleep(1)
    instr.write(f"SENS1:FREQ:START {start}")
    time.sleep(1)
    instr.write(f"SENS1:FREQ:STOP {stop}")
    time.sleep(1)
    instr.write(f":SENS1:SWE:POIN {points}")
    time.sleep(1)
    instr.write(":CALC1:PAR1:DEF S11")
    time.sleep(2)
    instr.write(":SENS:HOLD:FUNC HOLD")


def trigger():
    _ensure_open()
    instr.write(":TRIG:SING; *OPC?")
    instr.read()
    print("Querying...")
    sdata = instr.query_binary_values(
        ":CALC1:DATA:SDAT?",
        datatype="d",
        container=np.array
    ).reshape((-1, 2))
    sdata = sdata[:, 0] + 1j * sdata[:, 1]
    print("Received response.")
    return sdata


def close():
    global instr, rm
    try:
        if instr is not None:
            instr.close()
    except Exception:
        pass
    try:
        if rm is not None:
            rm.close()
    except Exception:
        pass
    instr = None
    rm = None
