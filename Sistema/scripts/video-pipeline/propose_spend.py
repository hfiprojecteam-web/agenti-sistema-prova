#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""propose_spend.py - UNICO modo in cui il Direttore cloud crea una proposta di spesa (Stadio 1, punto 63). STAGING R3 31/10/2026, NON installato.

Crea UNA riga nel DB "Approvazioni" con l'integrazione BOT 'Agenti cloud' (modalita' proxy del cloud: la chiave la aggiunge il proxy;
nessuna chiave in questo file). L'agente NON usa mai i tool del connettore Notion per scrivere: quel connettore scrive COME Cipriano e
una sua riga passerebbe il controllo `last_edited_by` di `approve_spend`.

Cosa garantisce lo script (e RIFIUTA tutto il resto, senza chiamare la rete):
  - UNA SOLA proposta viva per scheda (regola unica, nel codice): prima di scrivere LEGGE le righe di "Approvazioni" e, se per la scheda esiste gia' una riga
    ATTUALE (non `Scaduta`/`Usata` e con scadenza non passata) in qualunque stato, non crea nulla (exit 7): una `Proposta`/`Approvata` viva = non si propone
    di nuovo; una `Rifiutata` viva = scheda bloccata finche' Cipriano non porta la riga a `Scaduta`. Per annullare una proposta sbagliata Cipriano imposta
    `Stato = Scaduta` (o `Rifiutata` per un NO che vale anche per le prossime richieste); un run successivo potra' riproporre. (--dry-run non legge nulla.)
  - la riga nasce con Approvo = NON spuntato e Stato = Proposta (nessuna opzione per cambiarli);
  - importo > 0, finito, al massimo 2 decimali, e importo + spesa gia' sostenuta dalla card <= 5 USD (tetto per contenuto);
  - scadenza = un giorno ISO tra oggi (ORA DI ROMA) e oggi + 2 giorni (default: domani);
  - modello obbligatorio (se c'e' un --brief deve coincidere con sourcing.model del brief: quello che l'hook confrontera');
  - il campo Impronta e' calcolato con `approve_spend.proposal_fingerprint` (stessa formula che usa l'hook);
  - PRIMA di scrivere controlla chi e' il token (GET /users/me): se non e' un bot, o e' la persona Cipriano, NON scrive;
  - DOPO la creazione controlla la risposta del POST e poi RILEGGE la pagina con una GET (/pages/<id>) e ricontrolla tutto sulla riga riletta:
    Approvo non spuntato, Stato Proposta, impronta, scheda, importo, modello, SCADENZA uguali al voluto e autore (created_by e
    last_edited_by) diverso da Cipriano. Se qualcosa non torna esce con codice 4 e un ALLARME (non tenta correzioni: la riga non va toccata).
    NB: in Notion created_by/last_edited_by hanno di norma solo {object, id} (nessun tipo): "autore non Cipriano" e' un confronto di id,
    il controllo sul tipo bot scatta solo se l'API lo restituisce (il tipo si legge con certezza solo da GET /users/me, prima di scrivere).
  - Tempo: ogni fase (identita'+creazione, rilettura) ha una scadenza globale (approve_spend._Deadline): niente attese infinite.

Dopo la creazione la riga NON si tocca piu' (ne' da questo script ne' da altri): ogni modifica di un bot alla riga cancella il valore della
spunta di Cipriano. REGOLA UNICA per correggere (R3): NON si crea una seconda proposta (questo script la rifiuta con exit 7 finche' la riga e' viva); la proposta
sbagliata la porta a `Scaduta` Cipriano, e un run successivo potra' riproporre. (La regola di approve_spend "una riga attuale piu' recente invalida la
precedente" resta solo come rete di sicurezza per righe create in altro modo, per esempio a mano nel DB: questo script non la usa mai.)

Codici di uscita: 0 creata e verificata | 7 esiste gia' una riga attuale per la scheda (nessuna scrittura) | 2 argomenti non validi (nessuna rete) | 3 esito incerto (rete) o riga creata ma NON rileggibile: NON rilanciare, controlla il DB |
4 identita' del token non ammessa o riga creata diversa dal voluto | 5 errore di Notion/rete prima della creazione.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import sys
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approve_spend as aps  # noqa: E402

EXIT_OK, EXIT_ARGS, EXIT_AMBIGUOUS, EXIT_VERIFY, EXIT_NOTION, EXIT_EXISTS = 0, 2, 3, 4, 5, 7
MAX_TEXT = 1800
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class Rifiutata(Exception):
    """Argomento non ammesso (exit 2, nessuna chiamata di rete)."""


def _text(name, value, required, limit=MAX_TEXT):
    v = (value or "").strip()
    if required and not v:
        raise Rifiutata(f"{name} obbligatorio")
    if len(v) > limit:
        raise Rifiutata(f"{name} troppo lungo ({len(v)} > {limit} caratteri)")
    if _CTRL.search(v):
        raise Rifiutata(f"{name} contiene caratteri di controllo")
    return v


def _money(name, value):
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise Rifiutata(f"{name} non numerico: {value!r}")
    if not d.is_finite():
        raise Rifiutata(f"{name} non finito: {value!r}")
    if d != d.quantize(Decimal("0.01")):
        raise Rifiutata(f"{name} con piu' di 2 decimali: {value!r}")
    return d


def validate(args, today=None, brief=None, spent_from_brief=None):
    """Controlla gli argomenti e ritorna i valori normalizzati. Solleva Rifiutata. Nessuna rete."""
    today = today or aps._rome_today()
    card = aps.uuid32(args.card)
    if not card:
        raise Rifiutata("--card deve essere esattamente un id Notion di 32 esadecimali (con o senza trattini)")
    amount = _money("--amount", args.amount)
    if amount <= 0:
        raise Rifiutata("--amount deve essere > 0")
    if args.spent_so_far is None and brief is None:
        raise Rifiutata("serve --brief (la spesa gia' fatta si legge dal registro) oppure --spent-so-far (anche 0, dichiarato): niente valore implicito")
    spent = _money("--spent-so-far", args.spent_so_far if args.spent_so_far is not None else "0")
    if spent < 0:
        raise Rifiutata("--spent-so-far non puo' essere negativo")
    if spent_from_brief is not None:
        sb = Decimal(repr(float(spent_from_brief)))
        if not sb.is_finite() or sb < 0:
            raise Rifiutata(f"spesa della card dal brief non valida: {spent_from_brief!r}")
        spent = max(spent, sb)          # vale il piu' alto tra quanto dichiarato e quanto dice il registro
    cap = Decimal(str(aps.MAX_CONTENT_USD))
    if amount > cap:
        raise Rifiutata(f"--amount {amount} oltre il tetto di {cap} USD per contenuto")
    if amount + spent > cap:
        raise Rifiutata(f"--amount {amount} + spesa gia' sostenuta {spent} supera il tetto di {cap} USD per contenuto: "
                        "non si propone (apri una decisione per Cipriano nel rapporto)")
    model = _text("--model", args.model, True, 200)
    if "\n" in model:
        raise Rifiutata("--model su una riga sola")
    if brief is not None:
        bm = (brief.get("sourcing") or {}).get("model") if isinstance(brief.get("sourcing"), dict) else None
        if not isinstance(bm, str) or bm.strip() != model:
            raise Rifiutata(f"--model {model!r} diverso da sourcing.model del brief ({bm!r}): l'hook confronta con il brief")
        if aps.uuid32(brief.get("notion_card_id")) != card:
            raise Rifiutata("--card diverso da notion_card_id del brief")
    if args.expiry:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.expiry):
            raise Rifiutata("--expiry deve essere AAAA-MM-GG")
        try:
            expiry = datetime.date.fromisoformat(args.expiry)
        except ValueError:
            raise Rifiutata("--expiry non e' una data valida")
    else:
        expiry = today + datetime.timedelta(days=1)
    if expiry < today or expiry > today + datetime.timedelta(days=aps.MAX_VALID_DAYS):
        raise Rifiutata(f"--expiry {expiry} fuori dalla finestra [{today}, {today + datetime.timedelta(days=aps.MAX_VALID_DAYS)}]")
    return {
        "card": card, "amount": float(amount), "model": model, "expiry": expiry.isoformat(),
        "cosa": _text("--cosa", args.cosa, True), "motivo": _text("--motivo", args.motivo, True),
        "alternativa": _text("--alternativa", args.alternativa, False),
    }


