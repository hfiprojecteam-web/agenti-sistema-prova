#!/usr/bin/env python
"""
agent-permissions-guard.py - hook PreToolUse (strumenti MCP, Bash, PowerShell). Permessi per agente (17/10/2026).

Vale SOLO per le chiamate dei sub-agenti (l'evento contiene agent_id). La sessione principale, cioe' Cipriano o chi lavora
per sua richiesta, mantiene le richieste di permesso normali di Claude Code e non viene bloccata da questo hook.

Livelli per i sub-agenti:
- VIETATO a tutti: cancellare file/pagine/database, condividere file, strutture di Notion, Make (attivare, eliminare, creare,
  eseguire), Upwork (annunci, offerte, contratti, milestone: solo su richiesta di Cipriano, cioe' dalla sessione principale).
- EMAIL e INVITI con persone esterne: solo direttore, assistente, analista, e solo verso indirizzi della lista bianca
  `Sistema/architettura-agenti/lista-bianca-comunicazioni.md`. Un invio senza destinatari verificabili (es. una bozza gia' creata) e' negato.
- META ADS (creare, attivare, aggiornare, promuovere): solo il direttore e solo con un'approvazione valida di tipo `meta-ads`
  in `Sistema/architettura-agenti/approvazioni.md`.
- PUBBLICAZIONE SOCIAL (zernio_cli.py post, senza --dry-run): solo il direttore e solo con approvazione valida di tipo `pubblicazione-social`.
- UPWORK messaggi (send_message, get_messages): solo il direttore.
- NOTION scrittura di righe e campi: solo direttore e assistente.
`approvazioni.md` e la lista bianca sono protette (i sub-agenti non le scrivono, vedi protected-zone-guard.py): le aggiorna la sessione
principale dopo l'ok esplicito di Cipriano (chat, scheda Notion o workflow approvato).
Qualsiasi errore interno: lascia passare (exit 0).
"""
import json, re, sys, datetime, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
ARCH = ROOT / "Sistema" / "architettura-agenti"
WHITELIST = ARCH / "lista-bianca-comunicazioni.md"
APPROVALS = ARCH / "approvazioni.md"

MAIL_SERVER = "6e0ab785-6a16-45b0-ba85-a5811e116e8a"
WS = "workspace-mcp"
UPWORK = "f54dbe67-424a-4cfc-83c0-747feee45435"
GCAL = "f1a56278-64f0-4fc4-b5d3-61a9650b7fb8"
MAKE = "42d838ad-d94f-423b-a2e5-53003f0f0cd0"
META = "7d0a2c1d-e42a-4a9b-ba3c-9961761eda56"
NOTION = ("9cb489b1-22cd-4c3c-8f22-bebc1e011457", "plugin_design_notion")
DRIVE = "3a457b83-02e5-4b78-b5a1-9a725ccff9b2"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def log(decision, agent, tool, what):
    try:
        p = pathlib.Path(__file__).with_name("agent-permissions-guard.log")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {decision} {agent} {tool} {what[:160]!r}\n")
    except Exception:                                    # noqa: BLE001
        pass


def deny(reason, agent, tool, what=""):
    log("deny", agent, tool, what)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": "BLOCCATO - PERMESSI PER AGENTE: " + reason}}, ensure_ascii=False))
    sys.exit(0)


def whitelist_emails():
    try:
        return {e.lower() for e in EMAIL_RE.findall(WHITELIST.read_text(encoding="utf-8"))}
    except Exception:                                    # noqa: BLE001
        return set()


def has_approval(kind):
    """True se in approvazioni.md c'e' una riga di quel tipo con data di scadenza non passata."""
    try:
        today = datetime.date.today().isoformat()
        for line in APPROVALS.read_text(encoding="utf-8").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and cells[0].lower() == kind:
                m = re.search(r"\d{4}-\d{2}-\d{2}", cells[2])
                if m and m.group(0) >= today:
                    return True
    except Exception:                                    # noqa: BLE001
        pass
    return False


def short(tool):
    return tool.split("__")[-1]


def server(tool):
    parts = tool.split("__")
    return parts[1] if len(parts) > 2 else ""


def check_emails(who, tool, ti, allowed_agents, label):
    if who not in allowed_agents:
        deny(f"{label}: non consentito a '{who}' (solo {', '.join(sorted(allowed_agents))}).", who, tool)
    found = {e.lower() for e in EMAIL_RE.findall(json.dumps(ti, ensure_ascii=False))}
    if not found:
        deny(f"{label}: nessun destinatario verificabile nella chiamata (una bozza gia' creata o una risposta a un thread non si possono "
             "controllare). Chiedi a Cipriano.", who, tool)
    wl = whitelist_emails()
    bad = sorted(found - wl)
    if bad:
        deny(f"{label}: destinatari non in lista bianca ({', '.join(bad)[:120]}). Lista: Sistema/architettura-agenti/lista-bianca-comunicazioni.md; "
             "la aggiorna Cipriano.", who, tool, ",".join(bad))


