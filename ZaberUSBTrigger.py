# -*- coding: utf-8 -*-
"""
Created on Mon May  4 12:30:07 2026

@author: SpecVision
"""

import numpy as np
from zaber_motion import Units
from zaber_motion.ascii import Connection
import pyvisa
import time
import matplotlib.pyplot as plt
# from gpiozero import OutputDevice
import serial
from datetime import datetime
from ElementController3000 import App 

class UsbTrigger:
    def __init__(self, port, pulse_duration=0.002):
        self.port = port
        self.pulse_duration = pulse_duration
        self.ser = serial.Serial(port, baudrate=921600)
        self.ser.setRTS(False)
        self.ser.setDTR(False)  # ensure low at start
    
    def pulse(self):
        # Drive TXD high by sending a break-free byte
        self.ser.write(b'\x00')  # TXD pulses with data
    
        
    def close(self):
        # self.ser.setRTS(False)
        self.ser.close()

class VnaInstance:
    """
    In NSI Max software on VNA, in system settings, make sure System Configuration Web Access is set to Local and Remote
    """
    def __init__(self, ip_addr):
        self.ip_addr = ip_addr
        self.rm = pyvisa.ResourceManager('@py')
        self.instr = None
    def connect(self):
        self.instr = self.rm.open_resource(self.ip_addr)
        self.instr.timeout = 60000 #60 seconds
        #print("VNA ID:", self.instr.query("*IDN?").strip())
    def disconnect(self):
        if self.instr is not None:
            try:
                #self.instr.control_ren(6) # go to local mode
                #self.instr.write("\x1B")
                self.instr.close()
            except:
                pass
            self.instr = None
    def setup_single_sweep(self, start, stop, points):
        meas = "S21"
    
        self.instr.write("SYST:PRES")
        self.instr.write("*CLS")
        self.instr.write("*RST")
        self.instr.write("*OPC")
    
        #self.instr.write("DISP:WIND1:STAT ON")
        self.instr.write(f"CALC1:PAR:DEF:EXT 'Meas1',{meas}")
        #self.instr.write("DISP:WIND1:TRAC1:FEED 'Meas1'")
        self.instr.write("CALC1:PAR:SEL 'Meas1'")
    
        self.instr.write("SENS1:SWE:TYPE LIN")
        self.instr.write(f"SENS1:FREQ:STAR {start}")
        self.instr.write(f"SENS1:FREQ:STOP {stop}")
        self.instr.write(f"SENS1:SWE:POIN {points}")
    
        self.instr.write("INIT1:CONT OFF")
        self.instr.write("SENS1:SWE:MODE SING")
        
        self.instr.write("FORM:DATA REAL,64")
        self.instr.write("FORM:BORD SWAP")
        
        self.instr.write(":TRIG:SOUR EXT")                 # external trigger
        self.instr.query("*OPC?")
        
        print("Sweep type:", self.instr.query("SENS1:SWE:TYPE?").strip())
        print("Start Hz  :", self.instr.query("SENS1:FREQ:STAR?").strip())
        print("Stop Hz   :", self.instr.query("SENS1:FREQ:STOP?").strip())
        print("Points    :", self.instr.query("SENS1:SWE:POIN?").strip())
        print("Selected  :", self.instr.query("CALC1:PAR:SEL?").strip())
        print("Error     :", self.instr.query("SYST:ERR?").strip())
    def run_and_read_one_sweep(self):
        t0 = time.perf_counter()

        self.instr.write(":INIT1:IMM")             # start single sweep
        self.instr.query("*OPC?")                  # blocks until operation complete

        raw = self.instr.query_binary_values(
            "CALC1:DATA? SDATA",
            datatype="d",
            is_big_endian=False,
            container=np.array,
        )
    
        #print("raw size:", raw.size)
    
        if raw.size % 2 != 0:
            raise RuntimeError(f"Unexpected raw data length: {raw.size}")
    
        sdata = raw.reshape((-1, 2))
        
        t1 = time.perf_counter()
        return t1 - t0, 20*np.log10(np.abs(sdata[:, 0] + 1j * sdata[:, 1]))
        
    def setup_cw_time_sweep(self, FREQ_HZ, POINTS, IFBW_HZ):
            self.POINTS = POINTS
            self.IFBW_HZ = IFBW_HZ
            self.SAMPLE_OVERHEAD = .1111111e-3  # seconds

            self.sample_interval = (1 / self.IFBW_HZ) + self.SAMPLE_OVERHEAD
            self.sweep_time = self.sample_interval * self.POINTS
            self.sample_rate = 1.0 / self.sample_interval
            self.time_axis = np.linspace(0, self.sweep_time, self.POINTS)

            self.instr.write("*CLS")

            self.instr.write(":SENS1:SWE:CW 1") #Turn on CW mode for CH1
            self.instr.write(f":SENS1:FREQ:CW {FREQ_HZ}")      # CW frequency
            self.instr.write(f":SENS1:BAND {self.IFBW_HZ}")    # IFBW
            self.instr.write(f":SENS1:SWE:CW:POIN {self.POINTS}") # number of points
            
            self.instr.write(":FORM:DATA REAL")

            self.instr.write(":CALC1:PAR1:DEF S21")            # S21 measurement
            self.instr.write(":CALC1:PAR1:SEL")                # select trace 1
            self.instr.query("*OPC?")
            #print(f"Calculated sweep time: {self.sweep_time:.3f} s")
            #print(f"Sample interval: {self.sample_interval * 1000:.4f} ms")
            #print(f"Sample rate: {self.sample_rate:.1f} Hz")
            #print("Error:", self.instr.query("SYST:ERR?").strip())
            
    def set_hold_single(self):
            self.instr.write(":SENS1:HOLD:FUNC SING")   
            self.instr.write(":TRIG:SOUR EXT")       # single sweep on trigger
            self.instr.query("*OPC?")
        
    def start_running(self, pin):
        pin.pulse()
        

    def stop_and_read_complex_data(self):
        # Stop continuous sweeping / hold current trace
        
        #Hold sweep on channel 1 
        self.instr.write(":SENS1:HOLD:FUNC HOLD")
        time.sleep(0.1)
    
        # Read complex real/imaginary data
        raw = self.instr.query_binary_values(
            ":CALC1:DATA:SDATA?",
            datatype="d",
            is_big_endian=False,
            container=np.array
        )
        print(f"Got {len(raw)} values, expected {self.POINTS *2}")
        data = np.array(raw).reshape(-1, 2)
        complex_s = data[:, 0] + 1j * data[:, 1]
        phase = np.round(np.angle(complex_s, deg=True), 2)
        magnitude = 20*np.log10(np.abs(complex_s))
        
        return phase, magnitude
    


