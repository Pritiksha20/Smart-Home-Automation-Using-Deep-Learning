from flask import Flask, render_template_string, request, jsonify
import RPi.GPIO as GPIO
import board
import adafruit_dht
import time
import threading
import requests
import os
import cv2
import numpy as np
import pickle
import base64
from datetime import datetime
from werkzeug.utils import secure_filename

# ================= GPIO SETUP =================
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

RELAY1 = 17   # Light
RELAY2 = 27   # Fan
LOCK_RELAY = 22

GPIO.setup(RELAY1, GPIO.OUT)
GPIO.setup(RELAY2, GPIO.OUT)
GPIO.setup(LOCK_RELAY, GPIO.OUT)

GPIO.output(RELAY1, GPIO.LOW)
GPIO.output(RELAY2, GPIO.LOW)
GPIO.output(LOCK_RELAY, GPIO.LOW)

# ================= DHT11 =================
dht_device = adafruit_dht.DHT11(board.D4)

# ================= MQ SENSORS =================
MQ2_PIN = 23
MQ135_PIN = 24
GPIO.setup(MQ2_PIN, GPIO.IN)
GPIO.setup(MQ135_PIN, GPIO.IN)

# ================= TELEGRAM =================
BOT_TOKEN = '8729255398:AAH8XHpgU1WJsnXgbnbbuNsrBeUVTkmL28k'   # <-- replace
CHAT_ID = '8087925156'       # <-- replace

# ================= PASSWORD =================
PASSWORD = "1234"
entered_password = ""
last_pressed_key = ""
keypad_lock = threading.Lock()

# ================= KEYPAD =================
ROWS = [5, 6, 13, 19]
COLS = [12, 16, 20, 21]
KEYPAD = [
    ['1', '2', '3', 'A'],
    ['4', '5', '6', 'B'],
    ['7', '8', '9', 'C'],
    ['*', '0', '#', 'D']
]
for row in ROWS:
    GPIO.setup(row, GPIO.OUT)
    GPIO.output(row, GPIO.HIGH)