def main():
    try:
        ev = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
        if not ev.get("agent_id"):
            return
        who = ev.get("agent_type") or "?"
        tool = ev.get("tool_name") or ""
        ti = ev.get("tool_input") or {}
        name, srv = short(tool), server(tool)

        # --- Bash / PowerShell: pubblicazione social via Zernio
        if tool in ("Bash", "PowerShell"):
            cmd = ti.get("command") or ""
            if re.search(r"zernio_cli\.py\s+(\S+\s+)*post\b", cmd) and "--dry-run" not in cmd:
                if who != "direttore":
                    deny("pubblicare sui social e' consentito solo al direttore.", who, tool, cmd)
                if not has_approval("pubblicazione-social"):
                    deny("manca un'approvazione valida di tipo 'pubblicazione-social' in approvazioni.md (scheda o workflow approvato da Cipriano).", who, tool, cmd)
            return

        if not tool.startswith("mcp__"):
            return

        # --- cancellare / condividere: vietato a tutti i sub-agenti
        if re.search(r"(delete_file|trash_file|share_file|bulk_delete|trash_message|trash_thread|drive_delete|notion-delete|"
                     r"delete-databases|delete-pages|delete_label|delete_draft)", name) or name in ("delete_slide",):
            deny(f"{name}: cancellare o condividere e' vietato ai sub-agenti.", who, tool)
        # --- struttura di Notion
        if srv in NOTION and re.search(r"(create-database|update-data-source|create-view|update-view|move-pages|update-enabled-skills|upload-skill|spawn-session|send-message-to-session)", name):
            deny(f"{name}: cambiare la struttura di Notion o aprire sessioni e' vietato ai sub-agenti.", who, tool)
        # --- Make: attivare, eliminare, creare, eseguire
        if srv == MAKE and re.search(r"(scenarios_(activate|deactivate|delete|create|update|run|replay|set-interface)|hooks_(create|delete|update)|"
                                     r"data-stores?_(delete|create|update)|data-store-records_(delete|create|update|replace)|"
                                     r"organizations_(create|delete|update)|teams_(create|delete)|keys_delete|connections_)", name):
            if not re.search(r"(connections_(get|list)|connection-)", name):
                deny(f"Make '{name}': attivare, eliminare o modificare scenari solo su richiesta di Cipriano.", who, tool)
        # --- Upwork
        if srv == UPWORK or "upwork" in tool:
            if name in ("send_message", "get_messages"):
                if who != "direttore":
                    deny("Upwork: chattare con i freelancer e' consentito solo al direttore.", who, tool)
                return
            if re.match(r"(post_job|manage_|invite_|end_contract|update_contract|update_account|confirm_|start_attachment|store_uploaded|set_tool_)", name):
                deny(f"Upwork '{name}': annunci, offerte, contratti e simili solo su richiesta di Cipriano.", who, tool)
        # --- Meta Ads: scritture
        if srv == META and re.match(r"ads_(create|activate|update|boost|delete|creative_(delete|update|upload)|catalog_(create|delete|update|product)|"
                                    r"pixel_(event|parameter)_(create|update|delete)|experiment_(abtest|lift)_create)", name):
            if who != "direttore":
                deny(f"Meta Ads '{name}': consentito solo al direttore.", who, tool)
            if not has_approval("meta-ads"):
                deny("Meta Ads: manca un'approvazione valida di tipo 'meta-ads' in approvazioni.md (ok di Cipriano da chat, scheda o workflow).", who, tool)
        # --- Email: invio
        if (srv == WS and name in ("gmail_send_email", "gmail_send_draft", "gmail_reply_to_email")) or \
           (srv == MAIL_SERVER and name in ("send_message", "reply", "forward")):
            if name == "gmail_send_draft":
                deny("gmail_send_draft: il destinatario di una bozza non e' verificabile; invia con gmail_send_email indicando i destinatari della lista bianca, o chiedi a Cipriano.", who, tool)
            check_emails(who, tool, ti, {"direttore", "assistente", "analista"}, "Invio email")
        # --- Calendario con invitati
        if (srv == GCAL and name in ("create_event", "update_event")) or (srv == WS and name == "create_calendar_event") or \
           (srv in NOTION and "calendar-create-events" in name) or (srv in NOTION and "calendar-update-events" in name):
            emails = {e.lower() for e in EMAIL_RE.findall(json.dumps(ti, ensure_ascii=False))}
            if emails:
                check_emails(who, tool, ti, {"direttore", "assistente"}, "Invito a persone esterne")
        # --- Notion: scrittura di righe e campi
        if srv in NOTION and re.match(r"notion-(update-page|create-pages|create-comment|update-comment|create-attachment|create-file-upload|duplicate-page)", name):
            if who not in ("direttore", "assistente"):
                deny(f"Notion '{name}': scrivere su Notion e' consentito solo a direttore e assistente.", who, tool)
        # --- Drive: caricare/creare solo analista, direttore, ops (non gli altri)
        if srv == DRIVE and name in ("create_file", "update_file", "copy_file") and who not in ("analista", "direttore", "ops-architect"):
            deny(f"Drive '{name}': consentito solo ad analista, direttore, ops-architect.", who, tool)
    except SystemExit:
        raise
    except Exception:                                    # noqa: BLE001
        return


if __name__ == "__main__":
    main()