class Zaber:
    def __init__(self, port):
        self.port = port
        self.max_angle_deg = 70 #placeholder 
        self.min_step_size_deg = .1 #placeholder
        self.speed = 15 #deg/s
        self.accel = 200 #deg/s/s
        self._connect()
        self._init_zaber()
    def _connect(self):
        self.connection = Connection.open_serial_port(self.port)
        device_list = self.connection.detect_devices()
        print("Found {} devices".format(len(device_list)))
        self.device = device_list[0]
        self.axis = self.device.get_axis(1)
    def disconnect(self):
        self.axis.home()
        self.connection.close()
    def _init_zaber(self):
        #if not self.axis.is_homed():
        self.device.settings.set("system.access", 2, Units.NATIVE)

        self.axis.settings.set("limit.max", 180, Units.ANGLE_DEGREES)
        self.axis.settings.set("limit.min", -180, Units.ANGLE_DEGREES)
        
        self.axis.settings.set("accel", self.accel, Units.ANGULAR_ACCELERATION_DEGREES_PER_SECOND_SQUARED)
        self.axis.settings.set("maxspeed", self.speed, Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND)
        self.axis.settings.set("limit.approach.maxspeed", self.speed, Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND)
        self.axis.home()
        #self.move_abs(90)
    
    def move_abs(self, move_deg, wait_until_idle):
        self.axis.move_absolute(move_deg, Units.ANGLE_DEGREES, wait_until_idle)
    def move_rel(self, move_deg, wait_until_idle):
        self.axis.move_relative(move_deg, Units.ANGLE_DEGREES, wait_until_idle)
    def get_angles(self, span_deg, center_deg, num_points):
        user_max_angle = span_deg/2 + np.abs(center_deg)
        user_step_size = span_deg/num_points
        if user_max_angle > self.max_angle_deg:
            raise ValueError(f"Max angle must be less than +/- {self.max_angle_deg}°, current={user_max_angle}°")
        if user_step_size < self.min_step_size_deg:
            raise ValueError(f"Step size must be >= {self.min_step_size_deg}°, current = {user_step_size}°")
        start_angle_deg = (-span_deg/2)+center_deg
        stop_angle_deg = (span_deg/2)+center_deg
        return start_angle_deg, stop_angle_deg
        
