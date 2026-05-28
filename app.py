from flask import Flask, render_template, request, jsonify
from flasgger import Swagger
import serial
import serial.tools.list_ports
import threading
import time
import os
import subprocess
import sqlite3
import numpy as np
import logging

# --- KONFIGURACE ---
SERIAL_PORT = 'COM5' if os.name == 'nt' else '/dev/ttyUSB0'
BAUD_RATE = 115200
DB_FILE = 'logs.db'
FAN_PIN = 27 # Pin pro MOSFET ventilátoru

# Pevně nastavené PWM pro čerpadlo
HARDCODED_PUMP_PWM = 150

# Výchozí polohy serva
DEFAULT_POSITIONS = [157, 141, 125, 104, 87, 68]

# --- GLOBÁLNÍ PROMĚNNÉ ---
app = Flask(__name__)

# Konfigurace Swagger dokumentace
swagger_config = {
    "headers": [],
    "specs": [
        {
            "endpoint": 'apispec',
            "route": '/apispec.json',
            "rule_filter": lambda rule: True,
            "model_filter": lambda tag: True,
        }
    ],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/apidocs/"
}
swagger = Swagger(app, config=swagger_config, template={
    "info": {
        "title": "Masters Thesis API",
        "description": "API pro řízení a monitorování automatického dávkovače",
        "version": "1.2.0"
    }
})

ser = None

# ZÁMKY PRO VLÁKNA
db_lock = threading.Lock()      
serial_lock = threading.Lock()  

# Globální proměnná pro Lookup Table (LUT) - formát: [objem_ml, cas_ms]
LUT_DATA = None 

system_state = {'SYSTEM': 'DISCONNECTED', 'lights_on': False}
sensor_states = {str(i): {'SENSOR': 'OFF', 'DOSE': 'IDLE'} for i in range(6)}
virtual_inputs = {'BUTTON_MAIN': 0, 'BUTTON_FLUSH': 0, 'TARGET_ML': 40}

app_start_time = time.time()
last_save_time = time.time()

stats = {
    'session_glasses': 0, 'total_glasses': 0,
    'total_volume_ml': 0.0, 'total_runtime_sec': 0,
    'led_brightness': 200,
    'lights_on': False
}
calibration_data = list(DEFAULT_POSITIONS)

#PROMĚNNÉ PRO RETRY MECHANISMUS A CHYBY 
pending_command = None
retry_count = 0
last_command_time = 0
MAX_RETRIES = 3
RETRY_TIMEOUT = 0.2

system_errors = [] 

def report_error(error_msg):
    global system_errors
    timestamp = time.strftime("%H:%M:%S")
    full_msg = f"[{timestamp}] {error_msg}"
    print(f"!!! SYSTEM ERROR: {full_msg}")
    
    system_errors.append(full_msg)
    if len(system_errors) > 5: 
        system_errors.pop(0)

# HARDWARE SETUP (RPi GPIO)
GPIO_AVAILABLE = False
fan_active = False # Stavová proměnná pro hysterezi ventilátoru

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(FAN_PIN, GPIO.OUT)
    GPIO.output(FAN_PIN, GPIO.LOW)
    GPIO_AVAILABLE = True
except: pass

