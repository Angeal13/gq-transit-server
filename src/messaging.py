# messaging.py — City Hall Server
# Fixed: SMS parsing uses '>' separator between origin and destination
# so multi-word stop names work correctly.
# Format: PARADA Mercado Central > Aeropuerto Internacional
# The '>' separator is unambiguous regardless of stop name length.

import logging
import requests
from typing import Dict, Optional

import database as db
from config import SMS_CONFIG, WHATSAPP_CONFIG

logger = logging.getLogger(__name__)

FR_WORDS = {'bonjour', 'aller', 'prochain', 'bus', 'arret', 'aide', 'merci', 'de'}
EN_WORDS = {'hello', 'next', 'bus', 'stop', 'help', 'from', 'to', 'hi', 'when'}


def detect_language(text: str) -> str:
    words = set(text.lower().split())
    if words & FR_WORDS:
        return 'fr'
    if words & EN_WORDS:
        return 'en'
    return 'es'


STRINGS = {
    'es': {
        'help': (
            "Bioko Transporte:\n"
            "• Horario: PARADA origen > destino\n"
            "  Ej: PARADA Mercado > Aeropuerto\n"
            "• Buses activos: BUSES"
        ),
        'no_routes':  "No hay rutas entre {origin} y {destination}.",
        'no_data':    "Datos ETA disponibles después del día 45.",
        'eta_header': "Opciones {origin} → {destination}:",
        'eta_line':   "Ruta {rid}: ~{min} min ({stops} paradas)",
        'buses_none': "Sin buses activos ahora.",
        'buses_line': "Bus {bus} — {stop} → {dir}",
        'invalid':    "No entendí. Escribe AYUDA para instrucciones.",
        'format_err': "Formato: PARADA origen > destino\nEj: PARADA Mercado > Aeropuerto",
    },
    'fr': {
        'help': (
            "Bioko Transport:\n"
            "• Horaire: ARRET depart > arrivee\n"
            "  Ex: ARRET Marche > Aeroport\n"
            "• Bus actifs: BUS"
        ),
        'no_routes':  "Aucune route entre {origin} et {destination}.",
        'no_data':    "Données ETA disponibles après 45 jours.",
        'eta_header': "Options {origin} → {destination}:",
        'eta_line':   "Route {rid}: ~{min} min ({stops} arrêts)",
        'buses_none': "Aucun bus actif.",
        'buses_line': "Bus {bus} — {stop} → {dir}",
        'invalid':    "Non compris. Tapez AIDE pour les instructions.",
        'format_err': "Format: ARRET depart > arrivee",
    },
    'en': {
        'help': (
            "Bioko Transit:\n"
            "• ETA: STOP origin > destination\n"
            "  E.g: STOP Market > Airport\n"
            "• Live buses: BUSES"
        ),
        'no_routes':  "No routes between {origin} and {destination}.",
        'no_data':    "ETA data available after day 45.",
        'eta_header': "Options {origin} → {destination}:",
        'eta_line':   "Route {rid}: ~{min} min ({stops} stops)",
        'buses_none': "No active buses.",
        'buses_line': "Bus {bus} — {stop} → {dir}",
        'invalid':    "Didn't understand. Text HELP for instructions.",
        'format_err': "Format: STOP origin > destination",
    },
}


def _s(lang: str, key: str, **kwargs) -> str:
    tmpl = STRINGS.get(lang, STRINGS['es']).get(key, '')
    return tmpl.format(**kwargs) if kwargs else tmpl


def _handle_query(text: str, phone: str, region: str = 'Bioko') -> str:
    lang  = detect_language(text)
    upper = text.strip().upper()

    if upper in ('HELP', 'AYUDA', 'AIDE', 'H', '?'):
        return _s(lang, 'help')

    if upper in ('BUSES', 'BUS'):
        positions = db.get_live_positions(region)
        if not positions:
            return _s(lang, 'buses_none')
        lines = []
        for p in positions[:8]:
            lines.append(_s(lang, 'buses_line',
                            bus=str(p.get('bus_id', ''))[-5:],
                            stop=p.get('stop_name', '—'),
                            dir=p.get('direction', '?')))
        return '\n'.join(lines)

    # PARADA / ARRET / STOP  origin > destination
    # The '>' separator is required to distinguish multi-word stop names.
    trigger_words = {'PARADA', 'ARRET', 'STOP', 'CUANDO', 'CUÁNDO', 'ETA'}
    parts = text.strip().split(None, 1)
    if len(parts) >= 2 and parts[0].upper() in trigger_words:
        remainder = parts[1]
        if '>' not in remainder:
            return _s(lang, 'format_err')
        sep_idx    = remainder.index('>')
        origin     = remainder[:sep_idx].strip().title()
        destination = remainder[sep_idx+1:].strip().title()
        if not origin or not destination:
            return _s(lang, 'format_err')

        routes = db.get_eta(origin, destination, region)
        if not routes:
            return _s(lang, 'no_routes', origin=origin, destination=destination)

        lines = [_s(lang, 'eta_header', origin=origin, destination=destination)]
        for r in routes:
            if r.get('eta_minutes') is None:
                lines.append(_s(lang, 'no_data'))
            else:
                lines.append(_s(lang, 'eta_line',
                                rid=r['route_id'],
                                min=r['eta_minutes'],
                                stops=r.get('stops', '?')))
        return '\n'.join(lines)

    return _s(lang, 'invalid')


def handle_sms(phone: str, message: str, region: str = 'Bioko') -> str:
    reply = _handle_query(message, phone, region)
    try:
        with db.get_db() as (conn, cur):
            cur.execute(
                "INSERT INTO sms_logs (phone, message_in, message_out) VALUES (?,?,?)",
                (phone, message, reply)
            )
    except Exception as e:
        logger.error(f"SMS log error: {e}")
    logger.info(f"SMS {phone}: '{message[:40]}' → '{reply[:60]}'")
    return reply


def handle_whatsapp(data: Dict, region: str = 'Bioko') -> Optional[str]:
    try:
        msg_data = data.get('data', {}).get('message', {})
        phone    = data.get('data', {}).get('key', {}).get('remoteJid', '').split('@')[0]
        text     = (msg_data.get('conversation') or
                    msg_data.get('extendedTextMessage', {}).get('text', '')).strip()
    except Exception as e:
        logger.error(f"WhatsApp parse error: {e}")
        return None

    if not text or not phone:
        return None

    reply = _handle_query(text, phone, region)

    if WHATSAPP_CONFIG.get('enabled'):
        try:
            url = (f"{WHATSAPP_CONFIG['base_url']}/message/sendText/"
                   f"{WHATSAPP_CONFIG['instance']}")
            requests.post(url,
                          json={'number': phone, 'text': reply},
                          headers={'apikey': WHATSAPP_CONFIG['api_key']},
                          timeout=10)
        except Exception as e:
            logger.error(f"WhatsApp send error: {e}")

    logger.info(f"WhatsApp {phone}: '{text[:40]}' → '{reply[:60]}'")
    return reply
