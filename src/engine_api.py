# engine_api.py — City Hall Server
# Flask Blueprint for engine health. Fixed: fleet endpoint accepts region param.

import logging
import os
import threading
import time
from typing import List, Dict

from flask import Blueprint, request, jsonify
import requests as http

import engine_database as edb
from config import API_KEY, SMS_CONFIG, WHATSAPP_CONFIG

logger    = logging.getLogger(__name__)
engine_bp = Blueprint('engine', __name__, url_prefix='/api/engine')

_MECHANIC_PHONE = os.getenv('MECHANIC_PHONE', '')


def _auth_ok() -> bool:
    key = request.headers.get('X-API-Key') or request.args.get('api_key')
    return key == API_KEY


@engine_bp.route('/reading', methods=['POST'])
def ingest_reading():
    if not _auth_ok():
        return jsonify({'error': 'unauthorized'}), 401
    payload = request.get_json(force=True) or {}
    if not payload.get('bus_id'):
        return jsonify({'error': 'bus_id required'}), 400
    try:
        edb.insert_reading(payload)
        result = edb.analyse(payload)
        if result['alerts']:
            _notify_event.set()
        return jsonify({
            'status':       'ok',
            'health_score': result['health_score'],
            'severity':     result['worst_severity'],
            'alerts':       len(result['alerts']),
        })
    except Exception as e:
        logger.error(f"Engine ingest error: {e}")
        return jsonify({'error': str(e)}), 500


@engine_bp.route('/fleet')
def fleet_health():
    # Region filter: pass ?region=Bioko to see only one region's buses
    region = request.args.get('region', '')
    return jsonify(edb.get_fleet_health(region))


@engine_bp.route('/bus/<bus_id>/sparklines')
def bus_sparklines(bus_id):
    minutes = int(request.args.get('minutes', 30))
    return jsonify(edb.get_bus_sparklines(bus_id, minutes))


@engine_bp.route('/alerts')
def all_alerts():
    bus_id = request.args.get('bus_id')
    limit  = int(request.args.get('limit', 100))
    return jsonify(edb.get_recent_alerts(bus_id, limit))


@engine_bp.route('/bus/<bus_id>/alerts')
def bus_alerts(bus_id):
    return jsonify(edb.get_recent_alerts(bus_id, limit=50))


# ── Alert notifier ────────────────────────────────────────────────────────────

_notify_event = threading.Event()

_SEVERITY_EMOJI = {'critical': '🔴', 'warning': '🟡'}


def _format_sms(alert: Dict) -> str:
    emoji    = _SEVERITY_EMOJI.get(alert.get('severity', ''), '⚠')
    bus_short = str(alert.get('bus_id', ''))[-8:]
    sensor   = (alert.get('sensor') or 'sensor').replace('_', ' ')
    value    = alert.get('value')
    val_str  = f" ({value:.1f})" if value is not None else ''
    msg  = f"{emoji} BUS {bus_short} — {sensor.upper()}{val_str}\n"
    msg += str(alert.get('message', ''))[:100] + '\n'
    msg += f"Severity: {str(alert.get('severity', '')).upper()}"
    return msg[:160]


def _send_sms(phone: str, text: str):
    if not SMS_CONFIG.get('enabled') or not phone:
        logger.info(f"SMS (disabled): {text[:80]}")
        return
    try:
        import africastalking as at
        at.initialize(SMS_CONFIG['username'], SMS_CONFIG['api_key'])
        at.SMS.send(text, [phone], sender_id=SMS_CONFIG.get('sender_id', 'BIOKO'))
        logger.info(f"Alert SMS sent to {phone}")
    except Exception as e:
        logger.error(f"SMS send error: {e}")


def _send_whatsapp(phone: str, text: str):
    if not WHATSAPP_CONFIG.get('enabled') or not phone:
        return
    try:
        url = (f"{WHATSAPP_CONFIG['base_url']}/message/sendText/"
               f"{WHATSAPP_CONFIG['instance']}")
        http.post(url,
                  json={'number': phone, 'text': text},
                  headers={'apikey': WHATSAPP_CONFIG['api_key']},
                  timeout=8)
    except Exception as e:
        logger.error(f"WhatsApp alert error: {e}")


def _notifier_loop():
    while True:
        _notify_event.wait(timeout=30)
        _notify_event.clear()
        try:
            alerts = edb.get_unnotified_alerts()
            if not alerts:
                continue
            notified_ids = []
            for alert in alerts:
                if _MECHANIC_PHONE:
                    text = _format_sms(alert)
                    _send_sms(_MECHANIC_PHONE, text)
                    _send_whatsapp(_MECHANIC_PHONE, text)
                notified_ids.append(alert['id'])
                time.sleep(0.5)
            edb.mark_alerts_notified(notified_ids)
        except Exception as e:
            logger.error(f"Notifier loop error: {e}")


def start_notifier():
    t = threading.Thread(target=_notifier_loop, daemon=True, name='engine-notifier')
    t.start()
    logger.info("Engine alert notifier started.")