# --- DATABÁZE ---
def init_db():
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            
            c.execute('''CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, event_type TEXT, volume_ml REAL, details TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS machine_stats (id INTEGER PRIMARY KEY CHECK (id = 1), total_glasses INTEGER DEFAULT 0, total_volume_ml REAL DEFAULT 0.0, total_runtime_sec INTEGER DEFAULT 0, led_brightness INTEGER DEFAULT 200)''')
            c.execute("INSERT OR IGNORE INTO machine_stats (id) VALUES (1)")
            
            c.execute('''CREATE TABLE IF NOT EXISTS servo_calibration (position_index INTEGER PRIMARY KEY, angle INTEGER NOT NULL)''')
            c.execute("SELECT COUNT(*) FROM servo_calibration")
            if c.fetchone()[0] == 0:
                for i, angle in enumerate(DEFAULT_POSITIONS):
                    c.execute("INSERT INTO servo_calibration (position_index, angle) VALUES (?, ?)", (i, angle))
            
            
            c.execute('''CREATE TABLE IF NOT EXISTS volume_calibration (
                        volume_ml REAL PRIMARY KEY, 
                        duration_ms INTEGER NOT NULL)''')
            
            c.execute("SELECT COUNT(*) FROM volume_calibration")
            if c.fetchone()[0] == 0:
                print("DB >> Inicializuji empirickou kalibrační tabulku (LUT) z reálného měření...")
                defaults = [
    (1.85, 50), (2.41, 100), (4.00, 150), (4.44, 200), (5.50, 250),
    (6.67, 300), (7.41, 350), (8.52, 400), (9.24, 450), (10.31, 500),
    (11.15, 550), (11.89, 600), (12.46, 650), (13.52, 700), (14.09, 750),
    (15.33, 800), (16.63, 850), (17.59, 900), (18.56, 950), (19.61, 1000),
    (20.61, 1050), (21.41, 1100), (22.26, 1150), (23.43, 1200), (24.13, 1250),
    (24.46, 1300), (25.09, 1350), (25.41, 1400), (27.46, 1450), (28.43, 1500),
    (29.15, 1550), (30.30, 1600), (31.39, 1650), (32.28, 1700), (33.53, 1750),
    (34.93, 1800), (35.63, 1850), (36.79, 1900), (38.49, 1950), (39.51, 2000), (39.6, 2050), (39.8, 2100), (40.1, 2150)
]
                c.executemany("INSERT INTO volume_calibration (volume_ml, duration_ms) VALUES (?, ?)", defaults)

            conn.commit()
            conn.close()
        except Exception as e: 
            report_error(f"DB Init Error: {e}")

def load_all_data_from_db():
    global stats, calibration_data, LUT_DATA
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            
            c.execute("SELECT total_glasses, total_volume_ml, total_runtime_sec, led_brightness FROM machine_stats WHERE id=1")
            row = c.fetchone()
            if row:
                stats['total_glasses'], stats['total_volume_ml'], stats['total_runtime_sec'], stats['led_brightness'] = row
            
            # OPRAVENO: Vyhledává logy typu 'GLASS', aby to ladilo se zápisem dole
            c.execute("SELECT COUNT(*) FROM logs WHERE event_type='GLASS' AND date(timestamp) = date('now')")
            stats['session_glasses'] = c.fetchone()[0]

            c.execute("SELECT position_index, angle FROM servo_calibration ORDER BY position_index ASC")
            rows = c.fetchall()
            if rows:
                db_list = [r[1] for r in rows]
                calibration_data[:] = (db_list + DEFAULT_POSITIONS[len(db_list):])[:6]
            
            # Načtení objemové kalibrace (seřazeno vzestupně pro NumPy interpolaci)
            c.execute("SELECT volume_ml, duration_ms FROM volume_calibration ORDER BY volume_ml ASC")
            rows = c.fetchall()
            if rows:
                LUT_DATA = np.array(rows)
            else:
                LUT_DATA = np.array([[0.0, 0], [50.0, 3200]])

            conn.close()
        except Exception as e: 
            report_error(f"DB Load Error: {e}")

def save_stats_to_db():
    global app_start_time
    now = time.time()
    elapsed = now - app_start_time
    stats['total_runtime_sec'] += int(elapsed)
    app_start_time = now

    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("UPDATE machine_stats SET total_glasses=?, total_volume_ml=?, total_runtime_sec=?, led_brightness=? WHERE id=1", 
                         (stats['total_glasses'], stats['total_volume_ml'], stats['total_runtime_sec'], stats['led_brightness']))
            conn.commit()
            conn.close()
        except Exception as e: print(f"DB Save Error: {e}")

def write_log(event_type, volume=0.0, details=""):
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("INSERT INTO logs (event_type, volume_ml, details) VALUES (?, ?, ?)", (event_type, volume, details))
            conn.commit()
            conn.close()
        except Exception as e: print(f"Log Error: {e}")

def update_fan_status_file(is_on):
    with open("fan_status.txt", "w") as f:
        f.write("1" if is_on else "0")
def get_rpi_temp():
    global fan_active
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_c = int(round(int(f.read()) / 1000.0))
            if GPIO_AVAILABLE:
                if temp_c >= 40 and not fan_active:
                    GPIO.output(FAN_PIN, GPIO.HIGH)
                    fan_active = True
                    update_fan_status_file(True) # PŘIDAT TOTO
                elif temp_c <= 30 and fan_active:
                    GPIO.output(FAN_PIN, GPIO.LOW)
                    fan_active = False
                    update_fan_status_file(False) # PŘIDAT TOTO
            return temp_c
    except: return 0
