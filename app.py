# app.py
from flask import Flask, render_template, request, jsonify
import serial
import threading
import time
import json
import os
import subprocess
import sqlite3
from datetime import datetime, date

# --- KONFIGURACE ---
# Na Raspberry Pi to bude pravděpodobně '/dev/ttyUSB0' nebo '/dev/ttyACM0'
# Na Windows 'COM8'
SERIAL_PORT = 'COM8' if os.name == 'nt' else '/dev/ttyUSB0'
BAUD_RATE = 9600
SERIAL_TIMEOUT = 1
STATS_FILE = 'stats.json'
CALIB_FILE = 'calibration.json'
DB_FILE = 'logs.db'

# GPIO KONFIGURACE (MOSFET)
MOSFET_PIN = 27  # Pin, kam je připojen Gate MOSFETu (BCM číslování)

# Nastavení pumpy
PUMP_TIME_MIN = 1300
PUMP_TIME_MAX = 4000
BASE_FLOW_RATE = 0.03

# Barvy pro grafy a nastavení faktorů
ALCOHOL_CONFIG = {
    'VODKA': {'name': 'Vodka / Rum', 'factor': 1.0, 'color': '#3498db'},
    'JAGER': {'name': 'Jägermeister', 'factor': 0.75, 'color': '#e67e22'}, 
    'LIQUEUR': {'name': 'Vaječný likér', 'factor': 0.6, 'color': '#f1c40f'},
    'WATER': {'name': 'Voda (Test)', 'factor': 1.05, 'color': '#2ecc71'}
}

DEFAULT_POSITIONS = [157, 141, 125, 104, 87, 68]

app = Flask(__name__)

# --- HARDWARE SETUP (GPIO) ---
GPIO_AVAILABLE = False
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(MOSFET_PIN, GPIO.OUT)
    GPIO.output(MOSFET_PIN, GPIO.LOW) # Výchozí stav: vypnuto
    GPIO_AVAILABLE = True
    print(f"GPIO {MOSFET_PIN} inicializováno pro MOSFET.")
except ImportError:
    print("Knihovna RPi.GPIO nenalezena (běžíte na PC?), simuluji GPIO.")
except Exception as e:
    print(f"Chyba GPIO: {e}")

# --- GLOBÁLNÍ PROMĚNNÉ ---
# Přidáno 'lights_on' do stavu systému
system_state = {'SYSTEM': 'IDLE', 'lights_on': False} 
sensor_states = {str(i): {'SENSOR': 'OFF', 'DOSE': 'IDLE'} for i in range(6)}
virtual_inputs = {'BUTTON_MAIN': 0, 'BUTTON_FLUSH': 0, 'POT_VAL': 512}
current_alcohol_key = 'VODKA'
new_record_flag = False 

# Tachometr stroje
stats = {
    'session_shots': 0,
    'total_shots': 0,
    'total_volume_ml': 0.0,
    'total_runtime_sec': 0
}

app_start_time = time.time()
last_save_time = time.time()
calibration_data = list(DEFAULT_POSITIONS)

# --- PRÁCE S DATABÁZÍ (SQLITE) ---
def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                event_type TEXT,
                volume_ml REAL,
                details TEXT
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e: print(f"Chyba DB: {e}")

def write_log(event_type, volume=0.0, details=""):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO logs (event_type, volume_ml, details) VALUES (?, ?, ?)",
                  (event_type, volume, details))
        conn.commit()
        conn.close()
    except Exception as e: print(f"Chyba Log: {e}")

# --- SQL STATISTICKÉ FUNKCE ---
def get_stats_from_db():
    counts = {}
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        for key in ALCOHOL_CONFIG:
            search_str = f"%Key:{key}%"
            c.execute("SELECT COUNT(*) FROM logs WHERE event_type='SHOT' AND details LIKE ?", (search_str,))
            counts[key] = c.fetchone()[0]
        conn.close()
    except:
        for key in ALCOHOL_CONFIG: counts[key] = 0
    return counts

def get_today_count():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM logs WHERE event_type='SHOT' AND date(timestamp, 'localtime') = date('now', 'localtime')")
        res = c.fetchone()[0]
        conn.close()
        return res
    except: return 0

