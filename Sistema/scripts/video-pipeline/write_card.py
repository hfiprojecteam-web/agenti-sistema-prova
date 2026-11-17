#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""write_card.py - UNICO canale con cui il Direttore cloud scrive sulle schede dei contenuti (Stadio 1, punto 1c). STAGING R5 01/11/2026, NON installato.

Perche' esiste: la scelta (c) di R3 ("il Direttore cloud non scrive nulla sulle schede") e' stata respinta da Cipriano: gli agenti devono portare avanti la catena
da soli, a lui restano le decisioni. Quindi il canale di scrittura si costruisce SICURO: bot 'Agenti cloud' (proxy del cloud: la chiave la aggiunge il proxy,
nessuna chiave qui), ELENCO CHIUSO di campi e transizioni, fail-closed, confronto con lo stato atteso, rilettura, registro locale. Stesso stile di propose_spend.py.
L'agente NON usa mai i tool di scrittura del connettore Notion (scrivono COME Cipriano).

ELENCO CHIUSO (tutto il resto e' rifiutato prima di qualunque scrittura; vedi anche `ALLOWED_PROPERTY_NAMES`):
  1) Fase           solo Script -> Asset generati  e  Asset generati -> Verificato  (mai oltre, mai indietro, mai Programmato/Pubblicato/Montaggio);
                    serve Owner = IA e il campo `Video finale` non vuoto; lo stato attuale deve essere QUELLO DICHIARATO (--fase-da), altrimenti nessuna scrittura (exit 7).
  2) Owner          solo IA -> Cipriano (restituzione). Mai Cipriano -> IA: sarebbe l'INNESCO di un nuovo lavoro e lo da' solo Cipriano.
  3) Note           solo AGGIUNTA di una riga che inizia per "[agente] <data>"; il testo che c'era resta identico (verificato dopo la scrittura).
  4) Video finale   allegato di un file .mp4 (<= 20 MiB, intestazione mp4, nome final.mp4 / final_cta.mp4 dentro una cartella NN-Contenuti), solo se il campo e' VUOTO.
  5) Task Pending   INSERIMENTO di una riga (campione / caricamento manuale / azione di livello C / controllo di accesso), mai modifica, mai "Completato", con dedup.
  Una scrittura = una sola PATCH (Fase+Owner+Note insieme, se richiesti insieme) o un solo upload o un solo inserimento.