# VÝPOČET ČASU ČERPÁNÍ (Lineární interpolace z LUT)
def calculate_pump_time(target_ml):
    global LUT_DATA
    if LUT_DATA is None or len(LUT_DATA) == 0:
        return 2000 # Fallback čas
        
    volumes = LUT_DATA[:, 0]
    durations = LUT_DATA[:, 1]

    # np.interp interpoluje na základě dodané empirické tabulky
    duration_ms = np.interp(target_ml, volumes, durations)
    
    return int(duration_ms)

# KOMUNIKACE 
def send_to_arduino(body, is_retry=False):
    global ser, pending_command, retry_count, last_command_time
    with serial_lock:
        if ser and ser.is_open:
            try:
                cs = 0
                for char in body: cs ^= ord(char)
                msg = f"${body}*{cs:02X}\n"
                
                if not is_retry:
                    pending_command = body
                    retry_count = 0
                
                last_command_time = time.time()
                print(f"PYTHON >> {msg.strip()} {'(RETRY ' + str(retry_count) + ')' if is_retry else ''}") 
                ser.write(msg.encode('utf-8'))
            except Exception as e: 
                report_error(f"Nelze odeslat data: {e}")

def validate_checksum(line_str):
    try:
        if not line_str.startswith('$') or '*' not in line_str: return None 
        content, received_cs_hex = line_str[1:].rsplit('*', 1)
        my_cs = 0
        for char in content: my_cs ^= ord(char)
        if my_cs == int(received_cs_hex, 16): return content 
        return None
    except: return None

def sync_arduino_settings():
    time.sleep(3) 
    
    for i, angle in enumerate(calibration_data):
        send_to_arduino(f"SET_POS:{i};{angle}")
        time.sleep(0.1)
    send_to_arduino(f"BRIGHTNESS:{stats['led_brightness']}")

def read_from_arduino():
    global system_state, sensor_states, stats, ser, pending_command, retry_count
    with serial_lock:
        if not ser or not ser.is_open:
            raise serial.SerialException("Port closed")

        if ser.in_waiting > 0:
            raw_line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not raw_line: return
            
            print(f"ARDUINO RAW << {raw_line}")
            clean_data = validate_checksum(raw_line)
            if not clean_data:
                print(f"!!! Checksum Error on: {raw_line}")
                return

            if clean_data.startswith("ACK:"):
                acked_cmd = clean_data.split(":")[1]
                if pending_command and pending_command.startswith(acked_cmd):
                    print(f"ACK CONFIRMED >> {acked_cmd}")
                    pending_command = None
                return

            if clean_data.startswith("NACK:"):
                error_type = clean_data.split(":")[1] if len(clean_data.split(":")) > 1 else "Neznámá"
                report_error(f"Arduino zamítlo příkaz (NACK). Důvod: {error_type}")
                return

            if clean_data.startswith("ERROR:"):
                msg = clean_data.split(":", 1)[1] if len(clean_data.split(":")) > 1 else "HW Error"
                report_error(f"HW CHYBA: {msg}")
                return
            
            parts = clean_data.split(';')
            if parts[0] == 'STATUS:SYSTEM' and len(parts) > 1:
                new_sys_state = parts[1]
                if system_state['SYSTEM'] != new_sys_state:
                    system_state['SYSTEM'] = new_sys_state
                    print(f"SYSTEM STATE CHANGE: {new_sys_state}")
                    if new_sys_state == 'ESTOP': write_log('EMERGENCY', 0, 'Hardware E-Stop activated')

            elif parts[0] == 'STATUS:SENSOR' and len(parts) >= 3:
                idx, state = parts[1], parts[2]
                if idx in sensor_states: sensor_states[idx]['SENSOR'] = state
            
            elif parts[0] == 'STATUS:DOSE' and len(parts) >= 3:
                idx, new_state = parts[1], parts[2]
                if idx in sensor_states:
                    if new_state == 'PUMPED' and sensor_states[idx]['DOSE'] != 'PUMPED':
                        current_ml_target = virtual_inputs['TARGET_ML']
                        stats['session_glasses'] += 1
                        stats['total_glasses'] += 1
                        stats['total_volume_ml'] += current_ml_target
                        write_log('GLASS', current_ml_target, f"Pos {idx}")
                        save_stats_to_db()
                        print(f"--- GLASS REGISTERED: Pos {idx}, Vol {current_ml_target:.1f}ml ---")
                    sensor_states[idx]['DOSE'] = new_state

