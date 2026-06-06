# app.py — City Hall Server
# Single clean application factory. No appended patches.
# Run with: gunicorn "app:create_app()"

import logging
import time
from functools import wraps
from threading import Thread

from flask import Flask, request, jsonify, render_template, abort

import database as db
import engine_database as edb
from config import (FLASK_CONFIG, API_KEY, POSITION_STALE_SECONDS, ETA_CONFIG)
from messaging import handle_sms, handle_whatsapp
from admin_routes import admin_bp
from engine_api import engine_bp, start_notifier
from relay_api import relay_bp, init_relay_schema

logger = logging.getLogger(__name__)


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-API-Key') or request.args.get('api_key')
        if key != API_KEY:
            abort(401)
        return f(*args, **kwargs)
    return decorated


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = FLASK_CONFIG['secret_key']

    # ── Initialise database schemas ───────────────────────────────────────────
    db._Pool.init()
    db.init_schema()
    edb.init_engine_schema()
    init_relay_schema()

    # ── Register blueprints ───────────────────────────────────────────────────
    app.register_blueprint(admin_bp)
    app.register_blueprint(engine_bp)
    app.register_blueprint(relay_bp)

    # ── Background threads ────────────────────────────────────────────────────
    def _eta_rebuild_loop():
        interval = ETA_CONFIG['rebuild_interval']
        while True:
            time.sleep(interval)
            try:
                db.rebuild_travel_times(
                    history_days=ETA_CONFIG['history_days'],
                    min_samples=ETA_CONFIG['min_samples'],
                )
            except Exception as e:
                logger.error(f"ETA rebuild error: {e}")

    Thread(target=_eta_rebuild_loop, daemon=True, name='eta-rebuild').start()
    start_notifier()

    # ── Transit API — Pi bus facing ───────────────────────────────────────────

    @app.route('/api/routes')
    @require_api_key
    def api_routes():
        region = request.args.get('region', 'Bioko')
        return jsonify(db.get_routes(region))

    @app.route('/api/bus/register', methods=['POST'])
    @require_api_key
    def api_register():
        data = request.get_json(force=True) or {}
        bus_id = data.get('bus_id', '').strip()
        if not bus_id:
            return jsonify({'error': 'bus_id required'}), 400
        db.register_bus(bus_id, data.get('region', 'Bioko'))
        return jsonify({'status': 'ok'})

    @app.route('/api/bus/stop', methods=['POST'])
    @require_api_key
    def api_stop_event():
        event = request.get_json(force=True) or {}
        if not event.get('bus_id'):
            return jsonify({'error': 'bus_id required'}), 400
        db.insert_stop_event(event)
        return jsonify({'status': 'ok'})

    @app.route('/api/bus/heartbeat', methods=['POST'])
    @require_api_key
    def api_heartbeat():
        payload = request.get_json(force=True) or {}
        if not payload.get('bus_id'):
            return jsonify({'error': 'bus_id required'}), 400
        db.upsert_position(payload)
        return jsonify({'status': 'ok'})

    # ── Rider-facing API ──────────────────────────────────────────────────────

    @app.route('/api/positions')
    def api_positions():
        region = request.args.get('region', 'Bioko')
        return jsonify(db.get_live_positions(region, POSITION_STALE_SECONDS))

    @app.route('/api/eta')
    def api_eta():
        origin = request.args.get('from', '').strip()
        dest   = request.args.get('to',   '').strip()
        region = request.args.get('region', 'Bioko')
        if not origin or not dest:
            return jsonify({'error': 'Parameters "from" and "to" are required'}), 400
        return jsonify({
            'origin':      origin,
            'destination': dest,
            'routes':      db.get_eta(origin, dest, region, ETA_CONFIG['max_routes']),
        })

    @app.route('/api/stops')
    def api_stops():
        region = request.args.get('region', 'Bioko')
        return jsonify(db.get_all_stops(region))

    @app.route('/api/routes/map')
    def api_routes_map():
        return jsonify(db.get_routes(request.args.get('region', 'Bioko')))

    # ── Messaging webhooks ────────────────────────────────────────────────────

    @app.route('/api/sms', methods=['POST'])
    def sms_webhook():
        phone   = request.form.get('from', '')
        message = request.form.get('text', '').strip()
        return handle_sms(phone, message), 200, {'Content-Type': 'text/plain'}

    @app.route('/api/whatsapp', methods=['POST'])
    def whatsapp_webhook():
        data  = request.get_json(force=True) or {}
        reply = handle_whatsapp(data)
        return jsonify({'status': 'ok', 'reply': reply})

    # ── Web interfaces ────────────────────────────────────────────────────────

    @app.route('/')
    @app.route('/map/')
    def rider_map():
        return render_template('map.html')

    @app.route('/admin/')
    def admin_panel():
        return render_template('admin.html')

    @app.route('/admin/fleet/')
    def fleet_health():
        return render_template('fleet_health.html')

    logger.info("City Hall server ready.")
    return app


if __name__ == '__main__':
    application = create_app()
    application.run(
        host=FLASK_CONFIG['host'],
        port=FLASK_CONFIG['port'],
        debug=FLASK_CONFIG['debug'],
    )