MAI (nessuna opzione per farlo): campi che entrano nell'impronta di approvazione (scheda, modello, importo, scadenza), la casella o lo stato di approvazione, il DB "Approvazioni"
(salvo `propose_spend.py`), `Usata`, Hook/Descrizione/Hashtag/Angle/Clip generation spec/Scene master URL (alimentano il brief: non sono dello Stadio 1 cloud), Live, Link,
Data pubblicazione, Etichetta IA, Libro collegato, qualunque altra proprieta', qualunque altra pagina o database. La pagina deve stare nel DB di PROVA dei contenuti
(`read_card.CARD_DATA_SOURCE_PROVA`, nel codice: nessuna variabile d'ambiente lo cambia) e non essere archiviata.

Garanzie del flusso (provate in test_write_card.py con un Notion FINTO):
  - PRIMA di scrivere: identita' del token (GET /users/me: deve essere un bot diverso dalla persona Cipriano); lettura della scheda; controllo di perimetro, di Owner,
    di Fase, dei tipi dei campi; seconda lettura subito prima della PATCH (confronto: ultima modifica e valori osservati non devono essere cambiati). NB: Notion non ha un
    vero "scrivi solo se non e' cambiato" (nessun ETag): tra la seconda lettura e la PATCH resta una finestra di pochi decimi di secondo, dichiarata.
  - DOPO: la risposta e la RILETTURA devono mostrare i valori voluti, NESSUNA altra proprieta' cambiata e `last_edited_by` = il bot. Se non torna: exit 4, ALLARME, nessun tentativo di correzione.
  - Errore di rete durante la scrittura: exit 3 (la scrittura potrebbe essere arrivata): NON rilanciare, guarda la scheda.
  - Nessun token in uscita ne' nel registro; registro locale a righe JSON in un percorso FISSO (`<cartella temporanea>/write_card-log/write_card.log.jsonl`, mai deciso da variabili d'ambiente
    ne' da opzioni: R5/D-2) e una riga di rapporto in italiano sulla standard output. Il registro si apre solo se NON esiste (creato con il suo marcatore nella prima riga) o se e' un file
    regolare, non collegamento, del suo utente, con quel marcatore: un file di altri non riceve mai una riga. REGOLA: uno script del canale non legge percorsi di uscita da variabili d'ambiente.
  - R5/D-3 (gare): Notion non ha un "scrivi solo se non e' cambiato" (nessun ETag) e `last_edited_time` ha la granularita' del minuto (da verificare dal vivo). Quindi: (1) nessuna scrittura se la
    scheda e' stata modificata da qualcuno che NON e' il bot negli ultimi 180 s (`QUIET_S`: copre le modifiche a raffica di Cipriano; se l'ora e' arrotondata al minuto, la quiete vera e' fra 120 e 180 s); (2) il confronto fra la prima e la seconda lettura guarda l'ora,
    l'autore e TUTTE le proprieta' (non solo l'ora: nel Notion vero due modifiche nello stesso minuto non cambiano l'ora); (3) fra la seconda lettura e la PATCH non c'e' nessuna attesa "di ritmo"
    (`min_interval_s = 0`) e il registro conserva la durata massima della finestra (`finestra_ms_max`); (4) dopo la scrittura, se cambia un ALTRO campo (per esempio Owner) -> exit 4 ALLARME.
    MISURA VERA (test_write_card.py, sezione GARE): una modifica che arriva DOPO la seconda lettura e tocca lo STESSO campo che scriviamo (Fase, Note, Video finale) NON e' rilevabile ne'
    impedibile: la scrittura la sovrascrive in silenzio (3 casi su 4 provati; il quarto, un ALTRO campo, e' rilevato). Il registro conserva i valori PRECEDENTI dei campi scritti
    (letti alla seconda lettura) per ripristinarli a mano. E' un residuo dichiarato, non una garanzia.

Codici di uscita: 0 scritto e verificato | 2 argomenti non validi (nessuna rete) | 3 esito incerto: NON rilanciare | 4 ALLARME (identita' non ammessa o scheda diversa dal voluto dopo la scrittura) |
5 Notion/rete prima della scrittura | 6 scheda fuori perimetro o campo mancante/di tipo inatteso/non verificabile | 7 stato non atteso (Owner, Fase, video, campo non vuoto, riga gia' presente) o scheda cambiata nel frattempo.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import pathlib
import tempfile
import time
import unicodedata
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approve_spend as aps  # noqa: E402
import propose_spend as ps  # noqa: E402
import read_card as rc  # noqa: E402

EXIT_OK, EXIT_ARGS, EXIT_AMBIGUOUS, EXIT_VERIFY, EXIT_NOTION, EXIT_OUTSIDE, EXIT_STATE = 0, 2, 3, 4, 5, 6, 7

CARD_DATA_SOURCE = rc.CARD_DATA_SOURCE_PROVA                              # DB di PROVA dei contenuti: UNA sola definizione (read_card.py)
TASK_PENDING_DATA_SOURCE = "7212f1de-f174-4398-8e81-7273e30f9461"        # Task Pending (collection di mappa-trigger-database §3.5); vedi LEGGIMI per la prova su un DB di prova

F_FASE, F_OWNER, F_NOTE, F_VIDEO = "Fase", "Owner", "Note", "Video finale"
ALLOWED_PROPERTY_NAMES = frozenset({F_FASE, F_OWNER, F_NOTE, F_VIDEO})     # le uniche proprieta' di una SCHEDA che questo script puo' scrivere

OWNER_IA, OWNER_CIPRIANO = "IA", "Cipriano"
FASE_TRANSITIONS = {("Script", "Asset generati"), ("Asset generati", "Verificato")}      # catena di produzione fino a Verificato, un passo avanti per volta
FASI_VIDEO_OK = ("Script", "Asset generati")                                              # fasi in cui si puo' allegare il video finale

NOTE_MARK = "[agente]"
MAX_NOTE = 1500
VIDEO_MAX_BYTES = 20 * 1024 * 1024                                        # upload singolo di Notion: 20 MiB
VIDEO_NAME = re.compile(r"final(?:_cta)?\.mp4", re.I)
CONTENUTI_DIR = re.compile(r"^\d\d-Contenuti$")
UPLOAD_S = 90.0
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# R5/M-1: nota e testo del task su UNA riga, senza controlli C0/C1, separatori di riga Unicode, caratteri di direzione/invisibili
_STRICT_BAD = re.compile("[\x00-\x1f\x7f-\x9f\u00ad\u061c\u180e\u2028\u2029\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")      # controlli C0/C1, separatori di riga e di paragrafo, caratteri invisibili e di direzione
# R6/M5: oltre all'elenco sopra, niente caratteri "ignorabili" (Default_Ignorable_Code_Point: tag U+E0000-E0FFF, U+034F, U+3164, U+115F/1160, U+FFA0, selettori di variante FE00-FE0F e E0100-E01EF,
# U+180B-180F, U+FFF0-FFF8, U+2065, U+1BCA0-1BCA3, U+1D173-1D17A), niente categorie Cc/Cf/Cs/Co/Cn (controllo, formato, surrogati, uso privato, non assegnati), Zl/Zp, spazi diversi da quello normale (Zs: U+00A0, U+2000-200A,
# U+202F, U+205F, U+3000...) e U+2800 (braille vuoto). Un accento scomposto (e + U+0300, categoria Mn) NON e' invisibile e passa.
_IGNORABLE_RANGES = ((0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F), (0x2065, 0x2065), (0x2800, 0x2800), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0),
                     (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))
_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _invisible(text):
    """True se il testo contiene un carattere invisibile / ignorabile / non assegnato / spazio esotico (vedi sopra)."""
    for ch in text:
        o = ord(ch)
        cat = unicodedata.category(ch)
        if cat in _BAD_CATEGORIES or (cat == "Zs" and ch != " ") or any(lo <= o <= hi for lo, hi in _IGNORABLE_RANGES):
            return True
    return False


# build_package.py legge la PRIMA corrispondenza di questo schema nella Nota per trovare il clip da verificare: una nota dell'agente non puo' contenerlo
CLIP_RE = re.compile(r"(?:Self Publishing|Cavemen AI Project|Performance Creative Studio)[^\n\"]*?\.mp4|_grezzi[/\\]", re.I)
QUIET_S = 180
LOG_MARK = '{"write_card_log": 1}'
LOG_STATE = {"ok": True}
WORKSPACE = str(pathlib.Path(__file__).resolve().parents[3])        # radice del repository (Sistema/scripts/video-pipeline/write_card.py -> 3 cartelle sopra)

TIPI_TASK = ("campione", "caricamento-manuale", "azione-livello-c", "controllo-accesso")
CATEGORIE_TASK = ("KDP", "Business", "Sistema", "Personale", "Reviews", "Collaboratori")     # mappa-trigger-database §3.5
TP = {"stato": "Status", "categoria": "Categoria", "scadenza": "Scadenza"}                    # nomi da verificare sul DB (il codice controlla lo schema e si ferma con exit 6)
TP_STATO_INIZIALE = "Da fare"

SKIP_TYPES = {"formula", "rollup", "last_edited_time", "last_edited_by", "created_time", "created_by", "unique_id", "button", "verification"}


class Rifiutata(Exception):
    """Argomento non ammesso (exit 2, nessuna chiamata di rete)."""


class Perimetro(Exception):
    """Scheda fuori perimetro o campo mancante/di tipo inatteso (exit 6)."""


class StatoDiverso(Exception):
    """Stato diverso da quello atteso (exit 7)."""


# --------------------------------------------------------------------------- validazione degli argomenti (nessuna rete)

def _text(name, value, limit, strict=False):
    v = (value or "").strip()
    if not v:
        raise Rifiutata(f"{name} obbligatorio")
    if len(v) > limit:
        raise Rifiutata(f"{name} troppo lungo ({len(v)} > {limit} caratteri)")
    if (_STRICT_BAD if strict else _CTRL).search(v):
        raise Rifiutata(f"{name} contiene caratteri di controllo, a capo, separatori di riga o caratteri di direzione/invisibili (una sola riga di testo semplice)")
    if strict and _invisible(v):                                                   # R6/M5
        raise Rifiutata(f"{name} contiene caratteri invisibili, ignorabili o spazi insoliti (una sola riga di testo semplice, senza caratteri nascosti)")
    if strict and CLIP_RE.search(v):
        raise Rifiutata(f"{name} contiene un percorso di clip (.mp4 / _grezzi): una nota dell'agente non indirizza la verifica del clip")
    return v


def rome_now(now=None):
    return (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(aps.zoneinfo.ZoneInfo("Europe/Rome"))


def validate_edit(args):
    """Modifica di proprieta' della scheda (Fase/Owner/Note). Ritorna dict normalizzato. Solleva Rifiutata."""
    out = {}
    if (args.fase_da is None) != (args.fase_a is None):
        raise Rifiutata("--fase-da e --fase-a vanno insieme (si dichiara lo stato atteso e quello voluto)")
    if args.fase_a is not None:
        pair = (args.fase_da.strip(), args.fase_a.strip())
        if pair not in FASE_TRANSITIONS:
            raise Rifiutata(f"transizione di Fase {pair[0]!r} -> {pair[1]!r} non ammessa: ammesse solo " +
                            ", ".join(f"{a} -> {b}" for a, b in sorted(FASE_TRANSITIONS)) + " (mai oltre Verificato, mai indietro)")
        out["fase"] = pair
    if args.owner_a is not None:
        if args.owner_a.strip() != OWNER_CIPRIANO:
            raise Rifiutata(f"--owner-a ammette solo {OWNER_CIPRIANO!r} (restituzione). Rimettere Owner = IA e' l'innesco di un nuovo lavoro: lo da' solo Cipriano")
        out["owner_a"] = OWNER_CIPRIANO
    if args.nota is not None:
        out["nota"] = _text("--nota", args.nota, MAX_NOTE, strict=True)
    if not out:
        raise Rifiutata("nessuna modifica richiesta (--fase-da/--fase-a, --owner-a, --nota)")
    return out


def validate_video(path, workspace=None):
    """Controlli sul file (nessuna rete). Ritorna (percorso reale, nome, byte). Solleva Rifiutata.
    R5/M-3: il file deve stare DENTRO la radice del repository/lavoro (`WORKSPACE`; `workspace` solo per le prove) e sotto una cartella NN-Contenuti di quella radice."""
    if not path or _CTRL.search(path):
        raise Rifiutata("--video-finale: percorso non valido")
    if os.path.islink(path):
        raise Rifiutata("--video-finale: un collegamento simbolico non e' ammesso")
    real = os.path.realpath(path)
    ws = os.path.realpath(workspace or WORKSPACE)
    try:
        inside = os.path.normcase(os.path.commonpath([real, ws])) == os.path.normcase(ws)
    except ValueError:
        inside = False
    if not inside:
        raise Rifiutata("--video-finale: il file deve stare dentro la cartella di lavoro del repository (non fuori)")
    if not os.path.isfile(real):
        raise Rifiutata("--video-finale: il file non esiste o non e' un file regolare")
    name = os.path.basename(real)
    if not VIDEO_NAME.fullmatch(name):
        raise Rifiutata(f"--video-finale: il nome deve essere final.mp4 o final_cta.mp4 (trovato {name!r})")
    parts = os.path.relpath(real, ws).replace("\\", "/").split("/")
    if not any(CONTENUTI_DIR.match(p) for p in parts[:-1]):
        raise Rifiutata("--video-finale: il file deve stare in una cartella NN-Contenuti del progetto, dentro la cartella di lavoro (pack o _grezzi)")
    size = os.path.getsize(real)
    if size <= 0 or size > VIDEO_MAX_BYTES:
        raise Rifiutata(f"--video-finale: dimensione {size} byte fuori da 1..{VIDEO_MAX_BYTES} (oltre 20 MiB serve il caricamento a parti: non previsto, lo carica Cipriano)")
    with open(real, "rb") as fh:
        head = fh.read(12)
    if len(head) < 12 or head[4:8] != b"ftyp":
        raise Rifiutata("--video-finale: il file non ha l'intestazione di un mp4 (ftyp)")
    return real, name, size


def validate_task(args, today):
    if args.tipo not in TIPI_TASK:
        raise Rifiutata(f"--tipo deve essere uno tra {', '.join(TIPI_TASK)}")
    if args.categoria not in CATEGORIE_TASK:
        raise Rifiutata(f"--categoria deve essere una tra {', '.join(CATEGORIE_TASK)}")
    testo = _text("--testo", args.testo, 150, strict=True)
    if not args.scadenza or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.scadenza):
        raise Rifiutata("--scadenza obbligatoria, AAAA-MM-GG")
    try:
        d = datetime.date.fromisoformat(args.scadenza)
    except ValueError:
        raise Rifiutata("--scadenza non e' una data valida")
    if d < today or d > today + datetime.timedelta(days=30):
        raise Rifiutata("--scadenza fuori dalla finestra [oggi, oggi + 30 giorni]")
    return {"titolo": f"{NOTE_MARK} {args.tipo}: {testo}", "categoria": args.categoria, "scadenza": d.isoformat(), "tipo": args.tipo}


# --------------------------------------------------------------------------- lettura e controlli di una scheda

def _prop(props, name, kinds):
    pr = props.get(name)
    if not isinstance(pr, dict) or pr.get("type") not in kinds:
        raise Perimetro(f"campo {name!r} mancante o di tipo inatteso (atteso {'/'.join(kinds)}): la scheda non e' nel formato previsto")
    return pr


def _sel(pr):
    t = pr["type"]
    return ((pr.get(t) or {}).get("name")) or ""


def _plain(items):
    return "".join(x.get("plain_text", "") for x in items if isinstance(x, dict))


def _files(pr):
    return [f for f in (pr.get("files") or []) if isinstance(f, dict)]


def quiet_problem(page, me_id, now_utc):
    """R5/D-3: motivo (str) se la scheda e' stata modificata da qualcuno che NON e' il bot meno di QUIET_S secondi fa (o se l'ora non e' leggibile); altrimenti None."""
    uid, _t = aps._user(page, "last_edited_by")
    if uid and uid == me_id:
        return None
    ts = aps._created({"created_time": page.get("last_edited_time")})
    if ts is None:
        return "last_edited_time illeggibile: nel dubbio non si scrive"
    if (now_utc - ts).total_seconds() < QUIET_S:
        return f"la scheda e' stata modificata da qualcuno che non e' il bot meno di {QUIET_S} secondi fa (potrebbe essere Cipriano al lavoro): nessuna scrittura, un run successivo potra' riprovare"
    return None


def _snap(page):
    """Istantanea per il confronto fra due letture: ora e autore dell'ultima modifica, archiviazione e TUTTE le proprieta' (non solo quelle che si scrivono)."""
    uid, _t = aps._user(page, "last_edited_by")
    return (page.get("last_edited_time"), uid, bool(page.get("archived") or page.get("in_trash")), json.dumps(_stable(page["properties"], set()), sort_keys=True, default=str))


def prev_values(props):
    """Valori PRECEDENTI dei campi scrivibili: vanno nel registro e nell'allarme, per ripristinarli a mano se una gara li ha persi."""
    out = {}
    for n, fn in ((F_FASE, lambda pr: _sel(pr)), (F_OWNER, lambda pr: _sel(pr)), (F_VIDEO, lambda pr: [f.get("name") for f in _files(pr)]),
                  (F_NOTE, lambda pr: _plain(pr.get("rich_text") or [])[:2000])):
        try:
            out[n] = fn(props[n])
        except (KeyError, TypeError, AttributeError):
            out[n] = None
    return out


def check_card(page, want_id, *, need_fase=None, need_video=None, video_empty=False, me_id=None, now_utc=None):
    """Controlla perimetro e stato. Ritorna lo stato letto. Solleva Perimetro / StatoDiverso."""
    if not isinstance(page, dict) or aps.uuid32(page.get("id")) != want_id or not isinstance(page.get("properties"), dict):
        raise Perimetro("la risposta di Notion non e' la scheda richiesta")
    parent = page.get("parent") if isinstance(page.get("parent"), dict) else {}
    ds = aps.uuid32(parent.get("data_source_id"))
    if not ds:
        raise Perimetro("scheda senza parent.data_source_id: perimetro non verificabile")
    if ds != aps.uuid32(CARD_DATA_SOURCE):
        raise Perimetro("la pagina NON e' nel DB di prova dei contenuti (nessuna scrittura: potrebbe essere una riga di 'Approvazioni' o di un altro DB)")
    if page.get("archived") or page.get("in_trash"):
        raise Perimetro("scheda archiviata o nel cestino")
    props = page["properties"]
    owner = _sel(_prop(props, F_OWNER, ("select", "status")))
    fase = _sel(_prop(props, F_FASE, ("select", "status")))
    video = _files(_prop(props, F_VIDEO, ("files",)))
    st = {"owner": owner, "fase": fase, "n_video": len(video), "last_edited_time": page.get("last_edited_time")}
    if owner != OWNER_IA:
        raise StatoDiverso(f"Owner e' {owner!r}, non 'IA': l'agente scrive solo sulle schede assegnate all'IA")
    if me_id is not None and now_utc is not None:
        q = quiet_problem(page, me_id, now_utc)
        if q:
            raise StatoDiverso(q)
    if need_fase is not None and fase != need_fase:
        raise StatoDiverso(f"Fase e' {fase!r}, non {need_fase!r} come atteso")
    if need_video and not video:
        raise StatoDiverso("il campo 'Video finale' e' vuoto: serve il video per questa transizione")
    if video_empty and video:
        raise StatoDiverso("il campo 'Video finale' contiene gia' un file: non si sovrascrive (lo cambia Cipriano)")
    return st


def _note_items(props):
    pr = _prop(props, F_NOTE, ("rich_text",))
    items = pr.get("rich_text") or []
    if len(items) >= rc.LONG_ITEMS or pr.get("has_more"):
        raise Perimetro("il campo Note e' troppo lungo per essere riletto per intero: non si aggiunge nulla (lo fa Cipriano)")
    keep = []
    for it in items:
        if not isinstance(it, dict) or it.get("type", "text") != "text" or not isinstance((it.get("text") or {}).get("content", it.get("plain_text")), str):
            raise Perimetro("il campo Note contiene elementi che non sono testo semplice (menzioni, formule): non si modifica")
        content = (it.get("text") or {}).get("content", it.get("plain_text"))
        new = {"type": "text", "text": {"content": content}}
        link = (it.get("text") or {}).get("link")
        if link:
            new["text"]["link"] = link
        if isinstance(it.get("annotations"), dict):
            new["annotations"] = it["annotations"]
        keep.append(new)
    return keep


def _stable(props, skip):
    out = {}
    for n, pr in props.items():
        if n in skip or not isinstance(pr, dict) or pr.get("type") in SKIP_TYPES:
            continue
        out[n] = rc.flatten(pr)
    return out


def build_edit_patch(page, v, today_rome):
    """(properties da scrivere, valori attesi dopo la scrittura). Solo nomi in ALLOWED_PROPERTY_NAMES."""
    props = page["properties"]
    write, expect = {}, {}
    if "fase" in v:
        pr = props[F_FASE]
        write[F_FASE] = {pr["type"]: {"name": v["fase"][1]}}
        expect[F_FASE] = v["fase"][1]
    if "owner_a" in v:
        pr = props[F_OWNER]
        write[F_OWNER] = {pr["type"]: {"name": v["owner_a"]}}
        expect[F_OWNER] = v["owner_a"]
    if "nota" in v:
        keep = _note_items(props)
        old = _plain(props[F_NOTE].get("rich_text") or [])
        piece = ("\n" if old else "") + f"{NOTE_MARK} {today_rome.strftime('%Y-%m-%d %H:%M')} {v['nota']}"
        write[F_NOTE] = {"rich_text": keep + [{"type": "text", "text": {"content": piece}}]}
        expect[F_NOTE] = old + piece
    assert set(write) <= ALLOWED_PROPERTY_NAMES
    return write, expect


def verify_edit(page, want_id, expect, before_props, me_id):
    """Elenco dei problemi di una scheda riletta (vuoto = tutto come voluto, nessun'altra proprieta' cambiata, ultimo editor = il bot)."""
    if not isinstance(page, dict) or aps.uuid32(page.get("id")) != want_id or not isinstance(page.get("properties"), dict):
        return ["risposta senza id/properties della scheda"]
    props, problems = page["properties"], []
    for name, want in expect.items():
        pr = props.get(name)
        if not isinstance(pr, dict):
            problems.append(f"{name} assente")
        elif pr.get("type") == "rich_text":
            if _plain(pr.get("rich_text") or []) != want:
                problems.append(f"{name}: il testo non e' (vecchio testo + riga aggiunta)")
        elif pr.get("type") in ("select", "status"):
            if _sel(pr) != want:
                problems.append(f"{name}: {_sel(pr)!r} invece di {want!r}")
        else:
            problems.append(f"{name}: tipo inatteso")
    if _stable(props, set(expect)) != _stable(before_props, set(expect)):
        problems.append("un'altra proprieta' della scheda e' cambiata")
    uid, utype = aps._user(page, "last_edited_by")
    if uid != aps.uuid32(me_id):
        problems.append("last_edited_by non e' il bot 'Agenti cloud' che ha scritto")
    if uid == aps.uuid32(aps.APPROVER_PERSON_ID):
        problems.append("last_edited_by e' la persona Cipriano")
    return problems


# --------------------------------------------------------------------------- registro locale

def log_dir():
    """Cartella FISSA del registro (mai da variabili d'ambiente: R5/D-2)."""
    return os.path.join("/tmp" if os.name != "nt" else tempfile.gettempdir(), "write_card-log")


def log_path():
    return os.path.join(log_dir(), "write_card.log.jsonl")


def log_write(entry, path=None):
    """Aggiunge una riga al registro. Ritorna True se scritta. Non solleva mai (non deve fermare ne' mascherare l'esito) ma, se non riesce, lo segna in LOG_STATE.
    Apre il file SOLO se non esiste (lo crea con il marcatore LOG_MARK) o se e' un file regolare, non collegamento, del nostro utente, con quel marcatore nella prima riga."""
    ok = False
    try:
        p = path or log_path()
        d = os.path.dirname(os.path.abspath(p))
        os.makedirs(d, mode=0o700, exist_ok=True)
        if os.path.islink(d) or not os.path.isdir(d):
            raise OSError("la cartella del registro non e' una cartella vera (un collegamento non si segue)")
        if hasattr(os, "getuid") and os.stat(d).st_uid != os.getuid():
            raise OSError("cartella del registro di un altro utente")
        line = (json.dumps(dict(entry, quando=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")), ensure_ascii=False) + "\n").encode("utf-8")
        base = getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if os.path.lexists(p):
            if os.path.islink(p) or not os.path.isfile(p):
                raise OSError("il registro non e' un file regolare")
            if hasattr(os, "getuid") and os.stat(p).st_uid != os.getuid():
                raise OSError("registro di un altro utente")
            with open(p, "rb") as fh:
                first = fh.readline().rstrip(b"\r\n")
            if first != LOG_MARK.encode("ascii"):
                raise OSError("file esistente senza il marcatore del registro: non si scrive")
            fd = os.open(p, os.O_WRONLY | os.O_APPEND | base)
        else:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | base, 0o600)
            os.write(fd, (LOG_MARK + "\n").encode("ascii"))
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        ok = True
    except (OSError, ValueError):
        ok = False
    if not ok:
        LOG_STATE["ok"] = False
    return ok


# --------------------------------------------------------------------------- multipart (upload del video)

def multipart(filename, data, content_type="video/mp4"):
    boundary = "wc" + uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return boundary, head + data + tail


def _send_file(backend, upload_id, filename, data, cl, tok):
    boundary, body = multipart(filename, data)
    headers = dict(backend._headers())
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    status, _h, raw = backend.transport("POST", cl.NOTION_API_BASE + f"/file_uploads/{upload_id}/send", headers, body, UPLOAD_S)
    try:
        obj = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        obj = {}
    if not (200 <= status < 300):
        raise RuntimeError(cl.redact(f"invio del file: HTTP {status} {obj.get('code', '')}", tok))
    return obj


# --------------------------------------------------------------------------- esecuzione

def _report(out, payload, riga):
    payload = dict(payload, riga_rapporto=riga, registro_locale=LOG_STATE["ok"])
    print(json.dumps(payload, ensure_ascii=False), file=out)


def run(argv=None, *, transport=None, backend_kw=None, today=None, now=None, out=sys.stdout, err=sys.stderr, log=None, workspace=None):
    ap = argparse.ArgumentParser(description="Scrive sulla scheda dei contenuti SOLO dentro l'elenco chiuso (bot 'Agenti cloud').", allow_abbrev=False)
    ap.add_argument("--page-id", help="id della scheda (32 esadecimali)")
    ap.add_argument("--fase-da"), ap.add_argument("--fase-a")
    ap.add_argument("--owner-a")
    ap.add_argument("--nota")
    ap.add_argument("--video-finale")
    ap.add_argument("--task-pending", action="store_true")
    ap.add_argument("--tipo"), ap.add_argument("--categoria"), ap.add_argument("--testo"), ap.add_argument("--scadenza")
    ap.add_argument("--dry-run", action="store_true", help="valida e stampa cosa scriverebbe; NESSUNA chiamata di rete")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return EXIT_ARGS if e.code not in (0, None) else 0
    LOG_STATE["ok"] = True
    now_rome = rome_now(now)
    today_d = today or now_rome.date()
    mode, v = None, None
    try:
        edit_given = any(x is not None for x in (args.fase_da, args.fase_a, args.owner_a, args.nota))
        extras = [bool(edit_given), args.video_finale is not None, bool(args.task_pending)]
        if sum(extras) != 1:
            raise Rifiutata("serve esattamente UNA famiglia di scrittura: (--fase-da/--fase-a/--owner-a/--nota) oppure --video-finale oppure --task-pending")
        if args.task_pending:
            mode, v = "task", validate_task(args, today_d)
            if args.page_id:
                raise Rifiutata("--task-pending non vuole --page-id (non e' legato a una scheda)")
        else:
            pid = aps.uuid32(args.page_id)
            if not pid:
                raise Rifiutata("--page-id deve essere esattamente un id Notion di 32 esadecimali")
            if any(x is not None for x in (args.tipo, args.categoria, args.testo, args.scadenza)):
                raise Rifiutata("--tipo/--categoria/--testo/--scadenza valgono solo con --task-pending")
            if args.video_finale is not None:
                mode, v = "video", dict(zip(("path", "name", "size"), validate_video(args.video_finale, workspace)))
            else:
                mode, v = "edit", validate_edit(args)
    except Rifiutata as e:
        print(f"RIFIUTATA: {e}", file=err)
        return EXIT_ARGS

    page_id = aps.uuid32(args.page_id) if mode != "task" else None
    if args.dry_run:
        print(json.dumps({"dry_run": True, "modo": mode, "page_id": page_id, "richiesta": {k: val for k, val in v.items() if k != "path"}}, ensure_ascii=False), file=out)
        return EXIT_OK

    dl = aps._Deadline(aps._deadline_s())
    ds = TASK_PENDING_DATA_SOURCE if mode == "task" else CARD_DATA_SOURCE
    try:
        cl, backend, tok = aps.make_backend(data_source_id=ds, transport=transport, backend_kw=backend_kw, deadline=dl)
    except aps.ApprovalUnavailable as e:
        print(f"NON SCRITTA: {e}", file=err)
        return EXIT_NOTION

    entry = {"modo": mode, "page_id": page_id}
    try:
        with backend.time_budget(aps._deadline_s()):
            me = backend._request("GET", "/users/me")
            why = ps.whoami_problem(me)
            if why:
                print(f"NON SCRITTA (identita' del token): {why}", file=err)
                log_write(dict(entry, esito="identita-non-ammessa", exit=EXIT_VERIFY), log)
                return EXIT_VERIFY
            me_id = aps.uuid32(me.get("id"))
            if mode == "task":
                return _do_task(backend, cl, tok, v, me_id, out, err, entry, log)
            first = backend._request("GET", "/pages/" + page_id)
            if mode == "edit":
                return _do_edit(backend, cl, tok, dl, page_id, first, v, me_id, now_rome, out, err, entry, log)
            return _do_video(backend, cl, tok, dl, page_id, first, v, me_id, out, err, entry, log, now_rome.astimezone(datetime.timezone.utc))
    except Perimetro as e:
        print(f"NON SCRITTA (perimetro): {e}", file=err)
        log_write(dict(entry, esito="perimetro", motivo=str(e), exit=EXIT_OUTSIDE), log)
        return EXIT_OUTSIDE
    except StatoDiverso as e:
        print(f"NON SCRITTA (stato non atteso): {e}", file=err)
        log_write(dict(entry, esito="stato-diverso", motivo=str(e), exit=EXIT_STATE), log)
        return EXIT_STATE
    except cl._Ambiguous as e:
        print(f"ESITO INCERTO ({cl.redact(e, tok)}): la scrittura potrebbe essere arrivata. NON rilanciare: guarda la scheda.", file=err)
        log_write(dict(entry, esito="incerto", exit=EXIT_AMBIGUOUS), log)
        return EXIT_AMBIGUOUS
    except Exception as e:  # noqa: BLE001
        print(f"NON SCRITTA: {cl.redact(str(e), tok)[:200]} ({type(e).__name__})", file=err)
        log_write(dict(entry, esito="errore-prima-della-scrittura", tipo=type(e).__name__, exit=EXIT_NOTION), log)
        return EXIT_NOTION


def _reread(backend, dl, page_id, seconds=10.0):
    dl.reset(seconds)
    with backend.time_budget(seconds):
        return backend._request("GET", "/pages/" + page_id)


def _do_edit(backend, cl, tok, dl, page_id, first, v, me_id, now_rome, out, err, entry, log):
    need_fase = v["fase"][0] if "fase" in v else None
    video_needed = "fase" in v
    now_utc = now_rome.astimezone(datetime.timezone.utc)
    st = check_card(first, page_id, need_fase=need_fase, need_video=video_needed, me_id=me_id, now_utc=now_utc)
    write, expect = build_edit_patch(first, v, now_rome)
    # secondo controllo subito prima della scrittura (compare-and-set a piu' passi: vedi docstring; Notion non ha ETag, la finestra residua e' dichiarata)
    second = backend._request("GET", "/pages/" + page_id)
    st2 = check_card(second, page_id, need_fase=need_fase, need_video=video_needed, me_id=me_id, now_utc=now_utc)
    if _snap(second) != _snap(first):
        raise StatoDiverso("la scheda e' cambiata tra la prima lettura e la scrittura (ora, autore o qualunque campo): nessuna scrittura, un run successivo potra' riprovare")
    entry.update(prima=prev_values(second["properties"]), scritto=sorted(write))
    backend.min_interval_s = 0.0                      # R5/D-3: nessuna attesa "di ritmo" fra la seconda lettura e la PATCH (riduce la finestra)
    t_cas = time.monotonic()
    resp = backend._request("PATCH", "/pages/" + page_id, {"properties": write}, idempotent=False)
    entry["finestra_ms_max"] = int((time.monotonic() - t_cas) * 1000)      # dalla fine della seconda lettura alla risposta della PATCH (limite superiore della finestra)
    entry["scritta"] = True
    problems = verify_edit(resp, page_id, expect, first["properties"], me_id)
    if not problems:
        problems = verify_edit(_reread(backend, dl, page_id), page_id, expect, first["properties"], me_id)
    if problems:
        print("ALLARME: la scheda dopo la scrittura NON e' come voluta: " + "; ".join(problems) + f". id={page_id}. VALORI PRECEDENTI dei campi scritti (per ripristinarli a mano): "
              + json.dumps(entry.get("prima"), ensure_ascii=False)[:1200] + ". Non toccarla: avvisa Cipriano.", file=err)
        log_write(dict(entry, esito="allarme", problemi=problems, exit=EXIT_VERIFY), log)
        return EXIT_VERIFY
    desc = []
    if "fase" in v:
        desc.append(f"Fase {v['fase'][0]} -> {v['fase'][1]}")
    if "owner_a" in v:
        desc.append("Owner IA -> Cipriano")
    if "nota" in v:
        desc.append(f"nota {NOTE_MARK} aggiunta ({len(v['nota'])} caratteri)")
    riga = f"Scheda {page_id[:8]}: " + ", ".join(desc) + " (scritto dal bot, riletto, nessun altro campo cambiato)."
    log_write(dict(entry, esito="ok", cosa=desc, exit=EXIT_OK), log)
    _report(out, {"scritta": True, "riletta": True, "page_id": page_id, "campi": sorted(write), "cosa": desc}, riga)
    return EXIT_OK


def _do_video(backend, cl, tok, dl, page_id, first, v, me_id, out, err, entry, log, now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc)
    st = check_card(first, page_id, video_empty=True, me_id=me_id, now_utc=now_utc)
    if st["fase"] not in FASI_VIDEO_OK:
        raise StatoDiverso(f"Fase e' {st['fase']!r}: il video finale si allega solo in {' o '.join(FASI_VIDEO_OK)}")
    with open(v["path"], "rb") as fh:
        data = fh.read(VIDEO_MAX_BYTES + 1)
    if len(data) != v["size"] or data[4:8] != b"ftyp":
        raise StatoDiverso("il file e' cambiato durante la lettura")
    entry.update(file=v["name"], byte=v["size"], prima=prev_values(first["properties"]))
    dl.reset(UPLOAD_S)
    with backend.time_budget(UPLOAD_S):
        up = backend._request("POST", "/file_uploads", {"filename": v["name"], "content_type": "video/mp4"}, idempotent=False)
        uid = up.get("id") if isinstance(up, dict) else None
        if not isinstance(uid, str) or not aps.uuid32(uid):
            raise cl._Ambiguous("creazione dell'upload senza id leggibile")
        sent = _send_file(backend, uid, v["name"], data, cl, tok)
        if sent.get("status") != "uploaded":
            raise RuntimeError(f"upload non completato (status={sent.get('status')!r})")
        second = backend._request("GET", "/pages/" + page_id)
        st2 = check_card(second, page_id, video_empty=True, me_id=me_id, now_utc=now_utc)
        if _snap(second) != _snap(first):
            raise StatoDiverso("la scheda e' cambiata durante l'upload (ora, autore o qualunque campo): il file caricato non e' stato collegato (scade da solo in un'ora)")
        patch = {F_VIDEO: {"files": [{"type": "file_upload", "file_upload": {"id": uid}, "name": v["name"]}]}}
        assert set(patch) <= ALLOWED_PROPERTY_NAMES
        entry["prima"] = prev_values(second["properties"])
        backend.min_interval_s = 0.0                  # R5/D-3: nessuna attesa "di ritmo" fra la seconda lettura e la PATCH
        t_cas = time.monotonic()
        resp = backend._request("PATCH", "/pages/" + page_id, {"properties": patch}, idempotent=False)
        entry["finestra_ms_max"] = int((time.monotonic() - t_cas) * 1000)
    entry["scritta"] = True

    def probs(page):
        if not isinstance(page, dict) or aps.uuid32(page.get("id")) != page_id or not isinstance(page.get("properties"), dict):
            return ["risposta senza id/properties della scheda"]
        pr, p = page["properties"].get(F_VIDEO), []
        fl = _files(pr) if isinstance(pr, dict) and pr.get("type") == "files" else []
        if len(fl) != 1 or fl[0].get("name") != v["name"]:
            p.append("'Video finale' non contiene esattamente il file caricato")
        if _stable(page["properties"], {F_VIDEO}) != _stable(first["properties"], {F_VIDEO}):
            p.append("un'altra proprieta' della scheda e' cambiata")
        uid2, _t = aps._user(page, "last_edited_by")
        if uid2 != me_id:
            p.append("last_edited_by non e' il bot")
        return p
    problems = probs(resp) or probs(_reread(backend, dl, page_id))
    if problems:
        print("ALLARME: la scheda dopo il caricamento NON e' come voluta: " + "; ".join(problems) + f". id={page_id}. Non toccarla: avvisa Cipriano.", file=err)
        log_write(dict(entry, esito="allarme", problemi=problems, exit=EXIT_VERIFY), log)
        return EXIT_VERIFY
    riga = f"Scheda {page_id[:8]}: Video finale caricato ({v['name']}, {v['size']} byte), scritto dal bot e riletto."
    log_write(dict(entry, esito="ok", exit=EXIT_OK), log)
    _report(out, {"scritta": True, "riletta": True, "page_id": page_id, "campi": [F_VIDEO], "file": v["name"], "byte": v["size"]}, riga)
    return EXIT_OK


def _do_task(backend, cl, tok, v, me_id, out, err, entry, log):
    schema = backend._request("GET", "/data_sources/" + backend.container)
    sprops = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(sprops, dict):
        raise Perimetro("schema di Task Pending non leggibile")
    title_name = next((n for n, p in sprops.items() if isinstance(p, dict) and p.get("type") == "title"), None)
    need = {TP["stato"]: ("select", "status"), TP["categoria"]: ("select",), TP["scadenza"]: ("date",)}
    for n, kinds in need.items():
        if not isinstance(sprops.get(n), dict) or sprops[n].get("type") not in kinds:
            raise Perimetro(f"Task Pending: manca la colonna {n!r} (o non e' del tipo {'/'.join(kinds)}): nessuna scrittura")
    if not title_name:
        raise Perimetro("Task Pending: nessuna colonna titolo")
    st_type = sprops[TP["stato"]]["type"]
    opts = [o.get("name") for o in (sprops[TP["stato"]].get(st_type) or {}).get("options", []) if isinstance(o, dict)]
    if TP_STATO_INIZIALE not in opts:
        raise Perimetro(f"Task Pending: lo stato {TP_STATO_INIZIALE!r} non esiste tra le opzioni")
    cat_opts = [o.get("name") for o in (sprops[TP["categoria"]].get("select") or {}).get("options", []) if isinstance(o, dict)]
    if v["categoria"] not in cat_opts:
        raise Perimetro(f"Task Pending: la categoria {v['categoria']!r} non esiste tra le opzioni")
    for p in backend._raw_query(None):                                           # dedup: stessa voce ancora aperta
        pr = p.get("properties") if isinstance(p, dict) else None
        if not isinstance(pr, dict):
            continue
        titolo = _plain((pr.get(title_name) or {}).get("title") or [])
        stato = _sel(pr[TP["stato"]]) if isinstance(pr.get(TP["stato"]), dict) else ""
        if titolo == v["titolo"] and stato != "Completato":
            raise StatoDiverso(f"esiste gia' una voce aperta con lo stesso titolo (id={p.get('id')}): non se ne crea un'altra")
    props = {title_name: {"title": [{"type": "text", "text": {"content": v["titolo"]}}]},
             TP["stato"]: {st_type: {"name": TP_STATO_INIZIALE}},
             TP["categoria"]: {"select": {"name": v["categoria"]}},
             TP["scadenza"]: {"date": {"start": v["scadenza"]}}}
    parent = {"type": "data_source_id", "data_source_id": backend.container}
    entry.update(titolo=v["titolo"], scritto=sorted(props))
    resp = backend._request("POST", "/pages", {"parent": parent, "properties": props}, idempotent=False)
    entry["scritta"] = True
    uid, _t = aps._user(resp, "created_by") if isinstance(resp, dict) else ("", None)
    pr = resp.get("properties") if isinstance(resp, dict) else None
    problems = []
    if not isinstance(pr, dict) or _plain((pr.get(title_name) or {}).get("title") or []) != v["titolo"]:
        problems.append("titolo diverso")
    if isinstance(pr, dict) and _sel(pr.get(TP["stato"]) or {"type": "select"}) != TP_STATO_INIZIALE:
        problems.append("stato diverso da 'Da fare'")
    if uid != me_id:
        problems.append("created_by non e' il bot")
    if problems:
        print("ALLARME: la voce di Task Pending NON e' come voluta: " + "; ".join(problems) + f". id={resp.get('id') if isinstance(resp, dict) else '?'}.", file=err)
        log_write(dict(entry, esito="allarme", problemi=problems, exit=EXIT_VERIFY), log)
        return EXIT_VERIFY
    riga = f"Task Pending creato per Cipriano ({v['tipo']}, scadenza {v['scadenza']}): {v['titolo'][:80]}"
    log_write(dict(entry, esito="ok", id=resp.get("id"), exit=EXIT_OK), log)
    _report(out, {"scritta": True, "id": resp.get("id"), "titolo": v["titolo"]}, riga)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(run())