def serial_monitor():
    global last_save_time, ser, pending_command, retry_count, last_command_time, system_state
    last_heartbeat = 0
    last_sent_inputs = {'BUTTON_MAIN': -1, 'BUTTON_FLUSH': -1, 'TARGET_ML': -1}

    while True:
        if ser is None or not ser.is_open:
            system_state['SYSTEM'] = 'DISCONNECTED'
            try:
                
                ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
                
                threading.Thread(target=sync_arduino_settings, daemon=True).start()
            except Exception as e:
                time.sleep(2)
                continue

        try:
            read_from_arduino()
            
            if pending_command and (time.time() - last_command_time > RETRY_TIMEOUT):
                if retry_count < MAX_RETRIES:
                    retry_count += 1
                    print(f"TIMEOUT! Re-sending {pending_command}...")
                    send_to_arduino(pending_command, is_retry=True)
                else:
                    report_error(f"Příkaz selhal (timeout): {pending_command}")
                    with serial_lock:
                        pending_command = None

            if not pending_command:
                for key in ['BUTTON_MAIN', 'TARGET_ML']:
                    if virtual_inputs[key] != last_sent_inputs[key]:
                        send_to_arduino(f"INPUT:{key};{virtual_inputs[key]}")
                        last_sent_inputs[key] = virtual_inputs[key]
                        last_heartbeat = time.time()
                
                if virtual_inputs['BUTTON_FLUSH'] != last_sent_inputs['BUTTON_FLUSH']:
                     send_to_arduino(f"FLUSH:{virtual_inputs['BUTTON_FLUSH']}")
                     last_sent_inputs['BUTTON_FLUSH'] = virtual_inputs['BUTTON_FLUSH']

                if time.time() - last_heartbeat > 4.0:
                    send_to_arduino(f"BRIGHTNESS:{stats['led_brightness']}")
                    last_heartbeat = time.time()

        except Exception as e:
            report_error(f"Ztráta spojení: {e}")
            try: 
                with serial_lock: ser.close()
            except: pass
            ser = None
            system_state['SYSTEM'] = 'DISCONNECTED'

        if time.time() - last_save_time > 60:
            save_stats_to_db()
            last_save_time = time.time()
            
        time.sleep(0.005)

# API 

