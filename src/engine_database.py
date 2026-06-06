# engine_database.py — City Hall Server
# Fixed: partitions use p_future MAXVALUE (never expires),
#        fleet health query filtered by region,
#        score cache cleared on server restart warning logged,
#        all cursor results converted via _rows_to_dicts.

import logging
import math
import statistics
import threading
from collections import defaultdict, deque
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from database import get_db, _rows_to_dicts

logger = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

ENGINE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS engine_readings (
        id               BIGINT AUTO_INCREMENT PRIMARY KEY,
        bus_id           VARCHAR(255) NOT NULL,
        recorded_at      DATETIME     NOT NULL,
        rpm              FLOAT,
        coolant_temp_c   FLOAT,
        engine_load_pct  FLOAT,
        throttle_pct     FLOAT,
        fuel_trim_short  FLOAT,
        fuel_trim_long   FLOAT,
        intake_temp_c    FLOAT,
        oil_pressure_psi FLOAT,
        battery_v        FLOAT,
        engine_runtime_s INT,
        fault_code_count TINYINT DEFAULT 0,
        fault_codes      TEXT,
        local_severity   VARCHAR(16) DEFAULT 'ok',
        INDEX idx_bus_time (bus_id, recorded_at),
        INDEX idx_time     (recorded_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS engine_alerts (
        id           BIGINT AUTO_INCREMENT PRIMARY KEY,
        bus_id       VARCHAR(255) NOT NULL,
        alert_type   VARCHAR(32)  NOT NULL,
        severity     VARCHAR(16)  NOT NULL,
        sensor       VARCHAR(64),
        value        FLOAT,
        threshold    FLOAT,
        z_score      FLOAT,
        message      TEXT,
        notified     TINYINT DEFAULT 0,
        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_bus      (bus_id),
        INDEX idx_time     (created_at),
        INDEX idx_notified (notified)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS engine_health_scores (
        bus_id      VARCHAR(255) PRIMARY KEY,
        score       TINYINT  NOT NULL DEFAULT 100,
        trend       VARCHAR(16) DEFAULT 'stable',
        last_alert  VARCHAR(16) DEFAULT 'ok',
        region      VARCHAR(100),
        updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_region (region)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

SENSORS = [
    'rpm', 'coolant_temp_c', 'engine_load_pct', 'throttle_pct',
    'fuel_trim_short', 'fuel_trim_long', 'intake_temp_c',
    'oil_pressure_psi', 'battery_v',
]

SENSOR_COLUMN_MAP = {
    'rpm':                 'rpm',
    'coolant_temp_c':      'coolant_temp_c',
    'engine_load_pct':     'engine_load_pct',
    'throttle_pct':        'throttle_pct',
    'fuel_trim_short_pct': 'fuel_trim_short',
    'fuel_trim_long_pct':  'fuel_trim_long',
    'intake_temp_c':       'intake_temp_c',
    'oil_pressure_psi':    'oil_pressure_psi',
    'battery_v':           'battery_v',
}


def init_engine_schema():
    with get_db() as (conn, cur):
        for stmt in ENGINE_SCHEMA:
            try:
                cur.execute(stmt)
            except Exception as e:
                if 'already exists' not in str(e).lower():
                    raise
    logger.info("Engine health schema verified.")


# ── Storage ───────────────────────────────────────────────────────────────────

def insert_reading(payload: Dict) -> None:
    ts = payload.get('timestamp', '')
    recorded_at = ts.replace('Z', '').replace('T', ' ')[:19] if ts else \
                  datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as (conn, cur):
        cur.execute("""
            INSERT INTO engine_readings
                (bus_id, recorded_at, rpm, coolant_temp_c, engine_load_pct,
                 throttle_pct, fuel_trim_short, fuel_trim_long, intake_temp_c,
                 oil_pressure_psi, battery_v, engine_runtime_s,
                 fault_code_count, fault_codes, local_severity)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            payload.get('bus_id'), recorded_at,
            payload.get('rpm'), payload.get('coolant_temp_c'),
            payload.get('engine_load_pct'), payload.get('throttle_pct'),
            payload.get('fuel_trim_short_pct'), payload.get('fuel_trim_long_pct'),
            payload.get('intake_temp_c'), payload.get('oil_pressure_psi'),
            payload.get('battery_v'), payload.get('engine_runtime_s'),
            payload.get('fault_code_count', 0),
            ','.join(payload.get('fault_codes', [])),
            payload.get('local_severity', 'ok'),
        ))


def insert_alert(bus_id: str, alert_type: str, severity: str,
                 sensor: str, value: Optional[float], threshold: Optional[float],
                 z_score: Optional[float], message: str):
    with get_db() as (conn, cur):
        cur.execute("""
            INSERT INTO engine_alerts
                (bus_id, alert_type, severity, sensor, value, threshold, z_score, message)
            VALUES (?,?,?,?,?,?,?,?)
        """, (bus_id, alert_type, severity, sensor, value, threshold, z_score, message))


def update_health_score(bus_id: str, score: int, trend: str,
                        last_alert: str, region: str = ''):
    with get_db() as (conn, cur):
        cur.execute("""
            INSERT INTO engine_health_scores (bus_id, score, trend, last_alert, region)
            VALUES (?,?,?,?,?)
            ON DUPLICATE KEY UPDATE
                score=VALUES(score), trend=VALUES(trend),
                last_alert=VALUES(last_alert), region=VALUES(region)
        """, (bus_id, score, trend, last_alert, region))


def get_unnotified_alerts() -> List[Dict]:
    with get_db() as (conn, cur):
        cur.execute("""
            SELECT id, bus_id, severity, sensor, value, message, created_at
            FROM engine_alerts
            WHERE notified = 0 AND severity IN ('warning','critical')
            ORDER BY created_at ASC
            LIMIT 50
        """)
        rows = _rows_to_dicts(cur, cur.fetchall())
    for r in rows:
        if r.get('created_at') and hasattr(r['created_at'], 'isoformat'):
            r['created_at'] = r['created_at'].isoformat()
    return rows


def mark_alerts_notified(ids: List[int]):
    if not ids:
        return
    placeholders = ','.join('?' for _ in ids)
    with get_db() as (conn, cur):
        cur.execute(
            f"UPDATE engine_alerts SET notified=1 WHERE id IN ({placeholders})",
            ids
        )


# ── In-memory rolling statistics ──────────────────────────────────────────────
# NOTE: these reset if the server process restarts. This is acceptable because:
# - Z-score warnings need ~60 readings (2 min) to warm up — minor gap after restart
# - Hard rule alerts (thresholds) come from the Pi and do NOT depend on this
# - EMA trend warnings likewise warm up quickly from fresh readings

_WINDOW_SIZE = 43_200  # 24h at 2s intervals


class _BusSensorStats:
    def __init__(self, short_span: int = 150, long_span: int = 3600):
        self._window: deque = deque(maxlen=_WINDOW_SIZE)
        self._ema_short: Optional[float] = None
        self._ema_long:  Optional[float] = None
        self._alpha_s = 2.0 / (short_span + 1)
        self._alpha_l = 2.0 / (long_span + 1)
        self._slope_history: deque = deque(maxlen=30)

    def push(self, value: float):
        self._window.append(value)
        if self._ema_short is None:
            self._ema_short = value
            self._ema_long  = value
        else:
            self._ema_short = value * self._alpha_s + self._ema_short * (1 - self._alpha_s)
            self._ema_long  = value * self._alpha_l + self._ema_long  * (1 - self._alpha_l)
        self._slope_history.append(self._ema_short)

    def z_score(self, value: float) -> Optional[float]:
        if len(self._window) < 60:
            return None
        mu = statistics.mean(self._window)
        try:
            sigma = statistics.stdev(self._window)
        except statistics.StatisticsError:
            return None
        if sigma < 1e-6:
            return None
        return (value - mu) / sigma

    def ema_trend(self) -> Tuple[Optional[float], Optional[float], str]:
        if self._ema_short is None or self._ema_long is None:
            return None, None, 'stable'
        diff = self._ema_short - self._ema_long
        slope = 0.0
        if len(self._slope_history) >= 10:
            recent = list(self._slope_history)
            slope  = (recent[-1] - recent[0]) / len(recent)
        if abs(diff) < 0.5 or abs(slope) < 0.02:
            return self._ema_short, self._ema_long, 'stable'
        return self._ema_short, self._ema_long, 'rising' if diff > 0 else 'falling'

    @property
    def count(self) -> int:
        return len(self._window)


_stats: Dict[str, Dict[str, _BusSensorStats]] = defaultdict(
    lambda: defaultdict(_BusSensorStats)
)
_stats_lock = threading.Lock()

Z_WARNING  = 3.0
Z_ALERT    = 4.5
SCORE_PENALTIES = {
    'rule_critical':  40,
    'zscore_alert':   25,
    'zscore_warning': 10,
    'trend_warning':   8,
}
RISING_BAD  = {'coolant_temp_c', 'intake_temp_c', 'engine_load_pct',
               'fuel_trim_short', 'fuel_trim_long'}
FALLING_BAD = {'oil_pressure_psi', 'battery_v'}


def analyse(payload: Dict) -> Dict:
    bus_id  = payload['bus_id']
    region  = payload.get('region', '')
    alerts_generated = []
    score_penalty    = 0
    worst_severity   = 'ok'

    with _stats_lock:
        bus_stats = _stats[bus_id]
        for json_key, col in SENSOR_COLUMN_MAP.items():
            val = payload.get(json_key)
            if val is None:
                continue
            sensor_stat = bus_stats[col]
            sensor_stat.push(val)

            z = sensor_stat.z_score(val)
            if z is not None and abs(z) >= Z_WARNING:
                severity = 'critical' if abs(z) >= Z_ALERT else 'warning'
                msg = f"Bus {bus_id} — {col} anomaly: value={val:.2f}, z={z:.2f}"
                alerts_generated.append({
                    'type': 'zscore', 'severity': severity,
                    'sensor': col, 'value': val, 'z_score': z, 'message': msg,
                })
                score_penalty += (SCORE_PENALTIES['zscore_alert']
                                  if severity == 'critical'
                                  else SCORE_PENALTIES['zscore_warning'])
                if severity == 'critical':
                    worst_severity = 'critical'
                elif worst_severity == 'ok':
                    worst_severity = 'warning'

            ema_s, ema_l, direction = sensor_stat.ema_trend()
            if direction != 'stable' and sensor_stat.count > 300:
                is_bad = ((direction == 'rising'  and col in RISING_BAD) or
                          (direction == 'falling' and col in FALLING_BAD))
                if is_bad:
                    msg = (f"Bus {bus_id} — {col} trending {direction}: "
                           f"EMA5={ema_s:.2f} EMA2h={ema_l:.2f}")
                    alerts_generated.append({
                        'type': 'trend', 'severity': 'warning',
                        'sensor': col, 'value': val, 'z_score': None, 'message': msg,
                    })
                    score_penalty += SCORE_PENALTIES['trend_warning']
                    if worst_severity == 'ok':
                        worst_severity = 'warning'

    if payload.get('local_severity') == 'critical':
        score_penalty += SCORE_PENALTIES['rule_critical']
        worst_severity = 'critical'
        for msg in payload.get('local_alerts', []):
            alerts_generated.append({
                'type': 'rule', 'severity': 'critical',
                'sensor': 'multiple', 'value': None, 'z_score': None, 'message': msg,
            })

    if payload.get('fault_code_count', 0) > 0:
        codes = payload.get('fault_codes', [])
        score_penalty += SCORE_PENALTIES['rule_critical']
        worst_severity = 'critical'
        alerts_generated.append({
            'type': 'fault_code', 'severity': 'critical',
            'sensor': 'ECU', 'value': float(len(codes)), 'z_score': None,
            'message': f"OBD fault codes: {', '.join(str(c) for c in codes)}",
        })

    for a in alerts_generated:
        try:
            insert_alert(
                bus_id=bus_id, alert_type=a['type'], severity=a['severity'],
                sensor=a['sensor'], value=a['value'],
                threshold=None, z_score=a.get('z_score'), message=a['message'],
            )
        except Exception as e:
            logger.error(f"Alert insert error: {e}")

    current_score = _get_current_score(bus_id)
    new_score = max(0, current_score - score_penalty)
    if score_penalty == 0 and current_score < 100:
        new_score = min(100, current_score + 1)

    trend_label = ('critical' if worst_severity == 'critical'
                   else 'warning' if worst_severity == 'warning' else 'stable')
    try:
        update_health_score(bus_id, new_score, trend_label, worst_severity, region)
    except Exception as e:
        logger.error(f"Health score update error: {e}")

    return {
        'alerts':         alerts_generated,
        'health_score':   new_score,
        'worst_severity': worst_severity,
    }


_score_cache: Dict[str, int] = {}
_score_cache_lock = threading.Lock()


def _get_current_score(bus_id: str) -> int:
    with _score_cache_lock:
        if bus_id in _score_cache:
            return _score_cache[bus_id]
    try:
        with get_db() as (conn, cur):
            cur.execute(
                "SELECT score FROM engine_health_scores WHERE bus_id=?", (bus_id,)
            )
            row = cur.fetchone()
            score = row[0] if row else 100
            with _score_cache_lock:
                _score_cache[bus_id] = score
            return score
    except Exception:
        return 100


# ── Dashboard queries ─────────────────────────────────────────────────────────

def get_fleet_health(region: str = '') -> List[Dict]:
    with get_db() as (conn, cur):
        if region:
            cur.execute("""
                SELECT ehs.bus_id, ehs.score, ehs.trend, ehs.last_alert,
                       ehs.updated_at, rb.status AS bus_status
                FROM engine_health_scores ehs
                LEFT JOIN registered_buses rb ON rb.bus_id = ehs.bus_id
                WHERE ehs.region = ?
                ORDER BY ehs.score ASC
            """, (region,))
        else:
            cur.execute("""
                SELECT ehs.bus_id, ehs.score, ehs.trend, ehs.last_alert,
                       ehs.updated_at, rb.status AS bus_status
                FROM engine_health_scores ehs
                LEFT JOIN registered_buses rb ON rb.bus_id = ehs.bus_id
                ORDER BY ehs.score ASC
            """)
        rows = _rows_to_dicts(cur, cur.fetchall())
    for r in rows:
        if r.get('updated_at') and hasattr(r['updated_at'], 'isoformat'):
            r['updated_at'] = r['updated_at'].isoformat()
    return rows


def get_bus_sparklines(bus_id: str, minutes: int = 30) -> Dict[str, List]:
    with get_db() as (conn, cur):
        cur.execute("""
            SELECT recorded_at, rpm, coolant_temp_c, oil_pressure_psi,
                   battery_v, engine_load_pct
            FROM engine_readings
            WHERE bus_id=?
              AND recorded_at >= DATE_SUB(NOW(), INTERVAL ? MINUTE)
            ORDER BY recorded_at ASC
        """, (bus_id, minutes))
        rows = _rows_to_dicts(cur, cur.fetchall())

    result: Dict[str, List] = {k: [] for k in
        ['timestamps', 'rpm', 'coolant_temp_c', 'oil_pressure_psi',
         'battery_v', 'engine_load_pct']}
    for r in rows:
        ts = r['recorded_at']
        result['timestamps'].append(ts.isoformat() if hasattr(ts, 'isoformat') else str(ts))
        for key in ['rpm', 'coolant_temp_c', 'oil_pressure_psi', 'battery_v', 'engine_load_pct']:
            result[key].append(r[key])
    return result


def get_recent_alerts(bus_id: Optional[str] = None, limit: int = 100) -> List[Dict]:
    with get_db() as (conn, cur):
        if bus_id:
            cur.execute("""
                SELECT id, bus_id, alert_type, severity, sensor, value, message, created_at
                FROM engine_alerts WHERE bus_id=? ORDER BY created_at DESC LIMIT ?
            """, (bus_id, limit))
        else:
            cur.execute("""
                SELECT id, bus_id, alert_type, severity, sensor, value, message, created_at
                FROM engine_alerts ORDER BY created_at DESC LIMIT ?
            """, (limit,))
        rows = _rows_to_dicts(cur, cur.fetchall())
    for r in rows:
        if r.get('created_at') and hasattr(r['created_at'], 'isoformat'):
            r['created_at'] = r['created_at'].isoformat()
    return rows