def get_historical_daily_record():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*) as cnt 
            FROM logs 
            WHERE event_type='SHOT' 
            GROUP BY date(timestamp, 'localtime') 
            ORDER BY cnt DESC 
            LIMIT 1
        """)
        res = c.fetchone()
        conn.close()
        return res[0] if res else 0
    except: return 0

# --- LOAD/SAVE JSON ---
def load_stats():
    global stats
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                data = json.load(f)
                for k in stats.keys():
                    if k in data: stats[k] = data[k]
        except: pass

def save_stats():
    current_runtime = stats['total_runtime_sec'] + (time.time() - app_start_time)
    data = stats.copy()
    data['total_runtime_sec'] = int(current_runtime)
    try:
        with open(STATS_FILE, 'w') as f: json.dump(data, f)
    except: pass

def load_calibration():
    global calibration_data
    if os.path.exists(CALIB_FILE):
        try:
            with open(CALIB_FILE, 'r') as f: calibration_data = json.load(f)
        except: pass

def save_calibration():
    try:
        with open(CALIB_FILE, 'w') as f: json.dump(calibration_data, f)
    except: pass

load_stats()
load_calibration()
init_db()

# --- ARDUINO & LOGIC ---
def send_to_arduino(command):
    if ser and ser.is_open:
        try: ser.write(f"{command}\n".encode('utf-8'))
        except: pass

def sync_arduino_calibration():
    time.sleep(3)
    for i, angle in enumerate(calibration_data):
        send_to_arduino(f"CALIB:{i};{angle}")
        time.sleep(0.05)

ser = None
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=SERIAL_TIMEOUT)
    threading.Thread(target=sync_arduino_calibration, daemon=True).start()
except: print(f"Arduino nepřipojeno na {SERIAL_PORT}.")

def calculate_real_volume_from_pot():
    pot = virtual_inputs['POT_VAL']
    pump_time_ms = PUMP_TIME_MIN + (float(pot) / 1023.0) * (PUMP_TIME_MAX - PUMP_TIME_MIN)
    factor = ALCOHOL_CONFIG[current_alcohol_key]['factor']
    return pump_time_ms * (BASE_FLOW_RATE * factor)

def read_from_arduino():
    global system_state, sensor_states, stats, new_record_flag
    if ser and ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8').strip()
            if not line: return
            parts = line.split(';')
            
            if parts[0] == 'STATUS:SYSTEM':
                if len(parts) > 1: system_state['SYSTEM'] = parts[1]
            
            elif parts[0] == 'STATUS:SENSOR':
                if len(parts) >= 3:
                    idx, state = parts[1], parts[2]
                    if idx in sensor_states: sensor_states[idx]['SENSOR'] = state
            
            elif parts[0] == 'STATUS:DOSE':
                if len(parts) >= 3:
                    idx, new_state = parts[1], parts[2]
                    if idx in sensor_states:
                        curr = sensor_states[idx]['DOSE']
                        
                        # --- DETEKCE ÚSPĚŠNÉHO NAČEPOVÁNÍ ---
                        if new_state == 'PUMPED' and curr != 'PUMPED':
                            vol = calculate_real_volume_from_pot()
                            
                            stats['session_shots'] += 1
                            stats['total_shots'] += 1
                            stats['total_volume_ml'] += vol
                            
                            old_record = get_historical_daily_record()
                            alcohol_name = ALCOHOL_CONFIG[current_alcohol_key]['name']
                            write_log('SHOT', vol, f"Pos {idx} | {alcohol_name} | Key:{current_alcohol_key}")
                            
                            today_count = get_today_count()
                            if today_count > old_record and today_count > 1:
                                new_record_flag = True
                                print(f"!!! NOVÝ REKORD !!! {today_count} panáků")

                            save_stats()
                        
                        sensor_states[idx]['DOSE'] = new_state
        except: pass

def serial_monitor():
    global last_save_time
    while True:
        read_from_arduino()
        if ser and ser.is_open:
            send_to_arduino(f"INPUT:BUTTON_MAIN;{virtual_inputs['BUTTON_MAIN']}")
            send_to_arduino(f"INPUT:BUTTON_FLUSH;{virtual_inputs['BUTTON_FLUSH']}")
            send_to_arduino(f"INPUT:POT_VAL;{virtual_inputs['POT_VAL']}")
        
        if time.time() - last_save_time > 60:
            save_stats()
            last_save_time = time.time()
        time.sleep(0.1)

threading.Thread(target=serial_monitor, daemon=True).start()

# --- API ---
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    global new_record_flag
    rt = stats['total_runtime_sec'] + (time.time() - app_start_time)
    m, s = divmod(rt, 60); h, m = divmod(m, 60)
    
    is_new_record = new_record_flag
    if new_record_flag: new_record_flag = False

    response = {
        'system': system_state['SYSTEM'],
        'lights_on': system_state['lights_on'], # Odesíláme stav světel do UI
        'sensors': sensor_states,
        'pot_value': virtual_inputs['POT_VAL'],
        'stats': stats,
        'today_count': get_today_count(),
        'new_record': is_new_record,
        'alcohol': {
            'current': current_alcohol_key,
            'config': ALCOHOL_CONFIG
        },
        'display_stats': {
            'runtime': f"{int(h)}h {int(m)}m",
            'volume': f"{stats['total_volume_ml']/1000:.1f} L" if stats['total_volume_ml'] > 1000 else f"{int(stats['total_volume_ml'])} ml"
        }
    }
    return jsonify(response)

@app.route('/api/control', methods=['POST'])
def post_control():
    global virtual_inputs, system_state
    data = request.json
    act, val = data.get('action'), data.get('value')
    
    # --- OVLÁDÁNÍ MOSFETU (SVĚTLA) ---
    if act == 'lights_toggle':
        # Přepnutí stavu v paměti
        system_state['lights_on'] = not system_state['lights_on']
        
        # Fyzické přepnutí pinu (pokud jsme na RPi)
        if GPIO_AVAILABLE:
            if system_state['lights_on']:
                GPIO.output(MOSFET_PIN, GPIO.HIGH)
            else:
                GPIO.output(MOSFET_PIN, GPIO.LOW)
        print(f"Světla: {system_state['lights_on']}")

    # --- OSTATNÍ OVLÁDÁNÍ ---
    elif act == 'main_down': virtual_inputs['BUTTON_MAIN'] = 1
    elif act == 'main_up': virtual_inputs['BUTTON_MAIN'] = 0
    elif act == 'flush_down': virtual_inputs['BUTTON_FLUSH'] = 1
    elif act == 'flush_up': virtual_inputs['BUTTON_FLUSH'] = 0
    elif act == 'pot_set':
        if val is not None: virtual_inputs['POT_VAL'] = int(val)
        
    return jsonify({'status': 'ok'})

@app.route('/api/settings/alcohol', methods=['POST'])
def set_alcohol():
    global current_alcohol_key
    key = request.json.get('key')
    if key in ALCOHOL_CONFIG:
        current_alcohol_key = key
        return jsonify({'status': 'ok', 'name': ALCOHOL_CONFIG[key]['name']})
    return jsonify({'error': 'Unknown type'}), 400

@app.route('/api/calibration', methods=['GET', 'POST'])
def calib():
    global calibration_data
    if request.method == 'POST':
        i, a = int(request.json['index']), int(request.json['angle'])
        calibration_data[i] = a
        save_calibration()
        send_to_arduino(f"CALIB:{i};{a}")
        return jsonify({'status': 'ok'})
    return jsonify(calibration_data)

@app.route('/api/history', methods=['GET'])
def hist():
    try:
        conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 100").fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/stats/chart', methods=['GET'])
def get_chart_data():
    counts_from_db = get_stats_from_db()
    daily_record = get_historical_daily_record()
    return jsonify({
        'counts': counts_from_db,
        'config': ALCOHOL_CONFIG,
        'total': stats['total_shots'],
        'daily_record': daily_record
    })

@app.route('/api/system/shutdown', methods=['POST'])
def shutdown():
    save_stats()
    write_log('SYSTEM', 0, 'Shutdown')
    if GPIO_AVAILABLE:
        GPIO.cleanup() # Úklid pinů před vypnutím
    if os.name != 'nt': subprocess.run(['sudo', 'shutdown', 'now'])
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    write_log('SYSTEM', 0, 'Startup')
    try:
        # Použijte 0.0.0.0 aby to bylo vidět na síti
        app.run(host='0.0.0.0', port=5000)
    finally:
        if GPIO_AVAILABLE:
            GPIO.cleanup()