#!/usr/bin/env python3
# admin_cli.py  — City Hall Server
# Command-line tool to manage routes, stops, and coordinates.
# Run from the server directory: python admin_cli.py [command]

import sys
import json
import csv
import argparse
import logging

logging.basicConfig(level=logging.WARNING)  # Quiet during CLI use

import database as db
from database import _Pool, init_schema

_Pool.init()
init_schema()


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_list_routes(args):
    routes = db.get_routes(args.region)
    if not routes:
        print(f"No routes found for region: {args.region}")
        return
    for r in routes:
        stops = ' → '.join(s['name'] for s in r['stops'])
        print(f"[{r['id']}] {stops}  ({r['language']} | {r['client']})")


def cmd_list_stops(args):
    with db.get_db() as (conn, cur):
        cur.execute(
            "SELECT name, lat, lng FROM PARADAS WHERE region=%s ORDER BY name",
            (args.region,)
        )
        for row in cur.fetchall():
            print(f"{row['name']:40s}  {row['lat']:.6f}  {row['lng']:.6f}")


def cmd_add_stop(args):
    db.upsert_stop(args.name, float(args.lat), float(args.lng), args.region)
    print(f"Stop added/updated: {args.name}  ({args.lat}, {args.lng})")


def cmd_import_stops(args):
    """Import stops from CSV file with columns: name, lat, lng"""
    with open(args.file) as f:
        reader = csv.DictReader(f)
        count = 0
        for row in reader:
            db.upsert_stop(row['name'].strip(), float(row['lat']), float(row['lng']), args.region)
            count += 1
    print(f"Imported {count} stops.")


def cmd_add_route(args):
    """
    Add a route.
    Stops are comma-separated names that must already exist in PARADAS.
    Example:
      python admin_cli.py add-route --id R01 --type 1 \
        --client COCIGE --language es \
        --stops "Mercado Central,Estadio La Paz,Aeropuerto"
    """
    stop_names = [s.strip() for s in args.stops.split(',')]
    with db.get_db() as (conn, cur):
        # Insert route
        cur.execute("""
            INSERT INTO RUTAS (id, route_type, client, region, language, timezone)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                route_type=VALUES(route_type), client=VALUES(client),
                language=VALUES(language), timezone=VALUES(timezone)
        """, (args.id, int(args.type), args.client, args.region,
              args.language, args.timezone))

        # Link stops in order
        cur.execute("DELETE FROM RUTA_PARADAS WHERE route_id=%s", (args.id,))
        for order, name in enumerate(stop_names):
            cur.execute("SELECT id FROM PARADAS WHERE name=%s AND region=%s",
                        (name, args.region))
            row = cur.fetchone()
            if not row:
                print(f"WARNING: Stop '{name}' not found — add it first.")
                continue
            cur.execute("""
                INSERT INTO RUTA_PARADAS (route_id, stop_order, stop_id)
                VALUES (%s,%s,%s)
            """, (args.id, order, row['id']))

    print(f"Route {args.id} saved with {len(stop_names)} stops.")


def cmd_rebuild_eta(args):
    db.rebuild_travel_times(
        history_days=int(args.days),
        min_samples=int(args.min_samples)
    )
    print("ETA travel times rebuilt.")


def cmd_export_routes(args):
    routes = db.get_routes(args.region)
    print(json.dumps(routes, indent=2, ensure_ascii=False))


def cmd_bus_status(args):
    positions = db.get_live_positions(args.region, stale_seconds=300)
    if not positions:
        print("No active buses.")
        return
    print(f"{'Bus ID':30s}  {'Route':8s}  {'Stop':30s}  {'Updated':20s}")
    print('-' * 90)
    for p in positions:
        print(f"{p['bus_id']:30s}  {p.get('route_id','?'):8s}  "
              f"{p.get('stop_name','?'):30s}  {p.get('updated_at','?'):20s}")


# ─── Parser ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Bioko Transit Admin CLI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  list-routes         Show all routes for a region
  list-stops          Show all stops with coordinates
  add-stop            Add or update a stop
  import-stops        Bulk import stops from CSV
  add-route           Add or update a route
  rebuild-eta         Recompute travel time table
  export-routes       Export routes as JSON
  bus-status          Show live bus positions
        """
    )
    parser.add_argument('--region', default='Bioko', help='Region name (default: Bioko)')
    sub = parser.add_subparsers(dest='command')

    sub.add_parser('list-routes')
    sub.add_parser('list-stops')

    p = sub.add_parser('add-stop')
    p.add_argument('name');  p.add_argument('lat');  p.add_argument('lng')

    p = sub.add_parser('import-stops')
    p.add_argument('file', help='CSV with columns: name,lat,lng')

    p = sub.add_parser('add-route')
    p.add_argument('--id',       required=True)
    p.add_argument('--type',     default='1', help='1=circular 2=bidirectional')
    p.add_argument('--client',   required=True)
    p.add_argument('--language', default='es')
    p.add_argument('--timezone', default='Africa/Malabo')
    p.add_argument('--stops',    required=True, help='Comma-separated stop names')

    p = sub.add_parser('rebuild-eta')
    p.add_argument('--days',        default='45')
    p.add_argument('--min-samples', default='30')

    sub.add_parser('export-routes')
    sub.add_parser('bus-status')

    args = parser.parse_args()

    commands = {
        'list-routes':   cmd_list_routes,
        'list-stops':    cmd_list_stops,
        'add-stop':      cmd_add_stop,
        'import-stops':  cmd_import_stops,
        'add-route':     cmd_add_route,
        'rebuild-eta':   cmd_rebuild_eta,
        'export-routes': cmd_export_routes,
        'bus-status':    cmd_bus_status,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
