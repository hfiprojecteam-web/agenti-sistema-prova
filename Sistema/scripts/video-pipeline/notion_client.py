#!/usr/bin/env python
"""
notion_client.py — Notion API client for headless Mode 2 pipeline

Uses direct Notion API (v1) calls with NOTION_TOKEN from .env, enabling
batch.py to run autonomously (without an active Claude Code session).

Functions:
  - fetch_card(page_id) → dict with card properties (flat, python types)
  - query_cards(data_source_id, filter_dict, limit) → list of cards
  - update_page(page_id, properties) → PATCH page properties
  - list_pending_cards(project, days_ahead) → cards ready for production

Reads NOTION_TOKEN from Sistema/scripts/openmontage/.env

Usage as library:
  from notion_client import fetch_card, list_pending_cards
  card = fetch_card("3e263594-ed50-818c-bc88-e27ef1ccb527")
  print(card["properties"]["Hook"])

Usage as CLI:
  # Fetch a single card as JSON to disk (compatible with batch.py --card-files)
  python notion_client.py fetch --page-id <id> --out card.json

  # List pending cards ready for production
  python notion_client.py list-pending --project "Life Wisdom" --days-ahead 7

  # Test connection (verify token + DB access)
  python notion_client.py ping
"""
import argparse, os, json, sys, datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent
ENV_FILE = SCRIPT_DIR.parents[1] / "scripts" / "openmontage" / ".env"

# DB IDs — la REST API v1 richiede l'ID del DATABASE (55eca43b-...).
# L'ID 171f76f4-895d-4ae6-b83b-e18817722b7c che compare in README/Notion MCP e' il DATA SOURCE ("collection://"):
# stesso database, ID diverso, usabile SOLO dai tool MCP. Qui usa sempre quello REST.
CONTENUTI_DB_ID = "55eca43b-5b38-40f6-9e9b-2ab19274399c"       # 📱 Contenuti Self-Publishing Editoriale
PIPELINE_LIBRI_DB_ID = "bbf2a4f1-00bd-4ddf-b685-3a9dcd309343"  # 📚 Pipeline Produzione

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def load_token():
    if not ENV_FILE.exists():
        raise SystemExit(f"ERROR: env file not found: {ENV_FILE}")
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == "NOTION_TOKEN" and v.strip():
            return v.strip()
    raise SystemExit(
        "ERROR: NOTION_TOKEN not set in .env file.\n"
        "Follow setup: https://www.notion.so/my-integrations\n"
        "1. Create Internal Integration\n"
        "2. Copy Internal Integration Secret\n"
        "3. Add to .env: NOTION_TOKEN=<secret>\n"
        "4. Share DBs with the integration (Notion DB -> ... -> Connections)"
    )


