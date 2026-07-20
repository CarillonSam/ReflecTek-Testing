# -*- coding: utf-8 -*-
"""
Created on Wed Feb  4 09:40:55 2026

@author: labuser
"""




import numpy as np
from Shared.PiController import PiController
from pathlib import Path
import VNATest
import time

PI_HOST = "192.168.6.2" #IP of PI controlling DACs
USERNAME = "carillon"         
PASSWORD = "password"          
KEY_FILE = None # if using an SSH key, set path like "C:/Users/you/.ssh/id_rsa"
PI_PORT = 22

STOP_FILE = r"/home/carillon/Downloads/STOP.txt"
LOCAL_FILE_HB = r"C:\Users\labuser\Documents\ReflecTekCalibrationScholl\Array-Calibration\src\DataHB.txt"   #HB voltage file to send to PI
LOCAL_FILE_LB = r"C:\Users\labuser\Documents\ReflecTekCalibrationScholl\Array-Calibration\src\DataLB.txt" #LB voltage file to send to PI
REMOTE_FILE_HB = r"/home/carillon/Desktop/DataHB.txt"  # where to put it on the Pi
REMOTE_FILE_LB = r"/home/carillon/Desktop/DataLB.txt"  # where to put it on the Pi
REMOTE_PROGRAM = r"/home/carillon/Downloads/temp.py"#Gen3DAC60096EVM_SPI_DualPatch.py" #Location of program on PI that updates DACs
# Command to run on the Pi once file is uploaded
REMOTE_COMMAND = f"python3 {REMOTE_PROGRAM}"

def update_lb_array_file(V):
    #V = np.round(V * DAC_MIN_STEP_SIZE, 3)
    with open(LOCAL_FILE_HB, "w") as f:#should be LOCAL_FILE_LB
        for row in V:
            line = ",".join(str(x) for x in row)
            f.write(line + "\n")
    #This program is for LB only, so create a 0V array for the high band which is 24x8 in Sam's code
    with open(LOCAL_FILE_LB, "w") as f:#should be LOCAL_FILE_HB
        for row in V:#np.zeros((24,8)):
            line = ",".join(str(x) for x in row)
            f.write(line + "\n")


    
def main():
    
    rpi = PiController(
        host=PI_HOST,
        username=USERNAME,
        password=PASSWORD,
        local_file_hb=LOCAL_FILE_HB,
        local_file_lb=LOCAL_FILE_LB,
        remote_file_hb=REMOTE_FILE_HB,
        remote_file_lb=REMOTE_FILE_LB,
        remote_command=REMOTE_COMMAND,
        port = PI_PORT,
        key_filename=KEY_FILE,
        stop_file = STOP_FILE,
    )
    rpi.connect()
    
    EXP_DIR = r"C:\Users\labuser\Gen3WGSDualPatchGraphs\AutomationTesting"
    experiment_dir = Path(EXP_DIR)
    v_arr = []
    amp_arr = []
    phase_arr = []
    
    #Parameters for the number of x and y coordinates
   
    
    #Parameter for the number of distinct voltages to be measered at
    volts = 7
    
    #Create empty arrays to store all of the measurements to be taken organized by x index, y index,  and voltage bias
    comp_arr = np.empty((volts, 5000), dtype = np.complex128)
    amp_arr =  np.empty((volts, 5000))
    phase_arr =  np.empty((volts, 5000))
    
    # Initialize VNA Scan Parmeters
    VNATest.init(str(24e9), str(34e9), str(5000))
    
    #Start for loop for the number of voltages given by the parameter 'volts' defined on line 396
    for i in range(volts):
        value = (i-1)*2.0
        if i == 0:
            value = 0
        voltages = np.full((24, 8), value)
        update_lb_array_file(voltages)
        rpi.update_dacs()
        print(f"Voltage {value}")
        time.sleep(60)
            
        #Start for loop for the number of x coordinates given by the parameter 'xCoord' defined on line 392
        
        sdata = VNATest.trigger()#imagefile, datafile)
        phase = np.round(np.degrees(np.angle(sdata)),3)
        amp = np.round(np.abs(sdata),5)
                    
        #Store data into predifned slots from the given x, y, and voltage parameters on lines 392,393, and 396
        comp_arr[i] = sdata
        amp_arr[i] = amp
        phase_arr[i] = phase
                        
        #Save all the data collected so far for the given x and y coordinates 
        np.savez(
            experiment_dir / '2026-03-27-ID2-48inOvertheAirHB.npz',  # "results_phaseStep_40in.npz",
            comp=comp_arr,
            amplitudes=amp_arr,
            phases=phase_arr,
            iteration = i)
        time.sleep(1)

    if value >= 10:
        voltages = np.full((24,8),0.0)
        update_lb_array_file(voltages)
        rpi.update_dacs()
        time.sleep(10)
        print("reset to zero")
    
    rpi.stop_program()
    rpi.close()
    
if __name__ == "__main__":
    main()