def run_scan(zaber, vna, pin, span_deg, center_deg, num_angle_points, start_freq_ghz, stop_freq_ghz, num_freq_points): 
        magnitude = []
        phase = []
        position_axis = []
        speed = zaber.speed
        accel = zaber.accel
        
        start_angle_deg, stop_angle_deg = zaber.get_angles(span_deg, center_deg, num_angle_points)
        half_span = span_deg/2
        offset_angle_deg = (speed ** 2) / (2*accel) #angle offset to start and stop scan at so that the scan is at constant velocity over the span
        start_angle_real_deg = start_angle_deg - offset_angle_deg -1 #add 1 degree buffer
        stop_angle_real_deg = stop_angle_deg + offset_angle_deg +1 #add 1 degree buffer
        
        start_time_s = speed/accel 
        scan_time_s = span_deg/speed
        
        if (vna.sweep_time < scan_time_s):
                raise ValueError(f"Change IFBW or CW points so that VNA sweep time (curr: {vna.sweep_time}s) is greater than rotary scan time (curr: {scan_time_s}s)")
        
        vna.set_hold_single() #tells vna to wait for trigger
        
        #move to start point and wait
        zaber.move_abs(start_angle_real_deg, wait_until_idle = True)
        
        
        #start_vna = time.perf_counter()
        #start moving to stop point
        zaber.move_abs(stop_angle_real_deg, wait_until_idle = False)

        #Wait until scanner reaches start of scan position
        while True:
            start_position = zaber.axis.get_position(Units.ANGLE_DEGREES)
            if start_position >= start_angle_deg:
                start = time.perf_counter()
                break
    
        #start the vna measurment
        vna.start_running(pin)
        
        #wait until scan is complete
        while True:
            stop_position = zaber.axis.get_position(Units.ANGLE_DEGREES)
            if stop_position >= stop_angle_deg:
                stop = time.perf_counter()
                break
      
        python_time = stop-start
        #print(f"Scan time={python_time}s")
        phase, magnitude = vna.stop_and_read_complex_data()
        
        time_axis =  np.linspace(0, len(magnitude) * (vna.sample_interval), len(magnitude))
        time_axis = time_axis[(time_axis <= python_time)]

        position_axis = time_axis*speed + start_position

        position_axis = position_axis[position_axis <= stop_position]

        magnitude = magnitude[0:len(position_axis)]
        phase = phase[0:len(position_axis)]
        
        zaber.axis.home()

        return phase, magnitude, position_axis
        

def main(volt):
    port = "COM4"
    # port="/dev/ttyUSB0"
    vna_ip_addr = "TCPIP0::192.168.6.150::inst0::INSTR"
    
    span_deg = 120
    num_angle_points = 20
    center_deg = 0
    start_freq_ghz = 19.3e9
    stop_freq_ghz = 25e9
    num_freq_points = 1
    
    cw_points = 4000
    ifbw_hz = 200
    
    peak_loc = []
    zaber = None
    vna = None
    
    pin = UsbTrigger(port="COM3")#OutputDevice(16)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    try:
        vna = VnaInstance(vna_ip_addr)
        #vna=1
        vna.connect()
        vna.setup_cw_time_sweep(start_freq_ghz, cw_points, ifbw_hz)
        zaber = Zaber(port)
        for i in range(1):
                phase, magnitude, position_axis = run_scan(zaber, vna, pin, span_deg, center_deg, num_angle_points, start_freq_ghz, stop_freq_ghz, num_freq_points)
                peak_loc.append(position_axis[np.argmax(magnitude)])
                #time.sleep(3)
                
        np.savez_compressed(rf"C:\Users\uconn\Downloads\RadiationPatVsVoltage\2026-06-04\MagVsAz_{volt}V.npz", phase=phase, magnitude=magnitude, position_axis=position_axis)
        plt.figure()
        plt.plot(position_axis, magnitude)
        # plt.savefig(rf"C:\Users\uconn\Downloads\PartCoveredMeasurements\Optimized for Geometry\MagVsAz_LeftThird.png")
        plt.xlabel("Azimuth (°)")
        plt.ylabel("Magnitude (dB)")
        plt.title(f"Magnitude Vs Azimuth at {start_freq_ghz/1e9} Ghz")
        plt.ylim(-90, -50)
        plt.show()
    finally:
        if vna is not None:
             vna.disconnect()
        if zaber is not None:
            zaber.disconnect()
        pin.close()
    
if __name__ == "__main__":
    # INTERVAL_MINUTES = 10
    # TOTAL_HOURS = 4

    # interval = INTERVAL_MINUTES * 60
    # total_runs = (TOTAL_HOURS * 60) // INTERVAL_MINUTES

    # start_time = time.time()
    app = App()
    for i in range(11):
        time.sleep(60)
        voltages = voltages = np.full((64,32), i)
        app.send_voltages(voltages,lb_or_hb='lb')
        main(i)
        # next_run = start_time + (i + 1) * interval
        # sleep_for = next_run - time.time()
        # if sleep_for > 0:
        #     time.sleep(sleep_for)   
    