def _headers():
    return {
        "Authorization": f"Bearer {load_token()}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _requests():
    """Lazy import to avoid hard dependency when just showing --help."""
    import requests
    return requests


def _flatten_property(prop):
    """Flatten a Notion property object into a Python-native value."""
    if not isinstance(prop, dict):
        return prop
    ptype = prop.get("type")
    if ptype in ("title", "rich_text"):
        return "".join(rt.get("plain_text", "") for rt in prop.get(ptype, []))
    if ptype == "url":
        return prop.get("url") or ""
    if ptype == "select":
        s = prop.get("select") or {}
        return s.get("name") or ""
    if ptype == "multi_select":
        return [x.get("name") for x in prop.get("multi_select", [])]
    if ptype == "date":
        d = prop.get("date") or {}
        return d.get("start") or ""
    if ptype == "checkbox":
        return prop.get("checkbox", False)
    if ptype == "number":
        return prop.get("number")
    if ptype == "relation":
        return [r.get("id") for r in prop.get("relation", [])]
    if ptype == "people":
        return [p.get("id") for p in prop.get("people", [])]
    if ptype == "status":
        s = prop.get("status") or {}
        return s.get("name") or ""
    return prop


def fetch_card(page_id):
    """Fetch a Notion page (card) and return flat dict compatible with brief_from_notion."""
    r = _requests()
    resp = r.get(f"{NOTION_API_BASE}/pages/{page_id}", headers=_headers(), timeout=30)
    if resp.status_code == 404:
        raise SystemExit(f"ERROR: page {page_id} not found or integration not shared with it")
    resp.raise_for_status()
    page = resp.json()
    props = {k: _flatten_property(v) for k, v in page.get("properties", {}).items()}
    # extract page title from any "title" property
    title = ""
    for k, v in page.get("properties", {}).items():
        if v.get("type") == "title":
            title = _flatten_property(v)
            break
    return {"id": page["id"], "title": title, "properties": props}


def query_cards(data_source_id=CONTENUTI_DB_ID, filter_dict=None, sorts=None, page_size=50):
    """Query a DB with optional filter + sorts. Returns list of flat card dicts."""
    r = _requests()
    payload = {"page_size": page_size}
    if filter_dict:
        payload["filter"] = filter_dict
    if sorts:
        payload["sorts"] = sorts
    all_results = []
    next_cursor = None
    while True:
        if next_cursor:
            payload["start_cursor"] = next_cursor
        resp = r.post(
            f"{NOTION_API_BASE}/databases/{data_source_id}/query",
            headers=_headers(), json=payload, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            props = {k: _flatten_property(v) for k, v in page.get("properties", {}).items()}
            title = ""
            for k, v in page.get("properties", {}).items():
                if v.get("type") == "title":
                    title = _flatten_property(v)
                    break
            all_results.append({"id": page["id"], "title": title, "properties": props})
        if not data.get("has_more"):
            break
        next_cursor = data.get("next_cursor")
    return all_results


def update_page(page_id, properties):
    """Update a page. `properties` is a dict {prop_name: {notion-api format}}."""
    r = _requests()
    resp = r.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=_headers(), json={"properties": properties}, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def set_page_fase(page_id, fase_name):
    """Convenience: set the 'Fase' select property to fase_name."""
    return update_page(page_id, {"Fase": {"select": {"name": fase_name}}})


def set_page_link(page_id, url):
    """Convenience: set the 'Link' url property (post-publish tracking)."""
    return update_page(page_id, {"Link": {"url": url}})


def list_pending_cards(project=None, days_ahead=7, fase="Script"):
    """Card in `fase` con Data pubblicazione <= oggi+days_ahead (le card SENZA data non compaiono).
    `project`: nome o ID pagina come in projects_map.yaml (filtra sulla relation della card)."""
    today = datetime.date.today()
    limit_date = (today + datetime.timedelta(days=days_ahead)).isoformat()
    filter_dict = {
        "and": [
            {"property": "Fase", "select": {"equals": fase}},
            {"property": "Data pubblicazione", "date": {"on_or_before": limit_date}},
        ]
    }
    cards = query_cards(filter_dict=filter_dict,
                        sorts=[{"property": "Data pubblicazione", "direction": "ascending"}])
    if project:
        import vp_common as vp
        cfg = vp.load_projects()
        entry = vp.find_project(cfg, project)
        if not entry:
            raise SystemExit(f"ERROR: progetto '{project}' non presente in projects_map.yaml")
        want = vp.norm_id(entry["id"])
        def linked(c):
            ids = []
            for prop in cfg["relation_properties"]:
                v = c["properties"].get(prop) or []
                ids += v if isinstance(v, list) else [v]
            return want in {vp.norm_id(i) for i in ids}
        cards = [c for c in cards if linked(c)]
    return cards


def cli_ping():
    r = _requests()
    resp = r.get(f"{NOTION_API_BASE}/users/me", headers=_headers(), timeout=15)
    if resp.status_code == 200:
        u = resp.json()
        print(f"OK — Notion API reachable. Integration: {u.get('name', '?')} "
              f"(bot id: {u.get('bot', {}).get('id', '?')})")
        # Test DB access
        try:
            resp = r.post(
                f"{NOTION_API_BASE}/databases/{CONTENUTI_DB_ID}/query",
                headers=_headers(), json={"page_size": 1}, timeout=15,
            )
            if resp.status_code == 200:
                cnt = len(resp.json().get("results", []))
                print(f"OK - DB Contenuti Self-Publishing Editoriale accessible ({cnt}+ cards)")
            elif resp.status_code == 404:
                print("[WARN] DB Contenuti NOT accessible - "
                      "share the DB with the integration in Notion (... -> Connections)")
            else:
                print(f"[WARN] DB Contenuti query returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[WARN] DB check failed: {e}")
    else:
        print(f"FAIL — {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ping = sub.add_parser("ping", help="Test NOTION_TOKEN and DB access")

    p_fetch = sub.add_parser("fetch", help="Fetch a single card to JSON")
    p_fetch.add_argument("--page-id", required=True)
    p_fetch.add_argument("--out", required=True)

    p_list = sub.add_parser("list-pending",
                            help="List cards ready for production (Fase=Script)")
    p_list.add_argument("--project", help="Nome o ID pagina del progetto (vedi projects_map.yaml)")
    p_list.add_argument("--days-ahead", type=int, default=7)
    p_list.add_argument("--fase", default="Script")
    p_list.add_argument("--out-dir", help="If provided, dump each card as JSON here")

    p_setf = sub.add_parser("set-fase", help="Update Fase on a page")
    p_setf.add_argument("--page-id", required=True)
    p_setf.add_argument("--fase", required=True,
                        choices=["Idea", "Script", "Asset generati", "Verificato", "Montaggio",
                                 "Programmato", "Pubblicato"])

    args = ap.parse_args()

    if args.cmd == "ping":
        cli_ping()
        return

    if args.cmd == "fetch":
        card = fetch_card(args.page_id)
        Path(args.out).write_text(json.dumps(card, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"OK — card saved to {args.out}")
        print(f"   title: {card['title']}")
        print(f"   formato: {card['properties'].get('Formato', '?')}")
        print(f"   fase: {card['properties'].get('Fase', '?')}")
        return

    if args.cmd == "list-pending":
        cards = list_pending_cards(project=args.project,
                                   days_ahead=args.days_ahead, fase=args.fase)
        print(f"OK — {len(cards)} card(s) pending")
        for c in cards:
            print(f"  {c['title']} "
                  f"[{c['properties'].get('Formato', '?')}] "
                  f"date={c['properties'].get('Data pubblicazione', '?')}")
        if args.out_dir:
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            for c in cards:
                slug = "".join(x if x.isalnum() else "_" for x in c["title"])[:60]
                fname = out / f"{slug}.json"
                fname.write_text(json.dumps(c, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            print(f"   {len(cards)} JSON files written to {out}")
        return

    if args.cmd == "set-fase":
        set_page_fase(args.page_id, args.fase)
        print(f"OK — page {args.page_id} Fase -> {args.fase}")
        return


if __name__ == "__main__":
    main()
