#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""approve_spend.py - fonte dell'ok di spesa NEL CLOUD (Stadio 1, punto 63). VERSIONE R2 31/10/2026 (staging, NON installata).

Sul PC la riga `spesa-video` sta in `approvazioni.md` e la scrive la sessione principale dopo l'ok di Cipriano in chat.
Nel cloud non c'e' sessione principale e il repository clonato e' fermo al clone: la fonte dell'ok e' un database
"Approvazioni" su Notion (opzione A, decisa da Cipriano il 21/10/2026). L'agente (Direttore cloud) PROPONE con
`propose_spend.py` (integrazione bot 'Agenti cloud', MAI il connettore Notion): riga con Stato = Proposta, Approvo NON
spuntato, campo Impronta calcolato dalla proposta. Cipriano spunta "Approvo" (anche da telefono).

Il controllo qui e' DETERMINISTICO e FAIL-CLOSED: in caso di dubbio NEGATO. Una riga vale solo se TUTTO questo e' vero:
  1. l'ULTIMA modifica e' di Cipriano: l'id di `last_edited_by` e' uguale all'id LETTERALE della persona Cipriano nel codice (confronto
     dell'id COMPLETO a 32 esadecimali; nessuna variabile d'ambiente lo sovrascrive);
  2. la riga e' stata CREATA da qualcun altro: l'id di `created_by` NON e' quello di Cipriano. ATTENZIONE, onesta': nelle pagine Notion
     `created_by` e `last_edited_by` sono in genere solo {object, id} (nessun `type`): il controllo sul tipo ('person') scatta SOLO se l'API
     lo restituisce; in pratica il controllo e' "non creata da Cipriano" e "ultima modifica di Cipriano", cioe' un confronto di id;
  3. `Approvo` spuntato; `Stato` esattamente `Proposta` o `Approvata`;
  4. `Scadenza` = un giorno ISO esatto (AAAA-MM-GG) tra oggi (ORA DI ROMA) e oggi + 2 giorni;
  5. `Importo max USD` finito, > 0, <= 5 (tetto per contenuto) e >= importo richiesto (anch'esso <= 5);
  6. `Modello` della riga UGUALE (confronto esatto) al modello richiesto (quello del brief);
  7. `Impronta` della riga UGUALE all'impronta ricalcolata da scheda+modello+importo+scadenza: se qualcuno ha cambiato
     qualcosa dopo la proposta, o la riga non l'ha creata `propose_spend.py`, NEGATO;
  8. nessun'altra riga ATTUALE per la stessa scheda la contraddice: una `Rifiutata` ancora attuale = conflitto = NEGATO;
     una riga PIU' RECENTE (anche solo in attesa) rende non valida la precedente ("una nuova proposta invalida la vecchia").

Tempo: la lettura del DB ha un TEMPO MASSIMO TOTALE (default 20 s, tra 1 e 30) controllato prima di OGNI richiesta e di OGNI attesa
(pagine, nuovi tentativi, attese su HTTP 429): vedi `_Deadline`. Oltre = fonte non disponibile = NEGATO.

LIMITE NOTO (non risolvibile con la sola API di Notion): l'API non dice CHI ha spuntato una casella ne' QUANDO: dice solo chi ha
modificato per ultimo la pagina. Vedi LEGGIMI.md/RAPPORTO.md (G-2) per le opzioni di rinforzo, che richiedono una decisione di Cipriano.

Questo modulo NON applica i tetti giornalieri/mensili: restano nel registro costi (`spend_gate.py`/`cost_ledger.py`).

Solo libreria standard. Il trasporto HTTP verso Notion e la redazione dei token sono riusati da `cost_ledger.py`
(stesso pacchetto cloud) via import pigro: cosi' il modulo e la sua prova con righe finte non dipendono dalla rete.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys
import time
import zoneinfo
from collections import namedtuple

# --------------------------------------------------------------------------- configurazione FISSA (nessuna variabile d'ambiente)

# Id della PERSONA Cipriano in Notion (tipo person). Un bot/integrazione ha un id diverso: non puo' spacciarsi per questa persona.
# COSTANTE: non si legge da nessuna variabile d'ambiente (31/10/2026: provato che APPROVALS_APPROVER_PERSON_ID permetteva ALLOW con l'id del bot).
APPROVER_PERSON_ID = "2e1d872b-594c-81bb-b37a-0002b82327de"

# Quanti giorni al massimo puo' valere un'approvazione (come il PC: entro oggi + 2 giorni). COSTANTE.
MAX_VALID_DAYS = 2

# Tetto per contenuto (stesso valore di `budget_cap_per_content_usd`): una riga o una richiesta oltre questo importo e' NEGATA qui,
# prima ancora del cancello nel codice (`approval_gate`), che resta comunque. COSTANTE.
MAX_CONTENT_USD = 5.0

# Nomi dei campi nel database "Approvazioni" (vedi SETUP-DB-APPROVAZIONI-PROVA.md).
FIELDS = {
    "scheda": "Scheda",            # title: id della scheda (32 esadecimali)
    "importo": "Importo max USD",  # number
    "scadenza": "Scadenza",        # date (solo giorno)
    "stato": "Stato",              # select
    "approvo": "Approvo",          # checkbox
    "modello": "Modello",          # rich_text (OBBLIGATORIO: entra nell'impronta)
    "impronta": "Impronta",        # rich_text (OBBLIGATORIO: la scrive propose_spend)
}

STATI_OK = ("Proposta", "Approvata")                 # unici stati che possono valere (lista esatta)
STATI_NEGATI = ("Rifiutata", "Scaduta", "Usata")     # bloccano comunque, qualunque cosa dica "Approvo"
STATO_CONFLITTO = "Rifiutata"                        # una riga cosi', ancora attuale, rende NEGATA la scheda


class ApprovalUnavailable(Exception):
    """Fonte dell'ok non raggiungibile o illeggibile. Chi chiama la tratta come NEGATO (fail-closed)."""


Decision = namedtuple("Decision", "ok reason card_id max_usd ref detail")


def _negato(card_id, max_usd, reason, ref=None, detail=None):
    return Decision(False, reason, card_id, max_usd, ref, detail or {})


# --------------------------------------------------------------------------- identificativi e impronta

_HEX32 = re.compile(r"[0-9a-f]{32}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def uuid32(s):
    """Id Notion STRETTO: esattamente 32 esadecimali, con o senza i trattini nelle posizioni canoniche. Altrimenti ''.
    (Niente URL, niente caratteri in piu': il vecchio norm_id teneva i soli esadecimali di una stringa qualunque.)"""
    if not isinstance(s, str):
        return ""
    t = s.strip().lower()
    if _HEX32.fullmatch(t):
        return t
    if _UUID.fullmatch(t):
        return t.replace("-", "")
    return ""


def proposal_fingerprint(card32, model, importo, scadenza_iso):
    """Impronta della proposta: 16 esadecimali (sha256 troncato) di scheda|modello|importo (2 decimali)|scadenza.
    La calcola `propose_spend.py` alla creazione; `approve_spend` la ricalcola dai valori ATTUALI della riga.
    NON e' una firma segreta (il codice e' nel repository): protegge dalle modifiche involontarie/posteriori, non da chi sa scrivere la riga a mano."""
    canon = "|".join(["approvazioni-v1", card32, str(model).strip(), "%.2f" % float(importo), scadenza_iso])
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _rome_today(now=None):
    """Data di oggi a ROMA, anche se l'orologio e' in UTC (come `_rome_today` dell'hook). Senza tzdata solleva: check() nega."""
    return (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(zoneinfo.ZoneInfo("Europe/Rome")).date()


# --------------------------------------------------------------------------- lettura proprieta' Notion

def _prop(props, name):
    v = props.get(name)
    return v if isinstance(v, dict) else None


def _text(props, name):
    """Testo semplice da un campo title / rich_text / url. '' se assente o vuoto."""
    v = _prop(props, name)
    if not v:
        return ""
    t = v.get("type")
    if t in ("title", "rich_text"):
        parts = v.get(t) or []
        return "".join(p.get("plain_text", "") for p in parts if isinstance(p, dict))
    if t == "url":
        return v.get("url") or ""
    return ""


def _checkbox(props, name):
    """True/False dalla casella; None se il campo manca o non e' una checkbox (fail-closed)."""
    v = _prop(props, name)
    if v and v.get("type") == "checkbox":
        return bool(v.get("checkbox"))
    return None


def _number(props, name):
    """Numero FINITO dal campo; None se assente, non numerico, booleano, inf o NaN."""
    v = _prop(props, name)
    if v and v.get("type") == "number":
        n = v.get("number")
        if isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n):
            return float(n)
    return None


def _select(props, name):
    v = _prop(props, name)
    if v and v.get("type") == "select":
        return (v.get("select") or {}).get("name")
    return None


def _day(props, name):
    """Giorno ISO ESATTO (AAAA-MM-GG) dal campo date, senza ora e senza intervallo. None se in qualunque modo diverso."""
    v = _prop(props, name)
    if not (v and v.get("type") == "date"):
        return None
    d = v.get("date")
    if not isinstance(d, dict) or d.get("end") is not None:
        return None
    s = d.get("start")
    if not isinstance(s, str) or not _DAY.fullmatch(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def _created(page):
    s = page.get("created_time")
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def _user(page, key):
    """(id a 32 esadecimali o '', tipo o None) dell'utente `key` ('created_by' / 'last_edited_by') della pagina.
    Nelle pagine Notion questi campi sono in genere solo {object, id}: il tipo e' quasi sempre None e la decisione si basa sull'id."""
    u = page.get(key)
    if not isinstance(u, dict):
        return "", None
    return uuid32(u.get("id")), u.get("type")


# --------------------------------------------------------------------------- il controllo

def evaluate(pages, card_id, max_usd, *, today=None, model=None):
    """Decide su righe gia' recuperate (list di pagine Notion grezze). Nessuna rete: usata sia dalla prova sia dal percorso vero.
    Fail-closed su tutto. `today` (data di Roma) si inietta solo nelle prove."""
    today = today or _rome_today()
    target = uuid32(card_id)
    if not target:
        return _negato(card_id, max_usd, "id della scheda non valido (servono esattamente 32 esadecimali)")
    if isinstance(max_usd, bool):
        return _negato(card_id, max_usd, "importo richiesto non numerico")
    try:
        req = float(max_usd)
    except (TypeError, ValueError):
        return _negato(card_id, max_usd, "importo richiesto non numerico")
    if not math.isfinite(req) or not (req > 0):
        return _negato(card_id, max_usd, "importo richiesto non positivo o non finito")
    if req > MAX_CONTENT_USD + 1e-9:
        return _negato(card_id, max_usd, f"importo richiesto {req:.2f} oltre il tetto di {MAX_CONTENT_USD:.0f} USD per contenuto")
    if not isinstance(model, str) or not model.strip():
        return _negato(card_id, max_usd, "modello richiesto assente: serve il modello del brief per confrontarlo con la proposta")
    if not isinstance(pages, list):
        return _negato(card_id, max_usd, "righe del DB 'Approvazioni' non leggibili")

    # Righe la cui Scheda corrisponde (confronto STRETTO) alla card richiesta.
    cands = []
    for p in pages:
        props = p.get("properties") if isinstance(p, dict) else None
        if isinstance(props, dict) and uuid32(_text(props, FIELDS["scheda"])) == target:
            stato = _select(props, FIELDS["stato"])
            scad = _day(props, FIELDS["scadenza"])
            # Una riga e' ATTUALE se non e' chiusa (Scaduta/Usata) e la sua scadenza non e' passata. Scadenza illeggibile = attuale (fail-closed).
            attuale = stato not in ("Scaduta", "Usata") and (scad is None or scad >= today)
            cands.append({"page": p, "props": props, "stato": stato, "scad": scad, "created": _created(p), "attuale": attuale})
    if not cands:
        return _negato(card_id, max_usd, "nessuna proposta in 'Approvazioni' per questa scheda")

    # Conflitto: una riga Rifiutata ancora attuale per la stessa scheda. Vince il NEGATO, qualunque altra riga dica.
    for c in cands:
        if c["stato"] == STATO_CONFLITTO and c["attuale"]:
            return _negato(card_id, max_usd,
                           "righe in conflitto: c'e' una riga 'Rifiutata' ancora attuale per questa scheda "
                           "(se vuoi riproporre, imposta Stato = Scaduta sulla riga rifiutata)", ref=c["page"].get("id"))

    valid, reasons = [], {}
    for c in cands:
        ok, reason, detail = _row_ok(c, target=target, req=req, today=today, model=model)
        reasons[id(c)] = (reason, detail)
        if ok:
            valid.append(c)

    # "Una nuova proposta invalida la vecchia": una riga valida e' scavalcata se esiste un'altra riga ATTUALE piu' recente
    # (o con created_time uguale/illeggibile: nel dubbio nessuna delle due vale).
    left = []
    for c in valid:
        newer = [o for o in cands if o is not c and o["attuale"]
                 and (o["created"] is None or c["created"] is None or o["created"] >= c["created"])]
        if newer:
            reasons[id(c)] = (f"esiste una riga piu' recente (o contemporanea) per la stessa scheda, {newer[0]['stato']!r}: "
                              "l'ok precedente non vale piu' (una nuova proposta invalida la vecchia)", {})
        else:
            left.append(c)

    # Dopo lo scavalcamento resta al massimo UNA riga (a parita' di created_time nessuna delle due): due righe valide contemporanee = NEGATO.
    if len(left) != 1:
        # Motivo utile: quello della riga attuale piu' recente, altrimenti dell'ultima esaminata.
        pool = [c for c in cands if c["attuale"]] or cands
        pool = sorted(pool, key=lambda c: (c["created"] or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)))
        reason, detail = reasons[id(pool[-1])]
        return _negato(card_id, max_usd, reason, ref=pool[-1]["page"].get("id"), detail=detail)
    c = left[0]
    return Decision(True, "approvata da Cipriano", card_id, req, c["page"].get("id"), reasons[id(c)][1])


def _row_ok(c, target, req, today, model):
    """True + dettaglio se la singola riga autorizza la spesa. Altrimenti False + motivo."""
    page, props = c["page"], c["props"]
    detail = {}
    approver = uuid32(APPROVER_PERSON_ID)

    # 1. L'ANTI-FORGERY: l'ultima modifica deve essere di Cipriano (confronto dell'id COMPLETO). Valore normalizzato: vuoto/assente = NEGATO.
    #    Il controllo sul tipo 'person' vale solo se l'API lo da' (di norma non lo da').
    editor, editor_type = _user(page, "last_edited_by")
    detail["last_edited_by"] = editor or None
    detail["last_edited_by_type"] = editor_type
    if not editor:
        return False, "ultima modifica della riga assente o con id illeggibile", detail
    if editor != approver:
        return False, "ultima modifica della riga NON di Cipriano (id diverso: riga forgiata o toccata da un bot o da un altro utente)", detail
    if editor_type is not None and editor_type != "person":
        return False, f"ultima modifica di tipo '{editor_type}', non 'person'", detail

    # 2. SEPARAZIONE DEI RUOLI: la proposta non puo' averla creata Cipriano (id). Se l'API da' anche il tipo e il tipo e' 'person', NEGATO;
    #    senza tipo (il caso normale) conta solo l'id: una persona diversa da Cipriano non sarebbe riconosciuta come tale.
    creator, creator_type = _user(page, "created_by")
    detail["created_by"] = creator or None
    if not creator:
        return False, "creatore della riga assente o con id illeggibile", detail
    if creator == approver or creator_type == "person":
        return False, "la riga risulta creata da Cipriano o da una persona: la proposta deve crearla l'agente (bot) con propose_spend.py", detail
    if c["created"] is None:
        return False, "created_time della riga assente o illeggibile", detail

    # 3. "Approvo" spuntato.
    appr = _checkbox(props, FIELDS["approvo"])
    detail["approvo"] = appr
    if appr is not True:
        return False, "casella 'Approvo' non spuntata", detail

    # 4. Stato: lista ESATTA.
    stato = c["stato"]
    detail["stato"] = stato
    if stato in STATI_NEGATI:
        return False, f"Stato = '{stato}'", detail
    if stato not in STATI_OK:
        return False, f"Stato assente o non riconosciuto ({stato!r}): ammessi {', '.join(STATI_OK)}", detail

    # 5. Data della riga leggibile e dentro la finestra [oggi, oggi + MAX_VALID_DAYS] (oggi = data di Roma).
    scad = c["scad"]
    detail["scadenza"] = scad.isoformat() if scad else None
    if scad is None:
        return False, "Scadenza assente o non e' un giorno ISO esatto (AAAA-MM-GG, senza ora ne' intervallo)", detail
    if scad < today:
        return False, f"approvazione scaduta il {scad.isoformat()}", detail
    if scad > today + datetime.timedelta(days=MAX_VALID_DAYS):
        return False, f"Scadenza oltre oggi + {MAX_VALID_DAYS} giorni ({scad.isoformat()})", detail

    # 6. Importo: finito, > 0, <= 5 USD per contenuto, >= importo richiesto.
    cap = _number(props, FIELDS["importo"])
    detail["importo_riga"] = cap
    if cap is None or not (cap > 0):
        return False, "Importo max USD assente, non finito o non positivo", detail
    if cap > MAX_CONTENT_USD + 1e-9:
        return False, f"tetto della riga {cap:.2f} oltre i {MAX_CONTENT_USD:.0f} USD per contenuto", detail
    if req > cap + 1e-9:
        return False, f"importo richiesto {req:.2f} oltre il tetto della riga {cap:.2f}", detail

    # 7. Modello: CONFRONTO ESATTO (dopo strip), obbligatorio nella riga.
    riga_modello = _text(props, FIELDS["modello"]).strip()
    detail["modello_riga"] = riga_modello or None
    if not riga_modello:
        return False, "Modello assente nella riga", detail
    if riga_modello != model.strip():
        return False, f"modello richiesto '{model.strip()}' diverso dalla riga '{riga_modello}'", detail

    # 8. Impronta: ricalcolata dai valori ATTUALI della riga.
    riga_impronta = _text(props, FIELDS["impronta"]).strip()
    detail["impronta_riga"] = riga_impronta or None
    if not riga_impronta:
        return False, "Impronta assente: la riga non l'ha creata propose_spend.py", detail
    atteso = proposal_fingerprint(target, riga_modello, cap, scad.isoformat())
    if riga_impronta != atteso:
        return False, "Impronta non combacia: scheda, modello, importo o scadenza sono cambiati dopo la proposta", detail

    return True, "ok", detail


# --------------------------------------------------------------------------- percorso vero (Notion)

def _deadline_s():
    """Tempo massimo TOTALE della lettura (secondi): 20 di base, regolabile da APPROVALS_DEADLINE_S ma sempre tra 1 e 30.
    E' un tempo per l'INTERA lettura (tutte le pagine, tutti i tentativi, tutte le attese), non per la singola richiesta: lo fa rispettare
    `_Deadline`, controllata prima di ogni richiesta e di ogni attesa. Il resto dell'hook (le letture del registro costi) NON e' dentro
    questo tempo: lo limita l'hook con un suo tempo massimo totale (vedi spend-guard.py, TOTAL_DEADLINE_S, e LEGGIMI)."""
    try:
        v = float(os.environ.get("APPROVALS_DEADLINE_S", "20"))
    except ValueError:
        v = 20.0
    if not math.isfinite(v):
        v = 20.0
    return min(max(v, 1.0), 30.0)


_CLOCK = time.monotonic          # le prove lo sostituiscono con un orologio finto; in produzione e' l'orologio monotono


class _Deadline:
    """Scadenza GLOBALE di un'operazione che fa piu' richieste. `left()` = secondi che restano; `check()` solleva ApprovalUnavailable se finiti."""

    def __init__(self, seconds):
        self.seconds = float(seconds)
        self.end = _CLOCK() + self.seconds

    def left(self):
        return self.end - _CLOCK()

    def reset(self, seconds):
        """Nuova fase con un suo tempo (per esempio la rilettura dopo una creazione): sostituisce la scadenza, non la allunga di nascosto."""
        self.seconds = float(seconds)
        self.end = _CLOCK() + self.seconds

    def check(self, what):
        if self.left() <= 0:
            raise ApprovalUnavailable(f"tempo massimo totale ({self.seconds:.0f} s) esaurito durante {what}")


def _bounded_transport(inner, dl):
    """Trasporto che non parte se la scadenza globale e' finita e riduce il timeout al tempo che resta (non lo allunga mai)."""
    def transport(method, url, headers, body, timeout):
        dl.check("una richiesta a Notion")
        left = dl.left()
        return inner(method, url, headers, body, min(float(timeout), max(left, 0.2)))
    return transport


def _bounded_sleep(inner, dl):
    """Attesa che non supera la scadenza globale: se l'attesa non ci sta, ApprovalUnavailable (niente attese su 429 fino a 30 s fuori budget)."""
    def sleep(seconds):
        if seconds >= dl.left():
            raise ApprovalUnavailable(f"tempo massimo totale ({dl.seconds:.0f} s) esaurito: l'attesa di {seconds:.1f} s non ci sta")
        inner(seconds)
    return sleep


def load_cost_ledger():
    """Importa `cost_ledger` (pacchetto co-locato nel cloud; sul PC il pipeline installato; poi lo staging). ApprovalUnavailable se manca."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        here,
        os.path.join(here, "..", "..", "..", "..", "scripts", "video-pipeline"),
        os.path.join(here, "..", "..", "2026-10-20-registro-costi-notion", "consegna-v85"),
    ]
    for cand in candidates:                    # IN FONDO a sys.path, nell'ordine: un file della cartella con il nome di un modulo standard non deve coprire quello vero
        cand = os.path.abspath(cand)
        if os.path.isdir(cand) and cand not in sys.path:
            sys.path.append(cand)
    try:
        import cost_ledger as cl  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise ApprovalUnavailable(f"cost_ledger non importabile (serve nel pacchetto cloud): {type(e).__name__}")
    return cl


def make_backend(data_source_id=None, database_id=None, token=None, transport=None, backend_kw=None, deadline=None):
    """(cost_ledger, NotionBackend, token) per il DB 'Approvazioni'. Senza token (NOTION_TOKEN assente, nessun .env) = modalita' PROXY del cloud:
    la chiave la aggiunge il proxy dell'ambiente ed e' quella dell'integrazione bot 'Agenti cloud'. NON cade mai sul data source del registro costi."""
    cl = load_cost_ledger()
    ds = data_source_id or os.environ.get("APPROVALS_NOTION_DATA_SOURCE_ID")
    db = database_id or os.environ.get("APPROVALS_NOTION_DATABASE_ID")
    if not ds and not db:
        raise ApprovalUnavailable("manca APPROVALS_NOTION_DATA_SOURCE_ID (o ..._DATABASE_ID)")
    kw = dict(backend_kw or {})
    if transport is not None:
        kw["transport"] = transport
    if token is not None:
        kw["token"] = token
    elif not os.environ.get("NOTION_TOKEN"):
        try:
            kw.update(cl._cli_notion_kwargs())
        except Exception:  # noqa: BLE001
            pass
    if deadline is not None:           # scadenza globale: trasporto e attese passano dal controllo (anche quelli iniettati dalle prove)
        kw["transport"] = _bounded_transport(kw.get("transport") or cl._urllib_transport, deadline)
        kw["sleep"] = _bounded_sleep(kw.get("sleep") or time.sleep, deadline)
    tok = kw.get("token") or os.environ.get("NOTION_TOKEN")
    try:
        return cl, cl.NotionBackend(data_source_id=ds, database_id=db, **kw), tok
    except Exception as e:  # noqa: BLE001
        raise ApprovalUnavailable(f"backend Notion non creato: {type(e).__name__}")


def _fetch_pages(data_source_id=None, database_id=None, token=None, transport=None, backend_kw=None):
    """Scarica TUTTE le righe del DB 'Approvazioni' (il DB e' piccolo: si filtra qui). Fail-closed: ogni errore -> ApprovalUnavailable."""
    secs = _deadline_s()
    dl = _Deadline(secs)
    cl, backend, tok = make_backend(data_source_id, database_id, token, transport, backend_kw, deadline=dl)
    try:
        with backend.time_budget(secs):          # secondo livello (orologio del registro costi); il primo e' `dl`
            return backend._raw_query(None)
    except cl.LedgerError as e:
        raise ApprovalUnavailable(cl.redact(str(e), tok))
    except ApprovalUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise ApprovalUnavailable(f"query 'Approvazioni' fallita: {type(e).__name__}")


def check(card_id, max_usd, *, model=None, pages=None, today=None,
          data_source_id=None, database_id=None, token=None, transport=None, backend_kw=None):
    """Punto d'ingresso. `pages` iniettate (prova) oppure scaricate da Notion. NON solleva mai: qualsiasi problema
    (rete, lettura, bug, tzdata) -> NEGATO con motivo (fail-closed)."""
    try:
        if pages is None:
            try:
                pages = _fetch_pages(data_source_id=data_source_id, database_id=database_id, token=token,
                                     transport=transport, backend_kw=backend_kw)
            except ApprovalUnavailable as e:
                return _negato(card_id, max_usd, f"fonte dell'ok non disponibile: {e}")
        return evaluate(pages, card_id, max_usd, today=today, model=model)
    except Exception as e:  # noqa: BLE001
        return _negato(card_id, max_usd, f"errore nel controllo dell'approvazione ({type(e).__name__}): nel dubbio NEGATO")


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="Controlla l'ok di spesa nel DB 'Approvazioni' (Stadio 1 cloud).")
    ap.add_argument("--card", required=True, help="id della scheda Notion (32 esadecimali, con o senza trattini)")
    ap.add_argument("--max-usd", required=True, type=float, help="importo massimo richiesto per il run")
    ap.add_argument("--model", required=True, help="modello richiesto (quello del brief, confronto esatto)")
    ap.add_argument("--json", action="store_true", help="stampa la decisione in JSON")
    args = ap.parse_args(argv)

    d = check(args.card, args.max_usd, model=args.model)
    if args.json:
        print(json.dumps({"ok": d.ok, "reason": d.reason, "card_id": d.card_id,
                          "max_usd": d.max_usd, "ref": d.ref, "detail": d.detail}, ensure_ascii=False))
    else:
        print(("OK: " if d.ok else "NEGATO: ") + d.reason)
    return 0 if d.ok else 1


if __name__ == "__main__":
    sys.exit(main())
