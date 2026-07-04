import serial
import time

try:
    s = serial.Serial("COM5", 115200, timeout=1)
    # Toggle DTR/RTS to reset the board and capture boot logs
    s.setDTR(False)
    s.setRTS(False)
    time.sleep(0.1)
    s.setDTR(True)
    s.setRTS(True)
    
    t_end = time.time() + 5
    print("--- Capturing COM5 Serial Output ---")
    while time.time() < t_end:
        line = s.readline()
        if line:
            print(line.decode("utf-8", errors="ignore"), end="")
except Exception as e:
    print(f"Error: {e}")

