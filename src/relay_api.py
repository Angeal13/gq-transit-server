# relay_api.py — City Hall Server
# Relay node health reporting and status API.

import logging
from flask import Blueprint, request, jsonify
from database import get_db, _rows_to_dicts
from config import API_KEY

logger   = logging.getLogger(__name__)
relay_bp = Blueprint('relay', __name__, url_prefix='/api/relay')

RELAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_nodes (
    node_name       VARCHAR(50) PRIMARY KEY,
    backbone_up     TINYINT  DEFAULT 1,
    cache_size      INT      DEFAULT 0,
    connected_buses INT      DEFAULT 0,
    signal_dbm      VARCHAR(20),
    last_seen       DATETIME,
    INDEX idx_seen (last_seen)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def init_relay_schema():
    with get_db() as (conn, cur):
        cur.execute(RELAY_SCHEMA)
    logger.info("Relay schema verified.")


def _auth():
    key = request.headers.get('X-API-Key') or request.args.get('api_key')
    return key == API_KEY


@relay_bp.route('/health', methods=['POST'])
def relay_health():
    if not _auth():
        return jsonify({'error': 'unauthorized'}), 401
    data  = request.get_json(force=True) or {}
    node  = str(data.get('node_name', 'unknown')).strip()
    buses = data.get('connected_buses', [])
    with get_db() as (conn, cur):
        cur.execute("""
            INSERT INTO relay_nodes
                (node_name, backbone_up, cache_size, connected_buses, signal_dbm, last_seen)
            VALUES (?,?,?,?,?,NOW())
            ON DUPLICATE KEY UPDATE
                backbone_up=VALUES(backbone_up),
                cache_size=VALUES(cache_size),
                connected_buses=VALUES(connected_buses),
                signal_dbm=VALUES(signal_dbm),
                last_seen=NOW()
        """, (
            node,
            1 if data.get('backbone_up') else 0,
            int(data.get('cache_size', 0)),
            len(buses) if isinstance(buses, list) else int(buses),
            str(data.get('signal', {}).get('signal_dbm', 'unknown')),
        ))
    logger.info(f"Relay health from {node}: up={data.get('backbone_up')}, "
                f"buses={len(buses)}, cache={data.get('cache_size', 0)}")
    return jsonify({'status': 'ok'})


@relay_bp.route('/status')
def relay_status():
    with get_db() as (conn, cur):
        cur.execute("""
            SELECT node_name, backbone_up, cache_size, connected_buses,
                   signal_dbm, last_seen
            FROM relay_nodes ORDER BY node_name
        """)
        rows = _rows_to_dicts(cur, cur.fetchall())
    for r in rows:
        if r.get('last_seen') and hasattr(r['last_seen'], 'isoformat'):
            r['last_seen'] = r['last_seen'].isoformat()
    return jsonify(rows)