def build_properties(v):
    """Proprieta' della NUOVA riga. Approvo e Stato sono FISSI: non vengono dagli argomenti."""
    def rich(t):
        return [{"type": "text", "text": {"content": t}}] if t else []
    f = aps.FIELDS
    return {
        f["scheda"]: {"title": [{"type": "text", "text": {"content": v["card"]}}]},
        "Cosa": {"rich_text": rich(v["cosa"])},
        f["modello"]: {"rich_text": rich(v["model"])},
        f["importo"]: {"number": v["amount"]},
        f["scadenza"]: {"date": {"start": v["expiry"]}},
        "Motivo": {"rich_text": rich(v["motivo"])},
        "Alternativa": {"rich_text": rich(v["alternativa"])},
        f["stato"]: {"select": {"name": "Proposta"}},
        f["approvo"]: {"checkbox": False},
        f["impronta"]: {"rich_text": rich(aps.proposal_fingerprint(v["card"], v["model"], v["amount"], v["expiry"]))},
    }


def verify_created(resp, v, fingerprint):
    """Elenco dei problemi di una riga (risposta del POST o riga riletta con la GET); vuoto = tutto come voluto.
    Controlla: Approvo non spuntato, Stato, impronta, scheda, importo, modello, SCADENZA, autori diversi da Cipriano."""
    problems = []
    if not isinstance(resp, dict) or not isinstance(resp.get("id"), str) or not isinstance(resp.get("properties"), dict):
        return ["risposta di Notion senza id/properties"]
    props, f = resp["properties"], aps.FIELDS
    if aps._checkbox(props, f["approvo"]) is not False:
        problems.append("Approvo NON risulta non spuntato")
    if aps._select(props, f["stato"]) != "Proposta":
        problems.append("Stato non e' 'Proposta'")
    if aps._text(props, f["impronta"]).strip() != fingerprint:
        problems.append("Impronta diversa da quella calcolata")
    if aps.uuid32(aps._text(props, f["scheda"])) != v["card"]:
        problems.append("Scheda diversa")
    if aps._number(props, f["importo"]) != v["amount"]:
        problems.append("Importo diverso")
    if aps._text(props, f["modello"]).strip() != v["model"]:
        problems.append("Modello diverso")
    day = aps._day(props, f["scadenza"])
    if day is None or day.isoformat() != v["expiry"]:
        problems.append("Scadenza diversa o illeggibile")
    approver = aps.uuid32(aps.APPROVER_PERSON_ID)
    for key in ("created_by", "last_edited_by"):
        uid, utype = aps._user(resp, key)
        if not uid:
            problems.append(f"{key} assente")
        elif uid == approver:
            problems.append(f"{key} e' la persona Cipriano: la riga e' stata scritta COME Cipriano (connettore?)")
        elif utype is not None and utype != "bot":
            problems.append(f"{key} di tipo {utype!r}, non bot")
    return problems