@app.route('/')
def index(): 
    """
    Hlavní uživatelské rozhraní (HMI)
    ---
    tags:
      - Frontend
    responses:
      200:
        description: Vrátí hlavní HTML stránku webové aplikace
    """
    return render_template('index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    """
    Kompletní telemetrie a stav stroje
    ---
    tags:
      - Monitoring
    responses:
      200:
        description: JSON objekt se stavem FSM z MCU, senzorů, počítadel a diagnostiky Raspberry Pi
    """
    rt = stats['total_runtime_sec'] + (time.time() - app_start_time)
    m, s = divmod(int(rt), 60); h, m = divmod(m, 60)
    cpu_temp = get_rpi_temp()
    
    return jsonify({
        'system': system_state['SYSTEM'], 
        'lights_on': stats['lights_on'],
        'sensors': sensor_states, 
        'target_ml': virtual_inputs['TARGET_ML'],
        'pump_pwm': HARDCODED_PUMP_PWM,
        'stats': stats, 
        'errors': system_errors,
        'display_stats': {
            'runtime': f"{h}h {m}m", 
            'volume': f"{int(stats['total_volume_ml'])} ml",
            'cpu_temp': cpu_temp
        }
    })

@app.route('/api/control', methods=['POST'])
def post_control():
    """
    Odeslání řídicích příkazů do stroje
    ---
    tags:
      - Ovládání
    parameters:
      - in: body
        name: body
        description: JSON objekt s požadovanou akcí a případnou hodnotou
        required: true
        schema:
          type: object
          properties:
            action:
              type: string
              example: target_ml_set
            value:
              type: integer
              example: 40
    responses:
      200:
        description: Příkaz úspěšně přijat, přeložen do UART rámce a odeslán
    """
    global virtual_inputs, stats
    data = request.json
    act, val = data.get('action'), data.get('value')
    
    if act == 'lights_toggle':
        stats['lights_on'] = not stats['lights_on']
        send_to_arduino(f"LIGHTS:{1 if stats['lights_on'] else 0}")
    elif act == 'main_down': virtual_inputs['BUTTON_MAIN'] = 1
    elif act == 'main_up': virtual_inputs['BUTTON_MAIN'] = 0
    elif act == 'flush_down': virtual_inputs['BUTTON_FLUSH'] = 1
    elif act == 'flush_up': virtual_inputs['BUTTON_FLUSH'] = 0
    elif act == 'target_ml_set':
        if val is not None: 
            target_ml = int(val)
            virtual_inputs['TARGET_ML'] = target_ml
            
            # Zjistí čas z LUT tabulky a pošle čistě jen milisekundy
            duration_ms = calculate_pump_time(target_ml)
            send_to_arduino(f"PUMP:{duration_ms}")

    return jsonify({'status': 'ok'})

@app.route('/api/calibration', methods=['GET', 'POST'])
def calib():
    """
    Čtení a zápis kinematické kalibrace
    ---
    tags:
      - Kalibrace
    parameters:
      - in: body
        name: body
        required: false
        schema:
          type: object
          properties:
            index:
              type: integer
              description: Index zásobníku (0-5)
              example: 2
            angle:
              type: integer
              description: Pracovní úhel servomotoru
              example: 125
    responses:
      200:
        description: Vrací aktuální pole 6 úhlů nebo potvrzuje uložení nové polohy do SQLite
    """
    if request.method == 'POST':
        i, a = int(request.json['index']), int(request.json['angle'])
        calibration_data[i] = a
        with db_lock:
            try:
                conn = sqlite3.connect(DB_FILE)
                conn.execute("INSERT OR REPLACE INTO servo_calibration (position_index, angle) VALUES (?, ?)", (i, a))
                conn.commit(); conn.close()
            except Exception as e:
                report_error(f"DB Chyba ukládání polohy: {e}")
        send_to_arduino(f"CALIB:{i};{a}")
        return jsonify({'status': 'ok'})
    return jsonify(calibration_data)

@app.route('/api/settings/brightness', methods=['POST'])
def set_brightness():
    """
    Nastavení intenzity osvětlení
    ---
    tags:
      - Nastavení
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            value:
              type: integer
              description: PWM hodnota jasu (0-255)
              example: 200
    responses:
      200:
        description: Jas byl úspěšně upraven a uložen do databáze
    """
    val = int(request.json['value'])
    stats['led_brightness'] = val
    save_stats_to_db()
    send_to_arduino(f"BRIGHTNESS:{val}")
    return jsonify({'status': 'ok'})

@app.route('/api/system/exit_kiosk', methods=['POST'])
def exit_kiosk():
    """
    Nouzové ukončení Kiosk režimu displeje
    ---
    tags:
      - Systém
    responses:
      200:
        description: Proces prohlížeče Chromium byl na Raspberry Pi úspěšně ukončen
      500:
        description: Selhání při ukončování systémového procesu
    """
    try:
        if os.name != 'nt':
            subprocess.run(['pkill', '-f', 'chromium'], check=False)
        return jsonify({'status': 'ok', 'msg': 'Kiosk režim ukončen.'})
    except Exception as e:
        report_error(f"Chyba při ukončování Kiosk režimu: {str(e)}")
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/system/shutdown', methods=['POST'])
def shutdown():
    """
    Bezpečné vypnutí řídicího počítače (Halt)
    ---
    tags:
      - Systém
    responses:
      200:
        description: Databáze bezpečně uzavřena, GPIO vyčištěno a systém se vypíná
    """
    save_stats_to_db()
    if GPIO_AVAILABLE: GPIO.cleanup()
    if os.name != 'nt': subprocess.run(['sudo', 'shutdown', 'now'])
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    init_db()
    load_all_data_from_db()

    # --- POTLAČENÍ VÝPISŮ FLASK / WERKZEUG ---
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.logger.setLevel(logging.ERROR) 
    
    monitor_thread = threading.Thread(target=serial_monitor, daemon=True)
    monitor_thread.start()
    
    app.run(host='0.0.0.0', port=5000, debug=False)
