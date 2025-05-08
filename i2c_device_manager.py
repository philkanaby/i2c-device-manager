import eventlet
# Enable eventlet's monkey patching for asynchronous operations
eventlet.monkey_patch()  # Must be the very first thing to avoid import conflicts

import json
import time
import importlib
import smbus
import os
import logging
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('i2c_manager.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
socketio = SocketIO(app, async_mode='eventlet')  # Set async_mode to 'eventlet'

# Configuration file
CONFIG_FILE = 'i2c_config.json'

# Device instances and configuration
device_instances = {}
config = {
    "connections": [],
    "new_connections": [],
    "archived_connections": []
}
reading_tasks = {}

def load_config():
    global config
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        save_config()

def save_config():
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

def scan_i2c_bus(bus_number=1):
    bus = smbus.SMBus(bus_number)
    devices = []
    for address in range(128):
        try:
            bus.read_byte(address)
            devices.append(hex(address))
        except:
            pass
    return devices

def start_reading_task(addr):
    if addr in reading_tasks:
        return
    def task():
        while True:
            load_config()
            conn = next((c for c in config['connections'] if c['address'] == addr), None)
            if not conn or not conn.get('active') or 'read_interval' not in conn or conn['read_interval'] <= 0:
                break
            interval = conn['read_interval']
            try:
                data = device_instances[addr].read()
                socketio.emit('device_data', {'address': addr, 'data': data})
            except Exception as e:
                logger.info(f"Error reading device {addr}: {e}")
            eventlet.sleep(interval)
    reading_tasks[addr] = eventlet.spawn(task)

def stop_reading_task(addr):
    if addr in reading_tasks:
        reading_tasks[addr].kill()
        del reading_tasks[addr]

def manage_reading_tasks():
    active_reading_devices = {conn['address'] for conn in config['connections'] if conn.get('active') and 'read_interval' in conn and conn['read_interval'] > 0}
    for addr in list(reading_tasks.keys()):
        if addr not in active_reading_devices:
            stop_reading_task(addr)
    for addr in active_reading_devices:
        if addr not in reading_tasks:
            start_reading_task(addr)

def update_device_status():
    while True:
        current_devices = scan_i2c_bus()
        load_config()
        
        # Update existing connections
        for conn in config['connections'][:]:
            if conn['address'] not in current_devices and conn['active']:
                conn['active'] = 0
                config['archived_connections'].append(conn)
                config['connections'].remove(conn)
                if conn['address'] in device_instances:
                    del device_instances[conn['address']]
                logger.info(f"Archived device {conn['address']} ({conn['name']})")
        
        # Check archived connections
        for conn in config['archived_connections'][:]:
            if conn['address'] in current_devices and not conn['active']:
                conn['active'] = 1
                config['connections'].append(conn)
                config['archived_connections'].remove(conn)
                initialize_device(conn)
                logger.info(f"Restored device {conn['address']} ({conn['name']}) from archived")
        
        # Clean up new_connections to avoid duplicates
        config['new_connections'] = [
            conn for conn in config['new_connections']
            if conn['address'] not in {c['address'] for c in config['connections'] + config['archived_connections']}
        ]
        
        # Detect new devices
        known_addresses = {conn['address'] for conn in config['connections'] + config['archived_connections']}
        for addr in current_devices:
            if addr not in known_addresses:
                config['new_connections'].append({
                    "address": addr,
                    "library": "",
                    "class": "",
                    "name": "Unknown Device",
                    "active": 0
                })
                logger.info(f"Detected new device at {addr}")
        
        save_config()
        socketio.emit('config_update', config)
        manage_reading_tasks()
        eventlet.sleep(5)  # Use eventlet.sleep instead of time.sleep

def initialize_device(conn):
    if conn['active'] and conn['address'] not in device_instances:
        if 'interface_module' not in conn or 'interface_class' not in conn:
            logger.info(f"Missing 'interface_module' or 'interface_class' in configuration for device {conn['address']}")
            return
        try:
            module_name = conn.get('interface_module')
            if not module_name:
                raise ValueError("Missing 'interface_module' in configuration")
            module = importlib.import_module(f"interfaces.{module_name}")
            class_name = conn.get('interface_class')
            if not class_name:
                raise ValueError("Missing 'interface_class' in configuration")
            device_class = getattr(module, class_name)
            device_instances[conn['address']] = device_class(address=int(conn['address'], 16))
        except Exception as e:
            logger.info(f"Error initializing device {conn['address']}: {e}")

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    load_config()
    emit('config_update', config)

@socketio.on('update_config')
def handle_update_config(updated_config):
    global config
    # Deduplicate the received configuration
    deduped_config = {
        "connections": [],
        "new_connections": [],
        "archived_connections": []
    }
    
    # Collect all connections
    all_conns = (
        updated_config.get('connections', []) +
        updated_config.get('new_connections', []) +
        updated_config.get('archived_connections', [])
    )
    
    # Deduplicate by address
    address_map = {}
    for conn in all_conns:
        addr = conn['address']
        if addr not in address_map:
            address_map[addr] = conn
        else:
            # Prioritize connections with interface_module and specific name
            existing = address_map[addr]
            if 'interface_module' in conn and conn['interface_module'] and ('interface_module' not in existing or not existing['interface_module']):
                address_map[addr] = conn
            elif 'name' in conn and conn['name'] != "Unknown Device" and ('name' not in existing or existing['name'] == "Unknown Device"):
                address_map[addr] = conn
        logger.info(f"Deduplicating: Keeping {conn['address']} with name {conn['name']}")

    # Rebuild config
    for addr, conn in address_map.items():
        if conn.get('active', 0) == 1:
            deduped_config['connections'].append(conn)
        elif any(c['address'] == addr for c in updated_config['new_connections']):
            deduped_config['new_connections'].append(conn)
        else:
            deduped_config['archived_connections'].append(conn)

    config = deduped_config
    save_config()
    for conn in config['connections']:
        if conn['active'] and conn['address'] not in device_instances:
            initialize_device(conn)
    emit('config_update', config, broadcast=True)

@socketio.on('write_device')
def handle_write_device(data):
    addr = data['address']
    value = data['value']
    channel = data.get('channel')
    logger.info(f"Attempting to write value {value} to device {addr}, channel {channel}")
    if addr in device_instances:
        try:
            if channel is not None:
                device_instances[addr].write(value, channel)
                logger.info(f"Successfully wrote value {value} to device {addr}, channel {channel}")
            else:
                device_instances[addr].write(value)
                logger.info(f"Successfully wrote value {value} to device {addr}")
        except Exception as e:
            logger.error(f"Error writing to device {addr}, channel {channel}: {str(e)}")
    else:
        logger.error(f"No device instance found for address {addr}")

@socketio.on('list_interfaces')
def handle_list_interfaces():
    interfaces = [f[:-3] for f in os.listdir('interfaces') if f.endswith('.py') and f != 'base.py']
    emit('interface_list', interfaces)

@socketio.on('get_interface_code')
def handle_get_interface_code(module_name):
    try:
        with open(f'interfaces/{module_name}.py', 'r') as f:
            code = f.read()
        emit('interface_code', {'module_name': module_name, 'code': code})
    except Exception as e:
        emit('interface_code', {'error': str(e)})

@socketio.on('save_interface_code')
def handle_save_interface_code(data):
    module_name = data['module_name']
    code = data['code']
    try:
        with open(f'interfaces/{module_name}.py', 'w') as f:
            f.write(code)
        emit('save_status', {'status': 'success', 'module_name': module_name})
    except Exception as e:
        emit('save_status', {'status': 'error', 'error': str(e)})

if __name__ == '__main__':
    load_config()
    for conn in config['connections']:
        initialize_device(conn)
    
    manage_reading_tasks()
    
    # Start background tasks using eventlet's GreenPool
    pool = eventlet.GreenPool()
    pool.spawn(update_device_status)
    
    # Run the app using eventlet's WSGI server
    eventlet.wsgi.server(eventlet.listen(('', 5000)), app)
