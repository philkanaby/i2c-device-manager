from flask import Flask, send_file
from flask_socketio import SocketIO, emit
import json
import time
import threading
import smbus
import os

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# I2C bus configuration (assuming bus 1 for Raspberry Pi)
I2C_BUS = 1
bus = smbus.SMBus(I2C_BUS)

# Configuration file
CONFIG_FILE = "i2c_config.json"

# Default configuration
default_config = {
    "connections": [],
    "new_connections": [],
    "archived_connections": []
}

# Load or initialize configuration
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
else:
    config = default_config.copy()
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

# Device interfaces (imported dynamically)
device_interfaces = {}

# Thread lock for config access
config_lock = threading.Lock()

def save_config():
    """Save the configuration to i2c_config.json."""
    with config_lock:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)

def scan_bus():
    """Scan the I2C bus and update the configuration."""
    while True:
        with config_lock:
            # Scan I2C bus for devices (addresses 0x03 to 0x77)
            detected_addresses = []
            for addr in range(0x03, 0x78):
                try:
                    bus.read_byte(addr)
                    detected_addresses.append(hex(addr))
                except:
                    pass

            # Get current active addresses
            active_addresses = [conn['address'] for conn in config['connections']]

            # Move disconnected devices to archived_connections
            for conn in config['connections'][:]:
                if conn['address'] not in detected_addresses:
                    conn['active'] = 0
                    config['archived_connections'].append(conn)
                    config['connections'].remove(conn)
                    print(f"Archived device {conn['address']} ({conn['name']})")

            # Process detected devices
            for addr in detected_addresses:
                # Check if device exists in archived_connections
                archived_conn = next((conn for conn in config['archived_connections'] if conn['address'] == addr), None)
                if archived_conn:
                    # Move from archived to connections
                    archived_conn['active'] = 1
                    config['connections'].append(archived_conn)
                    config['archived_connections'].remove(archived_conn)
                    print(f"Restored device {addr} ({archived_conn['name']}) from archived")
                    continue

                # Check if device already in connections
                if addr in active_addresses:
                    continue

                # Check if device in new_connections (avoid duplicates)
                if any(conn['address'] == addr for conn in config['new_connections']):
                    continue

                # Add new device to new_connections
                new_conn = {
                    "address": addr,
                    "library": "",
                    "class": "",
                    "name": "Unknown Device",
                    "active": 0
                }
                config['new_connections'].append(new_conn)
                print(f"Detected new device at {addr}")

            save_config()
            socketio.emit('config_update', config)

        time.sleep(5)  # Scan every 5 seconds

def read_device(address):
    """Read data from a device periodically."""
    while True:
        with config_lock:
            conn = next((c for c in config['connections'] if c['address'] == address), None)
            if conn and conn.get('active', 0) == 1 and 'read_interval' in conn:
                try:
                    interface = device_interfaces.get(address)
                    if interface:
                        data = interface.read()
                        socketio.emit('device_data', {'address': address, 'data': data})
                except Exception as e:
                    print(f"Error reading device {address}: {e}")
                time.sleep(conn['read_interval'])
            else:
                time.sleep(1)  # Wait before retrying if not active

@app.route('/')
def serve_index():
    return send_file('index.html')

@socketio.on('write_device')
def handle_write_device(data):
    address = data.get('address')
    value = data.get('value')
    channel = data.get('channel')
    interface = device_interfaces.get(address)
    if interface:
        try:
            interface.write(channel=channel, value=value)
            print(f"Wrote to device {address}, channel {channel}, value {value}")
        except Exception as e:
            print(f"Error writing to device {address}: {e}")

@socketio.on('update_config')
def handle_update_config(new_config):
    global config
    with config_lock:
        # Validate and deduplicate the new configuration
        deduped_config = {
            "connections": [],
            "new_connections": [],
            "archived_connections": []
        }
        
        # Collect all connections
        all_conns = (
            new_config.get('connections', []) +
            new_config.get('new_connections', []) +
            new_config.get('archived_connections', [])
        )
        
        # Deduplicate by address
        address_map = {}
        for conn in all_conns:
            addr = conn['address']
            if addr not in address_map:
                address_map[addr] = conn
            else:
                # Prioritize connections, then archived, then new
                existing = address_map[addr]
                if 'interface_module' in conn and conn['interface_module'] and ('interface_module' not in existing or not existing['interface_module']):
                    address_map[addr] = conn
                elif 'name' in conn and conn['name'] != "Unknown Device" and ('name' not in existing or existing['name'] == "Unknown Device"):
                    address_map[addr] = conn

        # Rebuild config
        for addr, conn in address_map.items():
            if conn.get('active', 0) == 1:
                deduped_config['connections'].append(conn)
            elif any(c['address'] == addr for c in new_config['new_connections']):
                deduped_config['new_connections'].append(conn)
            else:
                deduped_config['archived_connections'].append(conn)

        config = deduped_config
        save_config()
        socketio.emit('config_update', config)
        
        # Update device interfaces
        device_interfaces.clear()
        for conn in config['connections']:
            if conn.get('active', 0) == 1:
                try:
                    module = __import__(conn['interface_module'])
                    interface_class = getattr(module, conn['interface_class'])
                    device_interfaces[conn['address']] = interface_class(conn['address'])
                    print(f"Initialized interface for {conn['address']} ({conn['name']})")
                    # Start read thread for devices with read_interval
                    if 'read_interval' in conn:
                        threading.Thread(target=read_device, args=(conn['address'],), daemon=True).start()
                except Exception as e:
                    print(f"Error initializing interface for {conn['address']}: {e}")

if __name__ == '__main__':
    # Start bus scanning thread
    threading.Thread(target=scan_bus, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