def existing_problem(pages, card, today):
    """Motivo (str) se per la scheda esiste gia' una riga ATTUALE in 'Approvazioni' (nessuna nuova proposta); altrimenti None.
    Attuale = Stato diverso da Scaduta/Usata e Scadenza non passata (Scadenza illeggibile o assente = attuale: nel dubbio blocca)."""
    if not isinstance(pages, list):
        return "righe di 'Approvazioni' non leggibili"
    f = aps.FIELDS
    for p in pages:
        props = p.get("properties") if isinstance(p, dict) else None
        if not isinstance(props, dict) or aps.uuid32(aps._text(props, f["scheda"])) != card:
            continue
        stato, scad = aps._select(props, f["stato"]), aps._day(props, f["scadenza"])
        if stato in ("Scaduta", "Usata") or (scad is not None and scad < today):
            continue
        ref = p.get("id") if isinstance(p.get("id"), str) else "?"
        if stato == "Rifiutata":
            return (f"scheda bloccata: c'e' una riga 'Rifiutata' ancora attuale (id={ref}); la riproposta e' possibile solo dopo che Cipriano "
                    "porta il suo Stato a 'Scaduta'")
        return (f"esiste gia' una riga '{stato}' non scaduta per questa scheda (id={ref}, Scadenza {scad.isoformat() if scad else 'illeggibile'}): "
                "non se ne crea un'altra; se e' sbagliata la porta a 'Scaduta' Cipriano")
    return None