for col in COLS:
    GPIO.setup(col, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# ================= FACE RECOGNITION =================
FACE_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
RECOGNIZER_FILE = "face_recognizer.yml"
LABELS_FILE = "face_labels.pkl"

face_module_ok = False
try:
    recognizer = cv2.face.LBPHFaceRecognizer_create()
    face_module_ok = True
except AttributeError:
    recognizer = None
    print("WARNING: opencv-contrib-python-headless not installed. Face recognition disabled.")

if face_module_ok and os.path.exists(RECOGNIZER_FILE) and os.path.exists(LABELS_FILE):
    recognizer.read(RECOGNIZER_FILE)
    with open(LABELS_FILE, 'rb') as f:
        label_names = pickle.load(f)
else:
    label_names = {}

face_lock = threading.Lock()

# ================= FLASK =================
app = Flask(__name__)
last_intruder_image = ""
last_alert_time = ""
last_unlock_message = ""
last_unlock_time = ""

# ---------- Sensor helpers ----------
def get_sensor_status(pin):
    return "CRITICAL" if GPIO.input(pin) == 0 else "NORMAL"

# ---------- Camera capture (Pi's USB webcam) ----------
def capture_with_opencv(filename):
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("OpenCV failed, trying fswebcam...")
        return capture_with_fswebcam(filename)
    for _ in range(10):
        cap.read()
        time.sleep(0.1)
    ret, frame = cap.read()
    cap.release()
    if ret:
        cv2.imwrite(filename, frame)
        print("Photo saved:", filename)
        return True
    return capture_with_fswebcam(filename)

def capture_with_fswebcam(filename):
    if os.system(f"fswebcam -r 1280x720 --no-banner {filename}") == 0:
        print("Photo saved via fswebcam:", filename)
        return True
    return False

# ---------- Face recognition ----------
def recognize_face(image_path):
    if not face_module_ok or not label_names:
        return None
    img = cv2.imread(image_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = FACE_CASCADE.detectMultiScale(gray, 1.1, 5)
    for (x, y, w, h) in faces:
        face_roi = gray[y:y+h, x:x+w]
        id_, confidence = recognizer.predict(face_roi)
        if confidence < 60:
            return label_names.get(id_, None)
    return None

def process_face_scan():
    global last_intruder_image, last_alert_time, last_unlock_message, last_unlock_time
    filename = f"static/face_scan_{int(time.time())}.jpg"
    if not capture_with_opencv(filename):
        return False, "Camera error"
    person = recognize_face(filename)
    if person:
        open_lock()
        success_msg = f"Door unlocked using face recognition for {person}"
        last_unlock_message = success_msg
        last_unlock_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        # Send Telegram text message
        try:
            url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
            requests.post(url, data={'chat_id': CHAT_ID, 'text': f"âœ… {success_msg}"}, timeout=10)
        except Exception as e:
            print("Telegram send message error:", e)
        return True, success_msg
    else:
        # Unknown face â€“ alert
        try:
            url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto'
            with open(filename, 'rb') as photo:
                requests.post(url, data={'chat_id': CHAT_ID}, files={'photo': photo}, timeout=10)
        except Exception as e:
            print("Telegram error:", e)
        last_intruder_image = filename
        last_alert_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        return False, "Unknown face â€“ alert sent"

def send_intruder_alert():
    global last_intruder_image, last_alert_time
    filename = f"static/intruder_{int(time.time())}.jpg"
    if not capture_with_opencv(filename):
        return
    try:
        url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto'
        with open(filename, 'rb') as photo:
            requests.post(url, data={'chat_id': CHAT_ID}, files={'photo': photo}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)
    last_intruder_image = filename
    last_alert_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

def open_lock():
    GPIO.output(LOCK_RELAY, GPIO.HIGH)
    time.sleep(5)
    GPIO.output(LOCK_RELAY, GPIO.LOW)

# ---------- Keypad thread ----------
def keypad_thread():
    global entered_password, last_pressed_key, PASSWORD
    while True:
        for i, row_pin in enumerate(ROWS):
            GPIO.output(row_pin, GPIO.LOW)
            for j, col_pin in enumerate(COLS):
                if GPIO.input(col_pin) == 0:
                    key = KEYPAD[i][j]
                    while GPIO.input(col_pin) == 0:
                        time.sleep(0.01)
                    time.sleep(0.05)
                    with keypad_lock:
                        last_pressed_key = key
                        if key == '#':
                            if entered_password == PASSWORD:
                                print("Correct Password")
                                open_lock()
                            elif entered_password == "":
                                success, msg = process_face_scan()
                                print("Face scan:", msg)
                            else:
                                print("Wrong Password")
                                send_intruder_alert()
                            entered_password = ""
                        elif key == '*':
                            entered_password = ""
                        else:
                            entered_password += key
            GPIO.output(row_pin, GPIO.HIGH)
        time.sleep(0.05)

# ================= UPDATED WEB UI (HTTP, no HTTPS) =================
HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SmartHome AI | Dashboard</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: radial-gradient(circle at 10% 20%, #0b0f19, #02040a);
    min-height: 100vh; color: #e0e0e0; display: flex; justify-content: center; align-items: center; padding: 1rem;
  }
  .stars {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: transparent; overflow: hidden; z-index: 0;
  }
  .stars::before, .stars::after {
    content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    background: radial-gradient(1px 1px at 20px 30px, #fff, rgba(0,0,0,0)),
                radial-gradient(1px 1px at 40px 70px, #fff, rgba(0,0,0,0)),
                radial-gradient(2px 2px at 90px 20px, #fff, rgba(0,0,0,0)),
                radial-gradient(1px 1px at 150px 80px, #fff, rgba(0,0,0,0)),
                radial-gradient(2px 2px at 200px 40px, #fff, rgba(0,0,0,0));
    background-size: 250px 150px;
    animation: starsMove 20s linear infinite;
    opacity: 0.3;
  }
  .stars::after {
    background-size: 300px 200px;
    animation: starsMove 30s linear infinite reverse;
    opacity: 0.2;
    background-position: 50px 50px;
  }
  @keyframes starsMove { 0% { transform: translateY(0); } 100% { transform: translateY(-150px); } }
  main {
    position: relative; z-index: 1; max-width: 1200px; width: 100%;
    display: flex; flex-direction: column; gap: 1.5rem; backdrop-filter: blur(10px);
  }
  .header {
    background: rgba(255,255,255,0.05); backdrop-filter: blur(15px);
    border-bottom: 1px solid rgba(0,255,200,0.15); border-radius: 30px;
    padding: 1.5rem 2rem; text-align: center; box-shadow: 0 8px 32px rgba(0,255,200,0.15);
  }
  .header h1 {
    font-size: clamp(1.8rem, 5vw, 2.8rem); font-weight: 700;
    background: linear-gradient(135deg, #00f2ff, #00ff88);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    letter-spacing: 2px;
  }
  .header p { color: #a0b0c0; margin-top: 0.3rem; }
  .dashboard { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; }
  .card {
    background: rgba(255,255,255,0.03); backdrop-filter: blur(20px);
    border: 1px solid rgba(0,255,200,0.2); border-radius: 25px;
    padding: 1.5rem; box-shadow: 0 4px 30px rgba(0,0,0,0.4); transition: all 0.3s ease;
  }
  .card:hover { border-color: rgba(0,255,200,0.5); box-shadow: 0 0 40px rgba(0,255,200,0.2); }
  .card h2 {
    font-size: 1.5rem; margin-bottom: 1.2rem; display: flex; align-items: center;
    gap: 10px; border-bottom: 1px dashed rgba(255,255,255,0.2); padding-bottom: 0.5rem;
  }
  .toggle-container { display: flex; justify-content: space-between; align-items: center; padding: 0.8rem 0; }
  .switch { position: relative; display: inline-block; width: 70px; height: 34px; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
    background-color: #2a2e3a; transition: 0.4s; border-radius: 34px;
  }
  .slider:before {
    position: absolute; content: ""; height: 26px; width: 26px; left: 4px; bottom: 4px;
    background-color: white; transition: 0.4s; border-radius: 50%;
  }
  input:checked + .slider { background: linear-gradient(90deg, #00ff99, #00ccff); box-shadow: 0 0 20px #00ffcc; }
  input:checked + .slider:before { transform: translateX(36px); }
  .status-text { font-weight: 600; text-transform: uppercase; letter-spacing: 1px; }
  .sensor-item { display: flex; justify-content: space-between; align-items: center; padding: 0.9rem 0; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .sensor-item:last-child { border-bottom: none; }
  .sensor-label { font-size: 1.1rem; display: flex; align-items: center; gap: 10px; }
  .sensor-value { font-size: 1.2rem; font-weight: 700; color: #00ffcc; }
  .badge { display: inline-block; padding: 0.25rem 0.8rem; border-radius: 20px; font-size: 0.8rem; font-weight: 700; background: rgba(255,255,255,0.1); }
  .badge.normal { background: #00c853; color: black; }
  .badge.critical { background: #ff1744; color: white; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.6; } 100% { opacity: 1; } }
  .alert-box {
    background: rgba(255,0,70,0.15); border: 2px solid #ff1744; border-radius: 20px;
    padding: 1rem; margin-top: 1rem; display: none; animation: fadeIn 0.5s;
  }
  .alert-box.show { display: block; }
  .alert-box img { width: 100%; border-radius: 15px; margin-top: 0.5rem; box-shadow: 0 0 20px red; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
  .keypad-feedback { background: rgba(0,0,0,0.4); border-radius: 15px; padding: 1rem; margin-top: 1rem; text-align: center; }
  .keypad-feedback .key { font-size: 3rem; font-weight: bold; color: #00ff99; }
  .keypad-feedback .passcode { font-size: 1.5rem; letter-spacing: 5px; margin-top: 0.5rem; }
  input[type="password"], input[type="text"] { padding: 0.8rem; border-radius: 10px; border: none; background: #1f2937; color: white; width: 100%; margin: 0.5rem 0; }
  button { padding: 0.8rem 1.5rem; border: none; border-radius: 10px; background: linear-gradient(135deg, #00f2ff, #0066ff); color: white; font-weight: bold; cursor: pointer; margin-top: 0.5rem; }
  button:hover { opacity: 0.9; }
  .captured-thumbs { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.5rem; }
  .captured-thumbs img { width: 70px; height: 70px; border-radius: 10px; object-fit: cover; border: 2px solid #00ffcc; }
  .placeholder { background: #1f2937; border-radius: 15px; padding: 2rem; text-align: center; color: #a0b0c0; }
  .unlock-notification {
    background: rgba(0,255,0,0.1); border: 2px solid #00ff99;
    border-radius: 15px; padding: 1rem; margin-top: 1rem;
    color: #00ff99; font-weight: bold; display: none;
  }
  @media (max-width: 600px) { .header { padding: 1rem; } .card { padding: 1rem; } }
</style>
</head>
<body>
<div class="stars"></div>
<main>
  <div class="header">
    <h1>ðŸ  SMART HOME AI</h1>
    <p><i class="fas fa-microchip"></i> Raspberry Pi 3B+ Control Center</p>
  </div>

  <div class="dashboard">
    <div class="card">
      <h2><i class="fas fa-bolt"></i> Appliance Control</h2>
      <div class="toggle-container">
        <span class="status-text" id="light-status">ðŸ’¡ Light</span>
        <label class="switch">
          <input type="checkbox" id="light-switch" onchange="toggleRelay(1)">
          <span class="slider"></span>
        </label>
      </div>
      <div class="toggle-container">
        <span class="status-text" id="fan-status">ðŸŒ€ Fan</span>
        <label class="switch">
          <input type="checkbox" id="fan-switch" onchange="toggleRelay(2)">
          <span class="slider"></span>
        </label>
      </div>
    </div>

    <div class="card">
      <h2><i class="fas fa-keyboard"></i> Keypad Live Monitor</h2>
      <div class="keypad-feedback">
        <div class="key" id="last-key">---</div>
        <div class="passcode" id="current-passcode">Enter password</div>
      </div>
    </div>

    <div class="card">
      <h2><i class="fas fa-lock"></i> Change Password</h2>
      <input type="password" id="new-password" placeholder="New password">
      <input type="password" id="confirm-password" placeholder="Confirm password">
      <button onclick="changePassword()"><i class="fas fa-save"></i> Update Password</button>
      <p id="pass-message" style="margin-top:0.5rem; color:#00ff99;"></p>
    </div>

    <!-- Pi Camera face training -->
    <div class="card">
      <h2><i class="fas fa-camera-retro"></i> Train Face (Pi Camera)</h2>
      <input type="text" id="face-name" placeholder="Person name">
      <div id="camera-placeholder" class="placeholder">
        <i class="fas fa-camera" style="font-size:3rem;"></i>
        <p>Click "Capture Photo" to take a picture from the Pi's webcam</p>
      </div>
      <button onclick="captureFromPiCamera()"><i class="fas fa-camera"></i> Capture Photo from Pi Camera</button>
      <div class="captured-thumbs" id="captured-thumbs"></div>
      <button onclick="trainFaces()" id="train-btn" style="display:none;"><i class="fas fa-brain"></i> Train & Replace (old photos removed)</button>
      <p id="face-message" style="margin-top:0.5rem; color:#00ff99;"></p>
    </div>

    <div class="card">
      <h2><i class="fas fa-thermometer-half"></i> Environment Sensors</h2>
      <div class="sensor-item">
        <span class="sensor-label"><i class="fas fa-temperature-high"></i> Temperature</span>
        <span class="sensor-value" id="temp-value">--Â°C</span>
      </div>
      <div class="sensor-item">
        <span class="sensor-label"><i class="fas fa-tint"></i> Humidity</span>
        <span class="sensor-value" id="hum-value">--%</span>
      </div>
      <div class="sensor-item">
        <span class="sensor-label"><i class="fas fa-smog"></i> Gas Level</span>
        <span class="sensor-value" id="mq2-value"><span class="badge critical">LOADING</span></span>
      </div>
      <div class="sensor-item">
        <span class="sensor-label"><i class="fas fa-wind"></i> Air Quality</span>
        <span class="sensor-value" id="mq135-value"><span class="badge critical">LOADING</span></span>
      </div>
    </div>

    <!-- Security Alert + Face Unlock Notification -->
    <div class="card" style="grid-column: span 2;">
      <h2><i class="fas fa-shield-haltered"></i> Security & Unlock Log</h2>
      <div id="unlock-notification" class="unlock-notification">
        <i class="fas fa-check-circle"></i> <span id="unlock-message-text"></span>
        <p id="unlock-time" style="font-size:0.8rem; color:#a0ffcc;"></p>
      </div>
      <div id="alert-box" class="alert-box">
        <h3 style="color:#ff1744;"><i class="fas fa-exclamation-triangle"></i> WRONG PASSWORD / UNKNOWN FACE</h3>
        <p id="alert-time" style="margin:0.5rem 0;"></p>
        <img id="alert-image" src="" alt="Intruder photo" style="display:none;">
      </div>
      <p id="no-alert" style="color:#a0b0c0; margin-top:1rem;"><i class="fas fa-check-circle"></i> System secured</p>
    </div>
  </div>
</main>

<script>
  // ----- Relay toggle -----
  function toggleRelay(relayId) {
    fetch('/toggle/' + relayId).then(res => res.text()).then(console.log);
  }

  // ----- Change password -----
  function changePassword() {
    const newPass = document.getElementById('new-password').value;
    const confirmPass = document.getElementById('confirm-password').value;
    const msg = document.getElementById('pass-message');
    if (!newPass || !confirmPass) {
      msg.innerText = 'Please fill both fields'; msg.style.color = '#ff4444'; return;
    }
    if (newPass !== confirmPass) {
      msg.innerText = 'Passwords do not match'; msg.style.color = '#ff4444'; return;
    }
    fetch('/set_password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: newPass })
    })
    .then(res => res.json())
    .then(data => {
      if (data.success) {
        msg.innerText = 'Password updated!'; msg.style.color = '#00ff99';
        document.getElementById('new-password').value = '';
        document.getElementById('confirm-password').value = '';
      } else {
        msg.innerText = 'Failed to update'; msg.style.color = '#ff4444';
      }
    });
  }

  // ----- Face training (Pi camera) -----
  let capturedImages = [];

  function captureFromPiCamera() {
    const msg = document.getElementById('face-message');
    msg.innerText = 'Capturing from Pi camera...';
    msg.style.color = '#00ffcc';

    fetch('/capture_training_photo')
      .then(res => res.json())
      .then(data => {
        if (data.success) {
          capturedImages.push(data.image);
          const img = document.createElement('img');
          img.src = data.image;
          document.getElementById('captured-thumbs').appendChild(img);
          document.getElementById('camera-placeholder').style.display = 'none';
          document.getElementById('train-btn').style.display = 'block';
          msg.innerText = `Photo ${capturedImages.length} captured.`;
          msg.style.color = '#00ff99';
        } else {
          msg.innerText = 'Error: ' + data.message;
          msg.style.color = '#ff4444';
        }
      })
      .catch(err => {
        msg.innerText = 'Network error: ' + err.message;
        msg.style.color = '#ff4444';
      });
  }

  function trainFaces() {
    const name = document.getElementById('face-name').value.trim();
    const msg = document.getElementById('face-message');
    if (!name) {
      msg.innerText = 'Enter a name first'; msg.style.color = '#ff4444'; return;
    }
    if (capturedImages.length === 0) {
      msg.innerText = 'Capture at least one photo'; msg.style.color = '#ff4444'; return;
    }

    const formData = new FormData();
    formData.append('name', name);
    capturedImages.forEach((dataUrl, idx) => {
      const byteString = atob(dataUrl.split(',')[1]);
      const mimeString = dataUrl.split(',')[0].split(':')[1].split(';')[0];
      const ab = new ArrayBuffer(byteString.length);
      const ia = new Uint8Array(ab);
      for (let i = 0; i < byteString.length; i++) {
        ia[i] = byteString.charCodeAt(i);
      }
      const blob = new Blob([ab], { type: mimeString });
      formData.append('images', blob, `capture_${idx}.jpg`);
    });

    msg.innerText = 'Training... please wait';
    msg.style.color = '#00ffcc';

    fetch('/train_face', {
      method: 'POST',
      body: formData
    })
    .then(res => res.json())
    .then(data => {
      if (data.success) {
        msg.innerText = data.message;
        msg.style.color = '#00ff99';
        capturedImages = [];
        document.getElementById('captured-thumbs').innerHTML = '';
        document.getElementById('face-name').value = '';
        document.getElementById('train-btn').style.display = 'none';
        document.getElementById('camera-placeholder').style.display = 'block';
      } else {
        msg.innerText = data.message;
        msg.style.color = '#ff4444';
      }
    })
    .catch(err => {
      msg.innerText = 'Network error: ' + err.message;
      msg.style.color = '#ff4444';
    });
  }

  // ----- Live keypad, sensors, alerts, and unlock message -----
  function updateKeypad() {
    fetch('/keypad_status')
      .then(res => res.json())
      .then(data => {
        document.getElementById('last-key').innerText = data.last_key || '---';
        document.getElementById('current-passcode').innerText = data.entered_password || 'Enter password';
      });
  }

  function updateSensors() {
    fetch('/sensors')
      .then(res => res.json())
      .then(data => {
        document.getElementById('temp-value').innerText = data.temp ? data.temp + 'Â°C' : '--Â°C';
        document.getElementById('hum-value').innerText = data.humidity ? data.humidity + '%' : '--%';
        let mq2badge = document.querySelector('#mq2-value .badge');
        mq2badge.innerText = data.mq2;
        mq2badge.className = 'badge ' + (data.mq2 === 'CRITICAL' ? 'critical' : 'normal');
        let mq135badge = document.querySelector('#mq135-value .badge');
        mq135badge.innerText = data.mq135;
        mq135badge.className = 'badge ' + (data.mq135 === 'CRITICAL' ? 'critical' : 'normal');
      });
  }

  function updateAlert() {
    fetch('/alert')
      .then(res => res.json())
      .then(data => {
        const alertBox = document.getElementById('alert-box');
        const noAlert = document.getElementById('no-alert');
        if (data.image && data.image !== '') {
          alertBox.classList.add('show');
          noAlert.style.display = 'none';
          document.getElementById('alert-time').innerText = 'Time: ' + data.time;
          document.getElementById('alert-image').src = '/' + data.image + '?t=' + new Date().getTime();
          document.getElementById('alert-image').style.display = 'block';
        } else {
          alertBox.classList.remove('show');
          noAlert.style.display = 'block';
        }
      });
  }

  function updateUnlockMessage() {
    fetch('/last_unlock')
      .then(res => res.json())
      .then(data => {
        const notif = document.getElementById('unlock-notification');
        if (data.message && data.message !== '') {
          document.getElementById('unlock-message-text').innerText = data.message;
          document.getElementById('unlock-time').innerText = data.time;
          notif.style.display = 'block';
        } else {
          notif.style.display = 'none';
        }
      });
  }

  setInterval(updateKeypad, 500);
  setInterval(updateSensors, 2000);
  setInterval(updateAlert, 3000);
  setInterval(updateUnlockMessage, 2000);
  updateKeypad();
  updateSensors();
  updateAlert();
  updateUnlockMessage();
</script>
</body>
</html>
'''

# ================= ROUTES =================
@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/toggle/<relay>')
def toggle(relay):
    if relay == '1':
        current = GPIO.input(RELAY1)
        GPIO.output(RELAY1, not current)
        return "Light Toggled"
    elif relay == '2':
        current = GPIO.input(RELAY2)
        GPIO.output(RELAY2, not current)
        return "Fan Toggled"
    return "Invalid"

@app.route('/sensors')
def sensors():
    temperature = humidity = None
    try:
        temperature = dht_device.temperature
        humidity = dht_device.humidity
    except RuntimeError:
        pass
    mq2 = get_sensor_status(MQ2_PIN)
    mq135 = get_sensor_status(MQ135_PIN)
    return jsonify({'temp': temperature, 'humidity': humidity, 'mq2': mq2, 'mq135': mq135})

@app.route('/alert')
def alert():
    return jsonify({'image': last_intruder_image, 'time': last_alert_time})

@app.route('/keypad_status')
def keypad_status():
    with keypad_lock:
        return jsonify({
            'last_key': last_pressed_key,
            'entered_password': '*' * len(entered_password) if entered_password else ''
        })

@app.route('/set_password', methods=['POST'])
def set_password():
    global PASSWORD, entered_password
    data = request.get_json()
    new_pass = data.get('password', '')
    if new_pass and len(new_pass) >= 1:
        with keypad_lock:
            PASSWORD = new_pass
            entered_password = ""
        return jsonify({'success': True})
    return jsonify({'success': False})

@app.route('/capture_training_photo')
def capture_training_photo():
    filename = f"static/train_{int(time.time())}.jpg"
    if not capture_with_opencv(filename):
        return jsonify({'success': False, 'message': 'Camera error'})
    with open(filename, 'rb') as f:
        img_data = f.read()
    b64 = base64.b64encode(img_data).decode('utf-8')
    data_url = f"data:image/jpeg;base64,{b64}"
    return jsonify({'success': True, 'image': data_url})

@app.route('/train_face', methods=['POST'])
def train_face():
    if not face_module_ok:
        return jsonify({'success': False, 'message': 'Face recognition module not installed.'})

    name = request.form.get('name', '').strip()
    files = request.files.getlist('images')

    if not name or not files:
        return jsonify({'success': False, 'message': 'Missing name or images'})

    face_rois = []

    for file in files:
        if file.filename == '':
            continue
        file_bytes = np.frombuffer(file.read(), np.uint8)
        img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = FACE_CASCADE.detectMultiScale(gray, 1.1, 5)
        if len(faces) == 1:
            (x, y, w, h) = faces[0]
            face_rois.append(gray[y:y+h, x:x+w])

    if len(face_rois) == 0:
        return jsonify({'success': False, 'message': 'No clear face detected. Use different angles.'})

    with face_lock:
        global label_names, recognizer
        recognizer = cv2.face.LBPHFaceRecognizer_create()
        label_names_new = {}
        person_id = 1
        label_names_new[person_id] = name
        for face_roi in face_rois:
            recognizer.update([face_roi], np.array([person_id]))

        label_names = label_names_new
        recognizer.write(RECOGNIZER_FILE)
        with open(LABELS_FILE, 'wb') as f:
            pickle.dump(label_names, f)

    return jsonify({'success': True, 'message': f'Face trained for {name} using {len(face_rois)} photos. Old data removed.'})

@app.route('/last_unlock')
def last_unlock():
    return jsonify({
        'message': last_unlock_message,
        'time': last_unlock_time
    })

# ================= START =================
if __name__ == '__main__':
    if not os.path.exists('static'):
        os.makedirs('static')

    t = threading.Thread(target=keypad_thread, daemon=True)
    t.start()

    # Plain HTTP â€“ no SSL
    app.run(host='0.0.0.0', port=5000, debug=False)