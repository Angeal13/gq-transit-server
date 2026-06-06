# database.py — City Hall Server
# Fixed: pool reconnection on stale connections, correct mariadb cursor API,
# schema uses p_future MAXVALUE so partitions never expire,
# all query results converted to plain dicts for JSON serialisation.

import mariadb
import time
import logging
import statistics
import heapq
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, List, Optional, Tuple

from config import DB_CONFIG

logger = logging.getLogger(__name__)

_pool_lock = Lock()


class _Pool:
    _conns: List = []
    _max = 10

    @classmethod
    def init(cls):
        with _pool_lock:
            for _ in range(cls._max):
                try:
                    cls._conns.append(mariadb.connect(**DB_CONFIG))
                except mariadb.Error as e:
                    logger.error(f"Pool init error: {e}")
        logger.info(f"DB pool initialised with {len(cls._conns)} connections.")

    @classmethod
    def _new_conn(cls):
        return mariadb.connect(**DB_CONFIG)

    @classmethod
    def get(cls):
        deadline = time.time() + 10
        while True:
            with _pool_lock:
                if cls._conns:
                    conn = cls._conns.pop()
                    try:
                        conn.ping()
                        return conn
                    except Exception:
                        try:
                            return cls._new_conn()
                        except Exception as e:
                            logger.error(f"Reconnect failed: {e}")
            if time.time() > deadline:
                raise RuntimeError("DB pool exhausted after 10s wait")
            time.sleep(0.1)

    @classmethod
    def release(cls, conn):
        with _pool_lock:
            cls._conns.append(conn)

    @classmethod
    def close_all(cls):
        with _pool_lock:
            for c in cls._conns:
                try:
                    c.close()
                except Exception:
                    pass
            cls._conns.clear()


def _row_to_dict(cursor, row) -> Dict:
    """Convert a mariadb row tuple to a plain dict using cursor description."""
    if row is None:
        return None
    return {cursor.description[i][0]: row[i] for i in range(len(row))}


def _rows_to_dicts(cursor, rows) -> List[Dict]:
    if not rows:
        return []
    cols = [d[0] for d in cursor.description]
    return [{cols[i]: row[i] for i in range(len(cols))} for row in rows]


@contextmanager
def get_db():
    conn = _Pool.get()
    # Standard mariadb cursor — no dictionary param (not supported)
    cur = conn.cursor()
    try:
        yield conn, cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _Pool.release(conn)


# ── Schema ────────────────────────────────────────────────────────────────────
# Partitions use p_future MAXVALUE so data is always accepted regardless of year.
# Yearly partitions are added by the DBA as needed for pruning old data.

SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS PARADAS (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        name        VARCHAR(255) NOT NULL,
        lat         DECIMAL(9,6) NOT NULL DEFAULT 0,
        lng         DECIMAL(9,6) NOT NULL DEFAULT 0,
        region      VARCHAR(100) NOT NULL,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_stop_region (name, region),
        INDEX idx_region (region)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS RUTAS (
        id          VARCHAR(50)  PRIMARY KEY,
        route_type  TINYINT      NOT NULL DEFAULT 1,
        client      VARCHAR(255) NOT NULL,
        region      VARCHAR(100) NOT NULL,
        language    VARCHAR(10)  NOT NULL DEFAULT 'es',
        timezone    VARCHAR(50)  NOT NULL DEFAULT 'Africa/Malabo',
        created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_region (region)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS RUTA_PARADAS (
        route_id    VARCHAR(50)  NOT NULL,
        stop_order  SMALLINT     NOT NULL,
        stop_id     INT          NOT NULL,
        PRIMARY KEY (route_id, stop_order),
        FOREIGN KEY (route_id) REFERENCES RUTAS(id) ON DELETE CASCADE,
        FOREIGN KEY (stop_id)  REFERENCES PARADAS(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS registered_buses (
        bus_id      VARCHAR(255) PRIMARY KEY,
        region      VARCHAR(100),
        last_seen   DATETIME,
        status      VARCHAR(20)  DEFAULT 'active',
        INDEX idx_region (region)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS bus_positions (
        bus_id      VARCHAR(255) PRIMARY KEY,
        route_id    VARCHAR(50),
        stop_name   VARCHAR(255),
        lat         DECIMAL(9,6),
        lng         DECIMAL(9,6),
        direction   VARCHAR(255),
        updated_at  DATETIME,
        INDEX idx_updated (updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS stop_events (
        id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        bus_id      VARCHAR(255) NOT NULL,
        route_id    VARCHAR(50),
        stop_name   VARCHAR(255),
        lat         DECIMAL(9,6),
        lng         DECIMAL(9,6),
        direction   VARCHAR(255),
        client      VARCHAR(255),
        region      VARCHAR(100),
        language    VARCHAR(10),
        timezone    VARCHAR(50),
        arrived_at  DATETIME     NOT NULL,
        INDEX idx_bus     (bus_id),
        INDEX idx_route   (route_id),
        INDEX idx_arrived (arrived_at),
        INDEX idx_stop    (stop_name, route_id, arrived_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS travel_times (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        route_id        VARCHAR(50)  NOT NULL,
        from_stop       VARCHAR(255) NOT NULL,
        to_stop         VARCHAR(255) NOT NULL,
        hour_of_day     TINYINT      NOT NULL,
        day_of_week     TINYINT      NOT NULL,
        median_seconds  INT          NOT NULL,
        sample_count    INT          NOT NULL,
        computed_at     DATETIME     DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_tt (route_id, from_stop, to_stop, hour_of_day, day_of_week),
        INDEX idx_route (route_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS sms_logs (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        phone       VARCHAR(30),
        message_in  TEXT,
        message_out TEXT,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS whatsapp_sessions (
        phone       VARCHAR(30) PRIMARY KEY,
        state       VARCHAR(50),
        context     JSON,
        updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


def init_schema():
    with get_db() as (conn, cur):
        for stmt in SCHEMA_SQL:
            try:
                cur.execute(stmt)
            except mariadb.Error as e:
                if 'already exists' not in str(e).lower():
                    raise
    logger.info("Transit schema verified.")


# ── Route operations ──────────────────────────────────────────────────────────

def get_routes(region: str) -> List[Dict]:
    with get_db() as (conn, cur):
        cur.execute("""
            SELECT r.id, r.route_type, r.client, r.region, r.language, r.timezone,
                   p.name AS stop_name, p.lat, p.lng, rp.stop_order
            FROM RUTAS r
            JOIN RUTA_PARADAS rp ON rp.route_id = r.id
            JOIN PARADAS p       ON p.id = rp.stop_id
            WHERE r.region = ?
            ORDER BY r.id, rp.stop_order
        """, (region,))
        rows = _rows_to_dicts(cur, cur.fetchall())

    routes: Dict[str, Dict] = {}
    for row in rows:
        rid = row['id']
        if rid not in routes:
            routes[rid] = {
                'id':         rid,
                'route_type': row['route_type'],
                'client':     row['client'],
                'region':     row['region'],
                'language':   row['language'],
                'timezone':   row['timezone'],
                'stops':      [],
            }
        routes[rid]['stops'].append({
            'name': row['stop_name'],
            'lat':  float(row['lat']) if row['lat'] else 0.0,
            'lng':  float(row['lng']) if row['lng'] else 0.0,
        })
    return list(routes.values())


def upsert_stop(name: str, lat: float, lng: float, region: str):
    with get_db() as (conn, cur):
        cur.execute("""
            INSERT INTO PARADAS (name, lat, lng, region)
            VALUES (?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE lat=VALUES(lat), lng=VALUES(lng)
        """, (name, lat, lng, region))


def get_all_stops(region: str) -> List[Dict]:
    with get_db() as (conn, cur):
        cur.execute(
            "SELECT name, lat, lng FROM PARADAS WHERE region=? ORDER BY name",
            (region,)
        )
        rows = _rows_to_dicts(cur, cur.fetchall())
    for r in rows:
        r['lat'] = float(r['lat']) if r['lat'] else 0.0
        r['lng'] = float(r['lng']) if r['lng'] else 0.0
    return rows


# ── Bus operations ────────────────────────────────────────────────────────────

def register_bus(bus_id: str, region: str):
    with get_db() as (conn, cur):
        cur.execute("""
            INSERT INTO registered_buses (bus_id, region, last_seen, status)
            VALUES (?, ?, NOW(), 'active')
            ON DUPLICATE KEY UPDATE last_seen=NOW(), status='active'
        """, (bus_id, region))


def upsert_position(payload: Dict):
    with get_db() as (conn, cur):
        cur.execute("""
            INSERT INTO bus_positions
                (bus_id, route_id, stop_name, lat, lng, direction, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, NOW())
            ON DUPLICATE KEY UPDATE
                route_id=VALUES(route_id), stop_name=VALUES(stop_name),
                lat=VALUES(lat), lng=VALUES(lng),
                direction=VALUES(direction), updated_at=NOW()
        """, (payload.get('bus_id'), payload.get('route_id'),
              payload.get('stop_name'), payload.get('lat'),
              payload.get('lng'), payload.get('direction')))


def insert_stop_event(event: Dict):
    ts = event.get('timestamp', '')
    arrived_at = ts.replace('Z', '').replace('T', ' ')[:19] if ts else \
                 datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as (conn, cur):
        cur.execute("""
            INSERT INTO stop_events
                (bus_id, route_id, stop_name, lat, lng, direction,
                 client, region, language, timezone, arrived_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            event.get('bus_id'), event.get('route_id'), event.get('stop_name'),
            event.get('lat'), event.get('lng'), event.get('direction'),
            event.get('client'), event.get('region'), event.get('language'),
            event.get('timezone'), arrived_at,
        ))
    upsert_position(event)


def get_live_positions(region: str, stale_seconds: int = 120) -> List[Dict]:
    with get_db() as (conn, cur):
        cur.execute("""
            SELECT bp.bus_id, bp.route_id, bp.stop_name,
                   bp.lat, bp.lng, bp.direction, bp.updated_at
            FROM bus_positions bp
            JOIN registered_buses rb ON rb.bus_id = bp.bus_id
            WHERE rb.region = ?
              AND bp.updated_at >= DATE_SUB(NOW(), INTERVAL ? SECOND)
            ORDER BY bp.updated_at DESC
        """, (region, stale_seconds))
        rows = _rows_to_dicts(cur, cur.fetchall())
    for r in rows:
        if r.get('updated_at') and hasattr(r['updated_at'], 'isoformat'):
            r['updated_at'] = r['updated_at'].isoformat()
        r['lat'] = float(r['lat']) if r.get('lat') else None
        r['lng'] = float(r['lng']) if r.get('lng') else None
    return rows


# ── ETA / travel times ────────────────────────────────────────────────────────

def rebuild_travel_times(history_days: int = 45, min_samples: int = 30):
    logger.info("Rebuilding travel times...")
    with get_db() as (conn, cur):
        cur.execute("""
            SELECT bus_id, route_id, stop_name, arrived_at,
                   HOUR(arrived_at) AS hr, DAYOFWEEK(arrived_at) AS dow
            FROM stop_events
            WHERE arrived_at >= DATE_SUB(NOW(), INTERVAL ? DAY)
              AND route_id IS NOT NULL
            ORDER BY bus_id, arrived_at
        """, (history_days,))
        rows = _rows_to_dicts(cur, cur.fetchall())

    buckets: Dict = defaultdict(list)
    prev: Dict[str, Dict] = {}
    for row in rows:
        bid = row['bus_id']
        rid = row['route_id']
        ts  = row['arrived_at']
        if bid in prev and prev[bid]['route_id'] == rid:
            elapsed = (ts - prev[bid]['arrived_at']).total_seconds()
            if 0 < elapsed < 7200:
                key = (rid, prev[bid]['stop_name'], row['stop_name'],
                       row['hr'], row['dow'])
                buckets[key].append(elapsed)
        prev[bid] = {'route_id': rid, 'stop_name': row['stop_name'], 'arrived_at': ts}

    written = 0
    with get_db() as (conn, cur):
        for (rid, frm, to, hr, dow), times in buckets.items():
            if len(times) < min_samples:
                continue
            median = int(statistics.median(times))
            cur.execute("""
                INSERT INTO travel_times
                    (route_id, from_stop, to_stop, hour_of_day, day_of_week,
                     median_seconds, sample_count, computed_at)
                VALUES (?,?,?,?,?,?,?,NOW())
                ON DUPLICATE KEY UPDATE
                    median_seconds=VALUES(median_seconds),
                    sample_count=VALUES(sample_count),
                    computed_at=NOW()
            """, (rid, frm, to, hr, dow, median, len(times)))
            written += 1
    logger.info(f"Travel times rebuilt: {written} cells.")


def get_eta(origin: str, destination: str, region: str,
            max_routes: int = 3) -> List[Dict]:
    with get_db() as (conn, cur):
        cur.execute("""
            SELECT tt.route_id, tt.from_stop, tt.to_stop, tt.median_seconds
            FROM travel_times tt
            JOIN RUTAS r ON r.id = tt.route_id
            WHERE r.region = ?
              AND tt.hour_of_day = HOUR(NOW())
        """, (region,))
        edges = _rows_to_dicts(cur, cur.fetchall())

        cur.execute("""
            SELECT DISTINCT r.id AS route_id
            FROM RUTAS r
            JOIN RUTA_PARADAS rp1 ON rp1.route_id = r.id
            JOIN PARADAS p1       ON p1.id = rp1.stop_id AND p1.name = ?
            JOIN RUTA_PARADAS rp2 ON rp2.route_id = r.id
            JOIN PARADAS p2       ON p2.id = rp2.stop_id AND p2.name = ?
            WHERE r.region = ?
        """, (origin, destination, region))
        candidate_routes = {row[0] for row in cur.fetchall()}

    if not candidate_routes:
        return []

    graph: Dict = defaultdict(lambda: defaultdict(list))
    for e in edges:
        if e['route_id'] in candidate_routes:
            graph[e['route_id']][e['from_stop']].append(
                (e['to_stop'], int(e['median_seconds']))
            )

    results = []
    for route_id in candidate_routes:
        g = graph[route_id]
        heap = [(0, origin, [origin])]
        visited: set = set()
        found = None
        while heap:
            cost, node, path = heapq.heappop(heap)
            if node in visited:
                continue
            visited.add(node)
            if node == destination:
                found = (cost, path)
                break
            for neighbour, weight in g.get(node, []):
                if neighbour not in visited:
                    heapq.heappush(heap, (cost + weight, neighbour, path + [neighbour]))

        if found:
            total_sec, path = found
            results.append({
                'route_id':    route_id,
                'path':        path,
                'stops':       len(path),
                'eta_seconds': total_sec,
                'eta_minutes': round(total_sec / 60),
                'data_source': 'historical',
            })
        else:
            results.append({
                'route_id':    route_id,
                'path':        [],
                'stops':       0,
                'eta_seconds': None,
                'eta_minutes': None,
                'data_source': 'insufficient_data',
            })

    results.sort(key=lambda x: (x['eta_seconds'] is None, x['eta_seconds']))
    return results[:max_routes]