def whoami_problem(me):
    """None se il token e' un bot diverso dalla persona Cipriano; altrimenti il motivo."""
    if not isinstance(me, dict):
        return "risposta di /users/me non valida"
    if me.get("type") != "bot":
        return f"il token non e' un bot (type={me.get('type')!r}): non si scrive"
    if aps.uuid32(me.get("id")) == aps.uuid32(aps.APPROVER_PERSON_ID):
        return "il token coincide con la persona Cipriano: non si scrive"
    if not aps.uuid32(me.get("id")):
        return "id del token illeggibile"
    return None


REREAD_S = 10.0          # tempo massimo della rilettura dopo la creazione


def run(argv=None, *, transport=None, backend_kw=None, today=None, out=sys.stdout, err=sys.stderr, brief_loader=None, spent_loader=None):
    ap = argparse.ArgumentParser(description="Crea UNA proposta di spesa (Stato=Proposta, Approvo=NO) nel DB 'Approvazioni' con il bot.", allow_abbrev=False)
    ap.add_argument("--card", required=True, help="id della scheda Notion (32 esadecimali)")
    ap.add_argument("--amount", required=True, help="importo massimo proposto in USD (max 2 decimali, <= 5 con la spesa gia' fatta)")
    ap.add_argument("--model", required=True, help="modello (uguale a sourcing.model del brief)")
    ap.add_argument("--cosa", required=True, help="la quotazione in una riga (formato, modello, durata)")
    ap.add_argument("--motivo", required=True, help="perche' questa spesa")
    ap.add_argument("--alternativa", default="", help="alternativa piu' economica, se c'e'")
    ap.add_argument("--expiry", default=None, help="scadenza AAAA-MM-GG (default: domani; al massimo oggi + 2)")
    ap.add_argument("--spent-so-far", default=None, help="USD gia' spesi per questa card (0 se prima generazione); con --brief si calcola da solo")
    ap.add_argument("--brief", default=None, help="percorso del brief YAML: ne ricava la spesa gia' fatta e confronta card/modello")
    ap.add_argument("--dry-run", action="store_true", help="valida e stampa la riga che creerebbe; NESSUNA chiamata di rete")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return EXIT_ARGS if e.code not in (0, None) else 0

    brief, spent_from_brief = None, None
    try:
        if args.brief:
            brief = (brief_loader or _load_brief)(args.brief)
            spent_from_brief = (spent_loader or _brief_spent)(brief)
        v = validate(args, today=today, brief=brief, spent_from_brief=spent_from_brief)
    except Rifiutata as e:
        print(f"RIFIUTATA: {e}", file=err)
        return EXIT_ARGS
    except Exception as e:  # noqa: BLE001
        print(f"RIFIUTATA: errore nel leggere il brief o la spesa ({type(e).__name__}): nel dubbio non si propone", file=err)
        return EXIT_ARGS

    fingerprint = aps.proposal_fingerprint(v["card"], v["model"], v["amount"], v["expiry"])
    props = build_properties(v)
    if args.dry_run:
        print(json.dumps({"dry_run": True, "properties": props, "impronta": fingerprint}, ensure_ascii=False), file=out)
        return EXIT_OK

    dl = aps._Deadline(aps._deadline_s())
    try:
        cl, backend, tok = aps.make_backend(transport=transport, backend_kw=backend_kw, deadline=dl)
    except aps.ApprovalUnavailable as e:
        print(f"NON CREATA: {e}", file=err)
        return EXIT_NOTION
    try:
        with backend.time_budget(aps._deadline_s()):
            me = backend._request("GET", "/users/me")
            why = whoami_problem(me)
            if why:
                print(f"NON CREATA (identita' del token): {why}", file=err)
                return EXIT_VERIFY
            why = existing_problem(backend._raw_query(None), v["card"], today or aps._rome_today())     # lettura (query), nessuna scrittura
            if why:
                print(f"NON CREATA (riga gia' presente): {why}", file=err)
                return EXIT_EXISTS
            parent = ({"type": "data_source_id", "data_source_id": backend.container} if backend.mode == "data_source"
                      else {"database_id": backend.container})
            resp = backend._request("POST", "/pages", {"parent": parent, "properties": props}, idempotent=False)
    except cl._Ambiguous as e:
        print(f"ESITO INCERTO ({cl.redact(e, tok)}): la riga potrebbe essere stata creata. NON rilanciare: guarda il DB 'Approvazioni'.", file=err)
        return EXIT_AMBIGUOUS
    except Exception as e:  # noqa: BLE001
        print(f"NON CREATA: {cl.redact(str(e), tok)[:200]} ({type(e).__name__})", file=err)
        return EXIT_NOTION

    problems = verify_created(resp, v, fingerprint)
    if problems:
        print("ALLARME: la riga creata NON e' come voluta: " + "; ".join(problems) +
              f". id={resp.get('id') if isinstance(resp, dict) else '?'}. Non toccarla: avvisa Cipriano.", file=err)
        return EXIT_VERIFY

    # RILETTURA: una GET della pagina appena creata e gli stessi controlli sul valore che Notion ha davvero memorizzato.
    page_id = resp["id"]
    if not aps.uuid32(page_id):
        print(f"ESITO INCERTO: la riga e' stata creata ma il suo id non e' leggibile ({page_id!r}). NON rilanciare: guarda il DB 'Approvazioni'.", file=err)
        return EXIT_AMBIGUOUS
    dl.reset(REREAD_S)
    try:
        with backend.time_budget(REREAD_S):
            reread = backend._request("GET", "/pages/" + aps.uuid32(page_id))
    except Exception as e:  # noqa: BLE001
        print(f"ESITO INCERTO: la riga e' stata creata (id={page_id}) ma la rilettura e' fallita ({cl.redact(str(e), tok)[:150]}). "
              "NON rilanciare: guarda il DB 'Approvazioni'.", file=err)
        return EXIT_AMBIGUOUS
    if not isinstance(reread, dict) or aps.uuid32(reread.get("id")) != aps.uuid32(page_id):
        print(f"ALLARME: la rilettura ha dato un'altra pagina o una risposta senza id (id={page_id}). Non toccarla: avvisa Cipriano.", file=err)
        return EXIT_VERIFY
    problems = verify_created(reread, v, fingerprint)
    if problems:
        print("ALLARME: la riga RILETTA da Notion NON e' come voluta: " + "; ".join(problems) +
              f". id={page_id}. Non toccarla: avvisa Cipriano.", file=err)
        return EXIT_VERIFY
    print(json.dumps({"creata": True, "riletta": True, "id": resp["id"], "url": resp.get("url"), "scheda": v["card"], "importo_max_usd": v["amount"],
                      "modello": v["model"], "scadenza": v["expiry"], "impronta": fingerprint, "stato": "Proposta", "approvo": False,
                      "creata_da": (resp.get("created_by") or {}).get("id")}, ensure_ascii=False), file=out)
    return EXIT_OK


def _load_brief(path):
    import yaml  # PyYAML e' gia' richiesto dal pipeline e dall'hook
    with open(path, encoding="utf-8") as fh:
        b = yaml.safe_load(fh) or {}
    if not isinstance(b, dict):
        raise ValueError("brief non e' un dizionario")
    return b


def _brief_spent(brief):
    """Spesa gia' sostenuta dalla card, con la STESSA funzione che usa il cancello nel codice (vp_common.card_spent_usd)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import vp_common
    return float(vp_common.card_spent_usd(brief))


if __name__ == "__main__":
    sys.exit(run())
