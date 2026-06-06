# config.py — City Hall Server
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/server.log', mode='a'),
    ]
)

DB_CONFIG = {
    'host':            os.getenv('DB_HOST',     '127.0.0.1'),
    'port':            int(os.getenv('DB_PORT', '3306')),
    'user':            os.getenv('DB_USER',     'bioko_app'),
    'password':        os.getenv('DB_PASSWORD', 'CHANGE_ME'),
    'database':        os.getenv('DB_NAME',     'bus_tracking_gq'),
    'connect_timeout': 10,
    'autocommit':      False,
}

FLASK_CONFIG = {
    'host':       os.getenv('FLASK_HOST',  '0.0.0.0'),
    'port':       int(os.getenv('FLASK_PORT', '5000')),
    'debug':      os.getenv('FLASK_DEBUG', 'false').lower() == 'true',
    'secret_key': os.getenv('SECRET_KEY', 'CHANGE_ME_SECRET'),
}

API_KEY                = os.getenv('API_KEY',                  'BIOKO_BUS_KEY_CHANGE_ME')
POSITION_STALE_SECONDS = int(os.getenv('POSITION_STALE_SECONDS', '120'))

ETA_CONFIG = {
    'min_samples':      int(os.getenv('ETA_MIN_SAMPLES',  '30')),
    'history_days':     int(os.getenv('ETA_HISTORY_DAYS', '45')),
    'max_routes':       3,
    'rebuild_interval': 3600,
}

SMS_CONFIG = {
    'username':  os.getenv('AT_USERNAME', ''),
    'api_key':   os.getenv('AT_API_KEY',  ''),
    'sender_id': os.getenv('AT_SENDER',   'BIOKO'),
    'enabled':   bool(os.getenv('AT_USERNAME')),
}

WHATSAPP_CONFIG = {
    'base_url': os.getenv('WA_BASE_URL',  'http://localhost:8080'),
    'api_key':  os.getenv('WA_API_KEY',   ''),
    'instance': os.getenv('WA_INSTANCE',  'bioko'),
    'enabled':  bool(os.getenv('WA_API_KEY')),
}
