# admin_routes.py — City Hall Server
# Fixed: stop list is now a JSON array from the web form, not a comma-split string.
# This allows stop names containing commas (e.g. "Calle 5, Barrio Norte").

import logging
from flask import Blueprint, request, jsonify
import database as db
from config import API_KEY

logger   = logging.getLogger(__name__)
admin_bp = Blueprint('admin', __name__, url_prefix='/admin/api')


@admin_bp.route('/stop', methods=['POST'])
def admin_add_stop():
    data   = request.get_json(force=True) or {}
    name   = str(data.get('name', '')).strip()
    lat    = data.get('lat')
    lng    = data.get('lng')
    region = str(data.get('region', 'Bioko')).strip()
    if not name or lat is None or lng is None:
        return jsonify({'error': 'name, lat and lng are required'}), 400
    try:
        db.upsert_stop(name, float(lat), float(lng), region)
        return jsonify({'status': 'ok', 'message': f"Stop '{name}' saved."})
    except (ValueError, TypeError):
        return jsonify({'error': 'lat and lng must be valid numbers'}), 400
    except Exception as e:
        logger.error(f"Stop save error: {e}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/route', methods=['POST'])
def admin_add_route():
    data       = request.get_json(force=True) or {}
    route_id   = str(data.get('id', '')).strip()
    client     = str(data.get('client', '')).strip()
    route_type = int(data.get('type', 1))
    language   = str(data.get('language', 'es')).strip()
    timezone   = str(data.get('timezone', 'Africa/Malabo')).strip()
    region     = str(data.get('region', 'Bioko')).strip()

    # Accept either a JSON array (preferred) or a comma-separated string (legacy)
    raw_stops = data.get('stops', [])
    if isinstance(raw_stops, list):
        stop_names = [str(s).strip() for s in raw_stops if str(s).strip()]
    else:
        # Legacy: comma-separated — warn that names with commas will break
        stop_names = [s.strip() for s in str(raw_stops).split(',') if s.strip()]

    if not route_id or not stop_names:
        return jsonify({'error': 'id and stops are required'}), 400

    try:
        with db.get_db() as (conn, cur):
            cur.execute("""
                INSERT INTO RUTAS (id, route_type, client, region, language, timezone)
                VALUES (?,?,?,?,?,?)
                ON DUPLICATE KEY UPDATE
                    route_type=VALUES(route_type), client=VALUES(client),
                    language=VALUES(language), timezone=VALUES(timezone)
            """, (route_id, route_type, client, region, language, timezone))

            cur.execute("DELETE FROM RUTA_PARADAS WHERE route_id=?", (route_id,))
            missing = []
            for order, name in enumerate(stop_names):
                cur.execute(
                    "SELECT id FROM PARADAS WHERE name=? AND region=?",
                    (name, region)
                )
                row = cur.fetchone()
                if not row:
                    missing.append(name)
                    continue
                cur.execute(
                    "INSERT INTO RUTA_PARADAS (route_id, stop_order, stop_id) VALUES (?,?,?)",
                    (route_id, order, row[0])
                )

        saved = len(stop_names) - len(missing)
        msg = f"Route '{route_id}' saved with {saved} stops."
        if missing:
            msg += f" Missing (add first): {', '.join(missing)}"
        return jsonify({'status': 'ok', 'message': msg})

    except Exception as e:
        logger.error(f"Route save error: {e}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/rebuild-eta', methods=['POST'])
def admin_rebuild_eta():
    try:
        from config import ETA_CONFIG
        db.rebuild_travel_times(
            history_days=ETA_CONFIG['history_days'],
            min_samples=ETA_CONFIG['min_samples'],
        )
        return jsonify({'status': 'ok', 'message': 'Travel times rebuilt.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
