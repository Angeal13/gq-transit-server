# gq-transit-server

City Hall server for the Guinea Ecuatorial public transit network.
Handles all provinces — Bioko Island and all four Rio Muni provinces — plus cross-province routes.

**Part of the [GQ Transit platform](https://github.com/YOUR_USERNAME/gq-transit-infra)**

---

## What this does

Flask + MariaDB server deployed at:
- **Malabo** (Ayuntamiento) — serves Bioko Island
- **Bata** (Gobernación) — serves Rio Muni: Litoral, Centro Sur, Wele-Nzas, Kie-Ntem

Each deployment uses **separate databases per province** but shares the same codebase. Cross-province (Greyhound-style) buses write to a shared `interprovince` database.

## Database architecture

| Database | Region | Server |
|----------|--------|--------|
| `bus_tracking_gq_bioko` | Bioko Island | Malabo |
| `bus_tracking_gq_litoral` | Litoral (Bata metro) | Bata |
| `bus_tracking_gq_centrosur` | Centro Sur | Bata |
| `bus_tracking_gq_welenzas` | Wele-Nzas | Bata |
| `bus_tracking_gq_kientem` | Kie-Ntem | Bata |
| `bus_tracking_gq_interprovince` | Cross-province routes | Bata |

Each province database is **independent** — if the WAN link between Malabo and Bata goes down, both servers continue operating normally for their local regions.

## Quick install

```bash
git clone https://github.com/YOUR_USERNAME/gq-transit-server.git
cd gq-transit-server/src
sudo bash install_city_hall.sh
```

The installer prompts for: database password, API key, mechanic phone, backbone IP and interface.

## Configuration

Edit `/opt/bioko_server/.env`:

```env
DB_NAME=bus_tracking_gq_bioko     # change per region
API_KEY=your_shared_key            # must match all buses
MECHANIC_PHONE=+2406XXXXXXX
BACKBONE_IP=10.10.0.1              # 10.20.0.1 for Bata
```

## Deploying for a new province

1. Set `DB_NAME=bus_tracking_gq_<province>` in `.env`
2. Run the server — schema is created automatically on first start
3. Import province stops: `python admin_cli.py import-stops data/stops_<province>.csv`
4. Add routes: `python admin_cli.py add-route --region <Province> ...`

## Cross-province buses

Buses on inter-province routes set `REGION_NAME=IntreProvince` in their `.env`. The relay node they connect to writes their events to `bus_tracking_gq_interprovince` in addition to the local province database.

The ETA engine in `database.py` queries across province databases when calculating routes that span provincial boundaries.

## Web interfaces

| URL | Interface |
|-----|-----------|
| `/` or `/map/` | Live passenger map (all buses in region) |
| `/admin/` | Admin panel — routes, stops, buses |
| `/admin/fleet/` | Fleet health dashboard |

## API reference

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/routes?region=Bioko` | Key | Route + stop + coordinate data |
| POST | `/api/bus/register` | Key | Bus registration |
| POST | `/api/bus/stop` | Key | Stop arrival event |
| POST | `/api/bus/heartbeat` | Key | 30s position ping |
| GET | `/api/positions?region=Bioko` | — | Live bus positions |
| GET | `/api/eta?from=X&to=Y&region=Bioko` | — | ETA top-3 routes |
| POST | `/api/engine/reading` | Key | Engine sensor data |
| GET | `/api/engine/fleet` | — | Fleet health scores |
| GET | `/api/relay/status` | — | Relay node health |
| POST | `/api/sms` | — | Africa's Talking webhook |
| POST | `/api/whatsapp` | — | Evolution API webhook |

## Repository structure

```
src/
  app.py                 — Flask app factory, all blueprints registered
  config.py              — configuration from .env
  database.py            — MariaDB pool, schema, transit operations, ETA engine
  engine_database.py     — engine health schema + Z-score/EMA analysis
  engine_api.py          — engine health Flask blueprint
  relay_api.py           — relay node health Flask blueprint
  admin_routes.py        — admin web dashboard API
  admin_cli.py           — command-line route/stop management
  messaging.py           — SMS (Africa's Talking) + WhatsApp (Evolution API)
  templates/
    map.html             — live passenger map (Leaflet)
    admin.html           — admin panel
    fleet_health.html    — fleet health dashboard
  data/
    bioko_stops_sample.csv
  install_city_hall.sh
  requirements.txt
  .env.template
```

## License

MIT — owned by the project owner. See LICENSE.
