"""
Cancello di spesa NEL CODICE (punto 28, 18/10/2026).

Non e' un hook: vale per chiunque lanci uno script del pipeline (sessione, terminale, routine, lab).
Due compiti:
  1. REGISTRO COSTI con blocco: unico punto di scrittura di generations/costs.json. Blocco tra processi
     (file .lock), scrittura atomica (file temporaneo + os.replace), mai azzerare lo storico se il file e' rovinato
     (se ne conserva una copia e si segnala).
  3. BUDGET DEL LAB (18/10/2026, punto 41): 50 USD al mese e 5 USD per prova per le voci con project = "Lab"
     (valori in `Sistema/lab/budget.yaml`, 50 e 5 se il file manca). Avvisi al 50% e all'80% del mese.
  2. TETTO GIORNALIERO: 10 USD al giorno in totale; 8 USD per una prima generazione (2 USD di riserva per i
     tentativi, cioe' il 20%). Se il registro non e' leggibile il cancello CHIUDE (fail closed).

Il tetto per contenuto (5 USD) e la richiesta di --max-usd restano in vp_common.approval_gate, che chiama questo modulo.
La riga di approvazione `spesa-video` in approvazioni.md NON e' controllata qui: e' una decisione di Cipriano ed e' fatta
rispettare dall'hook spend-guard per le chiamate degli agenti.

Limiti dichiarati: il controllo confronta la spesa REGISTRATA con il preventivo; due processi lanciati nello stesso
istante vedono lo stesso registro (nessuna prenotazione del budget). Chi scrive a mano in costs.json aggira il registro.
"""
import datetime
import json
import math
import os
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LEDGER = SCRIPT_DIR / "generations" / "costs.json"
DAILY_CAP_USD = 10.0     # tetto giornaliero totale (tentativi ripetuti inclusi)
NEW_GEN_CAP_USD = 8.0    # prima generazione di una card: lascia il 20% di riserva per i tentativi
LAB_PROJECT = "Lab"
LAB_BUDGET_FILE = SCRIPT_DIR.parents[1] / "lab" / "budget.yaml"       # Sistema/lab/budget.yaml
LAB_MONTHLY_USD = 50.0        # decisione di Cipriano 18/10/2026
LAB_PER_TEST_USD = 5.0
LAB_ALERTS = (0.5, 0.8)
LOCK_TIMEOUT_S = 30.0
LOCK_STALE_S = 120.0


class SpendGateError(Exception):
    """Il cancello nega la spesa (o non puo' verificarla)."""


class LedgerError(SpendGateError):
    """Registro costi illeggibile."""


class LedgerUnavailable(LedgerError):
    """v8.3 (N2): il file del registro esiste (o non si riesce a dire che non esiste) ma NON si riesce a leggerlo per un errore di accesso o di lettura
    (permesso negato, cartella al posto del file, disco, antivirus). NON e' 'assente' (lista vuota) ne' 'rovinato' (copia e ripartenza): il cancello resta
    CHIUSO e nessuno riscrive il file. E' una LedgerError: chi gia' nega sulle LedgerError nega anche qui."""


# --- registro condiviso PC/cloud (punto 58, cost_ledger.py) ---------------------------------------------------------
def shared_mode():
    """True se i costi stanno nel registro condiviso su Notion (COST_LEDGER_BACKEND=notion). Default: locale, come prima."""
    return os.environ.get("COST_LEDGER_BACKEND", "local").strip().lower() == "notion"


def _spool_dir_set():
    return bool(os.environ.get("COST_LEDGER_SPOOL_DIR", "").strip())


def _spool_path():
    """Coda dei costi in sospeso: generations/costs-pending.jsonl (come sempre) oppure, se e' impostata COST_LEDGER_SPOOL_DIR (v8, coda FUORI da
    OneDrive), <cartella>/costs-pending.jsonl: cost_ledger controlla che la cartella esista, sia scrivibile e non sia sotto OneDrive. Variabile
    impostata ma cartella non valida o cost_ledger troppo vecchio (manca resolve_spool_path) = eccezione: chi chiama nega (cancello chiuso)."""
    default = SCRIPT_DIR / "generations" / "costs-pending.jsonl"
    if not _spool_dir_set():
        return default
    import cost_ledger
    return cost_ledger.resolve_spool_path(default)


def _build_shared():
    """CostLedger su Notion. Chiave: NOTION_TOKEN se c'e', poi il .env del pipeline (PC); nel cloud nessuna chiave: richiesta senza
    Authorization, la aggiunge il proxy dell'ambiente. Qualsiasi problema = LedgerError (cancello chiuso)."""
    try:
        import cost_ledger
        token = os.environ.get("NOTION_TOKEN")
        source = "ambiente (NOTION_TOKEN)" if token else None
        if not token:
            try:
                import notion_client
                token = notion_client.load_token()
                source = ".env del pipeline tramite notion_client"
            except (ImportError, SystemExit):
                token = ""
        extra = {"legacy_spool_path": SCRIPT_DIR / "generations" / "costs-pending.jsonl"} if _spool_dir_set() else {}      # coda spostata: la vecchia deve restare vuota
        return cost_ledger.get_ledger("notion", token=token, token_source=source, origin=os.environ.get("COST_LEDGER_ORIGIN", "PC"),
                                      spool_path=_spool_path(), **extra)
    except Exception as e:                                      # noqa: BLE001
        raise LedgerError(f"registro condiviso non disponibile: {str(e)[:260]}")


def _shared():
    return _build_shared() if shared_mode() else None


def _shared_for(path=None):
    """Registro condiviso solo per il registro di DEFAULT (generations/costs.json, che e' il `path` passato dai runner di produzione):
    un `path` esplicito diverso (prove, copie, file temporanei) resta LOCALE, cosi' i test non scrivono righe finte su Notion."""
    if path is not None and Path(path).resolve() != LEDGER.resolve():
        return None
    return _shared()


_QUEUE_SEQ = 0


def _queue_entry(entry):
    """Mette la voce nella coda dei costi in sospeso (stesso formato di cost_ledger: una riga JSON) senza dipendere dalla rete: serve quando il
    registro condiviso non si riesce nemmeno a costruire. NESSUN append nudo su file condivisi tra processi e NESSUN blocco da attendere:
    la voce va SEMPRE in un file UNICO (nome con pid, nanosecondi, contatore; scritto in un .tmp, sincronizzato e rinominato su un nome
    che non esiste: mai sovrascritto) tramite cost_ledger.append_pending; se cost_ledger non si importa, lo stesso procedimento inline.
    Una voce gia' pagata non si scarta mai per un blocco non ottenuto. Il rinvio (cost_ledger) ripesca tutto. Non solleva mai."""
    global _QUEUE_SEQ
    try:
        item = {"op": "record_legacy", "entry": entry, "run_key": f"{entry.get('asset', '')}~q{time.time_ns()}"}
        q = SCRIPT_DIR / "generations" / "costs-pending.jsonl"
        try:
            q = _spool_path()
        except Exception as e:                # noqa: BLE001 - coda esterna non valida: la voce pagata va comunque nella coda di sempre (e li' chiude i cancelli)
            print(f"[spend_gate] ATTENZIONE: COST_LEDGER_SPOOL_DIR non utilizzabile ({str(e)[:160]}): la voce va nella coda di sempre", file=sys.stderr)
        line = json.dumps(item, ensure_ascii=False, default=str)
        try:
            import cost_ledger
            cost_ledger.append_pending(q, [line])
        except Exception:                     # noqa: BLE001 - cost_ledger vecchio/rotto o blocco non ottenibile: file unico, senza append
            q.parent.mkdir(parents=True, exist_ok=True)
            _QUEUE_SEQ += 1
            uniq = q.with_name(f"{q.name}.item-{os.getpid()}-{time.time_ns()}-{_QUEUE_SEQ}")
            tmp = uniq.with_name(uniq.name + ".tmp")
            tmp.write_bytes((line + "\n").encode("utf-8"))
            os.rename(tmp, uniq)
    except Exception as e:                    # noqa: BLE001
        print(f"[spend_gate] ATTENZIONE: voce NON accodata ({type(e).__name__}): {json.dumps(entry, ensure_ascii=False, default=str)[:400]}",
              file=sys.stderr)


# --- v8.4 (N3): spese PAGATE che non si sono potute scrivere in costs.json -------------------------------------------------
# Prima (v7, v8.x): se `ledger_append` falliva DOPO il pagamento per un motivo diverso da 'costs.json illeggibile' (blocco scaduto, cartella/file in sola scrittura negata,
# rinomina fallita) la voce non andava da nessuna parte: il cancello giornaliero locale leggeva un totale senza quella spesa e ne lasciava passare altre (provato: 7 USD nel
# file, 4 giri da 0,5 USD, il cancello passa 4 su 4, spesa vera 9,0 contro un tetto di 8). Ora la voce va in un FILE UNICO accanto al registro, `<registro>.unrecorded-<ns>-<pid>-<n>`,
# che i cancelli LOCALI CONTANO (non la coda dell'ombra: quella resta il canale verso Notion e puo' avere voci in condizioni normali). La prossima scrittura riuscita
# le riporta in costs.json (sotto il blocco, senza doppioni: deduplica per contenuto) e toglie i file. Nessun append nudo: file unico scritto in un .tmp e rinominato.
UNRECORDED_TAG = ".unrecorded-"
_UNREC_SEQ = 0


def _canon(entry):
    return json.dumps(entry, sort_keys=True, ensure_ascii=True, default=str)


def _short(x, n):
    """v8.6: i primi `n` caratteri di str(x) per un messaggio. str() di un intero enorme (> 4300 cifre) da' ValueError: lo si descrive senza convertirlo (da JSON non arriva mai: json.loads ha lo stesso limite)."""
    if isinstance(x, int) and x.bit_length() > 10000:
        return "<intero enorme>"[:n]
    return str(x)[:n]


def _unrecorded_put(path, entry):
    """Salva la voce pagata come 'non registrata' in un file UNICO accanto a `path`. Ritorna il nome del file o None se non e' stato possibile (lo dice a video). Non solleva mai."""
    global _UNREC_SEQ
    path = Path(path)
    tmp = None
    made = False
    try:
        _UNREC_SEQ += 1
        uniq = path.with_name(f"{path.name}{UNRECORDED_TAG}{time.time_ns():020d}-{os.getpid()}-{_UNREC_SEQ}")
        tmp = uniq.with_name(uniq.name + ".tmp")
        data = (json.dumps({"op": "unrecorded", "entry": entry}, ensure_ascii=False, default=str) + chr(10)).encode("utf-8")
        with open(tmp, "xb") as fh:                   # creazione esclusiva: mai un file di un altro sovrascritto
            made = True
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, uniq)                          # su Windows non sovrascrive
        return uniq.name
    except Exception as e:                            # noqa: BLE001
        if made and tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        print(f"[spend_gate] ATTENZIONE: voce NON salvata fra le 'non registrate' ({type(e).__name__}: {str(e)[:120]})", file=sys.stderr)
        return None


def _unrecorded_scan(path, strict=True):
    """Nomi dei file di voci non registrate di `path`, in ordine di creazione. Cartella ASSENTE = nessun registro = lista vuota; cartella che non si elenca = LedgerUnavailable
    (le voci non registrate non si potrebbero contare: cancello CHIUSO, regola Ops 33). strict: un `.tmp` (scrittura in corso o interrotta) NEGA; non strict (riconciliazione) lo ignora."""
    path = Path(path)
    pre = os.path.normcase(path.name + UNRECORDED_TAG)
    names = []
    try:
        with os.scandir(str(path.parent)) as it:
            for de in it:
                if os.path.normcase(de.name).startswith(pre):
                    names.append(de.name)
    except FileNotFoundError:
        return []
    except OSError as ex:
        raise LedgerUnavailable(f"registro costi NON accessibile ({path.name}): la cartella non si elenca ({type(ex).__name__}), quindi le spese pagate e non registrate non si possono contare. Cancello CHIUSO")
    names.sort()
    tmps = [n for n in names if n.endswith(".tmp")]
    if tmps and strict:
        raise LedgerUnavailable(f"voce pagata e non registrata in scrittura o interrotta ({tmps[0]}): non si sa se contarla. Cancello CHIUSO finche' Ops non la sistema")
    return [n for n in names if not n.endswith(".tmp")]


def _unrecorded_read(path, strict=True):
    """[(nome, voce)] delle voci pagate e non registrate di `path`. Un file sparito tra l'elenco e la lettura e' stato riportato in costs.json (che si legge DOPO): si salta.
    strict: un file che non si legge o una voce malformata (costo non finito/negativo, data non valida) NEGA; non strict (riconciliazione): si lascia dov'e'."""
    path = Path(path)
    out = []
    for name in _unrecorded_scan(path, strict):
        p = path.with_name(name)
        try:
            raw = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as ex:
            if strict:
                raise LedgerUnavailable(f"voce pagata e non registrata NON leggibile ({name}, {type(ex).__name__}). Cancello CHIUSO")
            continue
        try:
            entry = json.loads(raw)["entry"]
            if not isinstance(entry, dict):
                raise ValueError("la voce non e' un oggetto")
            try:
                entry_cost_usd(entry)         # v8.6: UNA regola per ogni lettore del costo (regola Ops 38). Prima: float(x or 0): `[]` valeva 0 e `true` 1 USD
            except LedgerError as ex:
                raise ValueError("costo non valido: " + _short(ex, 70))
            datetime.datetime.fromisoformat(str(entry.get("created")))
        except (ValueError, KeyError, TypeError) as ex:
            if strict:
                raise LedgerError(f"voce pagata e non registrata malformata ({name}: {str(ex)[:80]}): cancello CHIUSO finche' Ops non la sistema")
            continue
        out.append((name, entry))
    return out


def _writable_or_deny(path):
    """Il cancello locale NEGA se la cartella del registro non accetta un file nuovo: una spesa pagata dopo questo controllo non si potrebbe registrare in nessun modo
    (ne' in costs.json ne' come voce 'non registrata'). Prova con un file vuoto UNICO creato e tolto. Cartella assente: come oggi (la crea ledger_append)."""
    path = Path(path)
    probe = path.with_name(f"{path.name}.probe-{os.getpid()}-{time.time_ns()}")
    try:
        fd = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileNotFoundError:
        return
    except OSError as ex:
        raise LedgerUnavailable(f"registro costi NON accessibile ({path.name}): la cartella non accetta file nuovi ({type(ex).__name__}), quindi una spesa pagata non si potrebbe registrare. Cancello CHIUSO")
    os.close(fd)
    try:
        probe.unlink()
    except OSError:
        pass


def entry_cost_usd(e):
    """v8.5: costo in USD di UNA voce di costs.json che STA PER ENTRARE in una somma dei cancelli (giornaliero, lab, tetto per contenuto). Assente, None, 0, stringa vuota o False
    valgono 0.0, come sempre. Altrimenti `float(valore)` deve essere FINITO e >= 0: NaN (anche la stringa "nan"), infinito, negativo, non numerico ("abc", una lista) o troppo grande
    per un float = LedgerError, cioe' il cancello NEGA. Prima (v8.4 e precedenti): un NaN rendeva NaN tutto il totale (7,5 + NaN: `NaN + 1 > 8` e' falso e la spesa passava), un
    negativo lo abbassava (7,5 - 5 = 2,5) e un valore non numerico in una voce di oggi era ignorato in silenzio (`except ValueError: continue`). E' la stessa regola delle voci
    `.unrecorded` (costo finito e non negativo), ma qui NEGA invece di scartare. NON sta in `read_ledger`: li' un LedgerError vuol dire 'rovinato' e `_write_local` sposterebbe il file."""
    if not isinstance(e, dict):                                   # v8.6: una voce che non e' un oggetto = rifiuto pulito (prima: AttributeError, traceback)
        raise LedgerError(f"voce non e' un oggetto ({type(e).__name__}: {_short(e, 30)!r}): registro costi. Cancello CHIUSO: la voce va corretta a mano (dopo una copia di costs.json)")
    raw = e.get("cost_usd", 0)
    name = _short(e.get("asset", ""), 45)                         # v8.6: il NOME della voce va per primo nel motivo (l'hook lo taglia a 80 caratteri)
    if raw is None or raw is False or raw == "" or raw == 0:      # assente / None / False / stringa vuota / 0: valgono 0.0 (come `or 0`); una lista o una mappa VUOTE no: sono non numeriche
        return 0.0
    if raw is True:                                               # v8.6: `true` non e' un costo (prima: float(True) = 1 USD, una sovrastima); NEGA come ogni non numerico
        raise LedgerError(f"voce {name!r}: cost_usd non valido (true: un booleano non e' un costo). Cancello CHIUSO: la voce va corretta a mano (con il valore vero, dopo una copia di costs.json)")
    try:
        v = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise LedgerError(f"voce {name!r}: cost_usd non numerico ({_short(raw, 20)!r}). Cancello CHIUSO: la voce va corretta a mano (con il valore vero, dopo una copia di costs.json)")
    if not math.isfinite(v) or v < 0:
        raise LedgerError(f"voce {name!r}: cost_usd non valido ({_short(raw, 20)!r}: serve un numero finito >= 0). Cancello CHIUSO: "
                          f"la voce va corretta a mano (con il valore vero, dopo una copia di costs.json); un valore cosi' non puo' abbassare ne' annullare il totale")
    return v


def _gate_entries(path=None):
    """Voci che i cancelli LOCALI contano: costs.json + le voci pagate e non registrate (senza doppioni). ORDINE: prima le non registrate, poi costs.json: una riconciliazione in
    corso scrive costs.json e POI toglie i file, quindi chi ha visto il file ma legge costs.json dopo la scrittura trova la voce in costs.json e la deduplica (mai un buco)."""
    path = Path(path or LEDGER)
    _writable_or_deny(path)
    pend = _unrecorded_read(path)
    base = read_ledger(path)
    for i, e in enumerate(base):                  # v8.6: una voce che non e' un oggetto = LedgerError con il suo numero (NON in read_ledger: li' vorrebbe dire 'rovinato' e _write_local sposterebbe il file)
        if not isinstance(e, dict):
            raise LedgerError(f"voce n. {i} di {path.name} non e' un oggetto ({type(e).__name__}: {_short(e, 30)!r}). Cancello CHIUSO: la voce va corretta a mano (dopo una copia del file)")
    if not pend:
        return base
    seen = Counter(_canon(e) for e in base)
    extra = []
    for _, e in pend:
        c = _canon(e)
        if seen[c] > 0:                       # gia' riportata in costs.json
            seen[c] -= 1
        else:
            extra.append(e)
    return base + extra


def _fold_unrecorded(path, entries):
    """Aggiunge a `entries` (lista di costs.json, in memoria) le voci non registrate non ancora presenti. Ritorna i NOMI dei file da togliere DOPO la scrittura. Best effort:
    qualunque problema (cartella non elencabile, file illeggibile) lascia tutto com'e' e la scrittura della voce nuova prosegue; le voci continuano a contare nei cancelli."""
    try:
        pend = _unrecorded_read(path, strict=False)
    except SpendGateError as ex:
        print(f"[spend_gate] ATTENZIONE: voci non registrate non riportate ({str(ex)[:140]})", file=sys.stderr)
        return []
    seen = Counter(_canon(e) for e in entries)
    names = []
    for name, e in pend:
        c = _canon(e)
        if seen[c] > 0:
            seen[c] -= 1
        else:
            entries.append(e)
        names.append(name)
    return names


def _write_local(path, entry=None, _done=None):
    """Scrive `entry` (se c'e') in costs.json con blocco e scrittura atomica e, nello stesso giro, riporta le voci pagate e non registrate prima. Ritorna quante ne ha riportate.
    Qualunque errore sale: chi chiama (`ledger_append`) mette la voce pagata dove i cancelli la vedono. v8.5 (S11): `_done` (una lista) riceve un elemento DOPO la scrittura riuscita
    (`os.replace` fatto): un errore che arriva piu' tardi (messaggio su stderr, rilascio del blocco, Ctrl-C) non e' un errore della registrazione e `ledger_append` lo sa."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _ledger_lock(path):
        try:
            entries = read_ledger(path)
        except LedgerUnavailable:             # v8.3 (N2): non si riscrive MAI un file che non si e' riusciti a leggere (le voci vecchie andrebbero perse)
            raise
        except LedgerError as e:
            stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
            keep = path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")
            os.replace(path, keep)
            print(f"[spend_gate] ATTENZIONE: {e}. Copia conservata in {keep.name}; si riparte da un registro nuovo "
                  f"(il cancello giornaliero restera' tarato su questa spesa soltanto: ricostruire dallo storico).",
                  file=sys.stderr)
            entries = []
        folded = _fold_unrecorded(path, entries)
        if entry is not None:
            entries.append(entry)
        elif not folded:
            return 0
        _atomic_write(path, entries)
        if _done is not None:                 # v8.5 (S11): da qui costs.json CONTIENE la voce
            _done.append(True)
        for name in folded:                   # solo DOPO la scrittura riuscita; se non si toglie, la deduplica per contenuto evita il doppio conteggio
            try:
                path.with_name(name).unlink()
            except FileNotFoundError:
                pass
            except OSError as ex:
                print(f"[spend_gate] ATTENZIONE: voce non registrata {name} riportata in costs.json ma non tolta ({type(ex).__name__}): non si conta due volte", file=sys.stderr)
        if folded:
            print(f"[spend_gate] riportate in {path.name} {len(folded)} spese pagate e non registrate prima", file=sys.stderr)
        return len(folded)


def _say(msg):
    try:
        print(msg, file=sys.stderr)
    except Exception:                         # noqa: BLE001 - stderr chiuso: non deve nascondere l'errore originale che il chiamante rialza subito dopo
        pass


def _keep_paid_entry(path, entry, err):
    """`ledger_append` e' fallito DOPO il pagamento (blocco scaduto, permesso negato, rinomina fallita, file illeggibile...): la voce non deve sparire. (1) file 'non registrata' che i
    cancelli locali contano; (2) la coda di sempre (in ombra e' il canale verso Notion; la rinvia l'ombra o `spool-flush`); (3) il messaggio a video con l'importo. Non solleva mai."""
    path = Path(path)
    try:
        line = json.dumps(entry, ensure_ascii=False, default=str)[:400]
    except Exception:                         # noqa: BLE001
        line = repr(entry)[:400]
    saved = _unrecorded_put(path, entry)
    where = (f"la voce e' salvata come 'non registrata' ({saved}): il cancello locale la conta e la prossima scrittura riuscita la riporta in {path.name}" if saved
             else "ATTENZIONE: la voce NON e' stata salvata in nessun file di questa cartella, esiste solo in questo messaggio e nella coda")
    _say(f"[spend_gate] SPESA NON REGISTRATA in {path.name} ({type(err).__name__}: {str(err)[:200]}). Il file NON e' stato riscritto da questa chiamata; {where}: {line}")
    _queue_entry(entry)


def reconcile_unrecorded(path=None):
    """Riporta in costs.json le spese pagate e non registrate (v8.4, N3), sotto il blocco, senza doppioni. Ritorna quante. Se costs.json non si legge o non si scrive solleva
    l'errore e le voci restano dove sono (continuano a contare). Si lancia a mano dopo aver ripristinato un permesso; `ledger_append` lo fa da solo alla prossima scrittura riuscita."""
    return _write_local(Path(path or LEDGER), None)


LOCK_TICKET_LIVE_S = 2.0       # un biglietto d'attesa non rinnovato da piu' di cosi' e' di un processo morto o fermo: gli altri lo scavalcano
_TICKET_SEQ = 0


def _ticket_new(lock):
    """Biglietto d'attesa (v8, M6): file vuoto `<blocco>.w<ns>-<pid>-<n>`, creato solo quando c'e' contesa. Chi ha il biglietto piu' vecchio (e vivo)
    prova per primo: ordine d'arrivo al posto della fortuna dello scheduler (con tanti scrittori a polling, chi rientrava subito dopo il rilascio
    lasciava gli altri a secco oltre i 30 s: `registro costi occupato`). NON e' un blocco: se il biglietto non si crea si prosegue come prima."""
    global _TICKET_SEQ
    _TICKET_SEQ += 1
    t = lock.with_name(f"{lock.name}.w{time.time_ns():020d}-{os.getpid()}-{_TICKET_SEQ}")
    try:
        os.close(os.open(str(t), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return t
    except OSError:
        return None


def _ticket_blocked(lock, ticket):
    """True se davanti a noi c'e' un biglietto VIVO (senza biglietto nostro: un biglietto vivo qualsiasi). Biglietti vecchi di oltre LOCK_STALE_S si
    tolgono; anche quelli con la data nel FUTURO oltre LOCK_TICKET_LIVE_S (v8.1: eta' negativa = non valido). Se la cartella non si riesce a leggere:
    False (si prosegue come prima)."""
    mine = ticket.name if ticket is not None else None
    try:
        now = time.time()
        for e in os.scandir(str(lock.parent)):
            if not e.name.startswith(lock.name + ".w") or (mine is not None and e.name >= mine):
                continue
            try:
                age = now - e.stat().st_mtime
            except OSError:
                continue
            if age < -LOCK_TICKET_LIVE_S or age > LOCK_STALE_S:
                # v8.1 (M-a): un'eta' NEGATIVA oltre la tolleranza (data del biglietto nel futuro: orologio tornato indietro, file con data sfalsata) non e' di un
                # processo vivo, che rinnova il biglietto con la data di ADESSO: vale come scaduto (prima restava 'vivo' per sempre e la scrittura falliva dopo 30 s).
                try:
                    os.unlink(e.path)
                except OSError:
                    pass
                continue
            if age <= LOCK_TICKET_LIVE_S:
                return True
    except OSError:
        return False
    return False


@contextmanager
def _ledger_lock(path):
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + LOCK_TIMEOUT_S
    fd, ticket = None, None
    try:
        while fd is None:
            turn = True
            if _ticket_blocked(lock, ticket):             # c'e' qualcuno in attesa da prima di noi: si fa la fila, non si salta
                turn = False
                if ticket is None:
                    ticket = _ticket_new(lock)
            if turn:
                try:
                    fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode())
                    break
                except (FileExistsError, PermissionError):     # su Windows un blocco in cancellazione da' PermissionError
                    if ticket is None:
                        ticket = _ticket_new(lock)         # prima contesa: ci si mette in fila
                    try:
                        if time.time() - lock.stat().st_mtime > LOCK_STALE_S:     # blocco rimasto da un processo morto
                            lock.unlink()
                            continue
                    except OSError:
                        pass
            if time.time() > deadline:
                raise SpendGateError(f"registro costi occupato da oltre {LOCK_TIMEOUT_S:.0f} s ({lock.name}): "
                                     f"nessuna scrittura fatta")
            if ticket is not None:
                try:
                    os.utime(str(ticket))                     # biglietto rinnovato: siamo vivi
                except OSError:
                    pass
            time.sleep(0.01 if turn else 0.05)
    finally:
        if ticket is not None:
            try:
                ticket.unlink()
            except OSError:
                pass
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except OSError:
            pass


def read_ledger(path=None):
    """Lista delle voci. File assente = lista vuota. File rovinato = LedgerError (mai una lista vuota in silenzio)."""
    path = Path(path or LEDGER)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:                 # v8.3 (N2): SOLO "il file non c'e'" vale assente (lista vuota). Mai Path.exists(): inghiotte 'accesso negato'
        return []
    except OSError as e:                      # permesso negato, cartella al posto del file, disco, antivirus: NON e' assente e NON e' rovinato
        raise LedgerUnavailable(f"registro costi NON accessibile ({path.name}, {type(e).__name__}): {str(e)[:160]}. Cancello CHIUSO: il file non viene letto ne' riscritto")
    except ValueError as e:                   # testo non UTF-8: rovinato (come sempre)
        raise LedgerError(f"registro costi illeggibile ({path.name}): {e}")
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise LedgerError(f"registro costi illeggibile ({path.name}): {e}")
    if not isinstance(data, list):
        raise LedgerError(f"registro costi con formato inatteso ({path.name}): non e' una lista")
    return data


def _atomic_write(path, entries):
    tmp = Path(str(path) + f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        for attempt in range(10):                       # OneDrive o antivirus possono tenere il file per un attimo
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.2)
        os.replace(tmp, path)
    except BaseException:                           # v8.3 (regola Ops 23): il proprio .tmp non resta dopo un errore (anche Ctrl-C); l'errore SALE
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def ledger_append(entry, path=None):
    """Aggiunge una voce al registro con blocco e scrittura atomica. Una voce senza 'created' riceve l'ora UTC."""
    path = Path(path or LEDGER)
    entry = dict(entry)
    entry.setdefault("created", datetime.datetime.now(datetime.timezone.utc).isoformat())
    try:
        led = _shared_for(path)
    except LedgerError:                       # registro condiviso non costruibile: la spesa pagata NON deve sparire
        print(f"[spend_gate] SPESA NON REGISTRATA su Notion (registro non disponibile, voce in coda): "
              f"{json.dumps(entry, ensure_ascii=False, default=str)[:400]}", file=sys.stderr)
        _queue_entry(entry)
        raise
    if led is not None:                       # registro condiviso: la spesa va su Notion, non in un costs.json locale
        import cost_ledger
        try:
            cost_ledger.record_legacy_entry(led, entry)
        except Exception as e:                # noqa: BLE001 - qualunque cosa: traccia sul terminale, coda (gia' fatta dal modulo) ed errore
            print(f"[spend_gate] SPESA NON REGISTRATA su Notion (in coda in generations/costs-pending.jsonl): "
                  f"{json.dumps(entry, ensure_ascii=False, default=str)[:400]}", file=sys.stderr)
            raise LedgerError(f"voce NON registrata su Notion ({type(e).__name__}): {str(e)[:260]}")
        return
    done = []
    try:
        _write_local(path, entry, done)
    except BaseException as e:                # v8.4 (N3): la spesa e' GIA' pagata. QUALUNQUE errore (blocco scaduto, permesso negato, rinomina fallita, file illeggibile, Ctrl-C)
        if not done:
            _keep_paid_entry(path, entry, e)  # mette la voce dove i cancelli la vedono (file 'non registrata' + coda + messaggio con l'importo) e RIALZA l'errore
            raise
        # v8.5 (S11): la scrittura in costs.json era GIA' riuscita (os.replace fatto): l'errore e' arrivato dopo (messaggio su stderr, rilascio del blocco, Ctrl-C). La voce e' registrata:
        # nessun doppione in 'non registrate' e in coda e, per un errore normale, nessun errore al chiamante (vedeva un fallimento su una voce salva). Ctrl-C / SystemExit si rialzano sempre.
        _say(f"[spend_gate] ATTENZIONE: errore DOPO la scrittura in {path.name} ({type(e).__name__}: {str(e)[:120]}): la voce e' gia' registrata, nessuna copia in 'non registrate' ne' in coda")
        if not isinstance(e, Exception):
            raise
    if os.environ.get("COST_LEDGER_SHADOW") == "1" and path.resolve() == LEDGER.resolve():      # fase 1 (solo il registro di default, non i percorsi di prova): copia best-effort nel registro condiviso, senza mai bloccare
        attempted = False
        try:
            t0 = time.monotonic()
            import cost_ledger
            shadow = _build_shared()
            if not hasattr(shadow, "time_budget"):
                raise RuntimeError("cost_ledger.py troppo vecchio: serve la v5 o successiva (da installare PRIMA o INSIEME a questo spend_gate)")
            try:
                budget = float(os.environ.get("COST_LEDGER_SHADOW_BUDGET_S", "12"))
            except ValueError:
                budget = 12.0
            if not (0 < budget < 3600):                  # nan, inf, zero, negativi, enormi: si usa il default (un nan disattiverebbe il budget)
                budget = 12.0
            remaining = budget - (time.monotonic() - t0)         # anche la preparazione (chiave, tetti) conta nel tempo massimo
            # v8 (M3): il budget limita la RETE; dopo la sua scadenza resta del lavoro su disco (la voce in coda, i resti rimessi in coda, i file in rinvio
            # tolti: sotto carico fino a ~1,4 s oltre i 12). Si tiene da parte una quota per quel lavoro: il tempo totale resta dentro il budget dichiarato.
            remaining -= min(1.0, remaining * 0.1)
            if remaining <= 0.2:
                raise RuntimeError("tempo massimo esaurito dalla preparazione")
            with shadow.time_budget(remaining):          # TEMPO MASSIMO TOTALE della copia in ombra (rinvii + scrittura): mai fermare il run a lungo
                try:
                    shadow.flush_spool(max_items=2)      # prima si svuotano al massimo 2 voci rimaste in coda (Notion tornato su)
                except Exception:                        # noqa: BLE001
                    pass
                attempted = True
                cost_ledger.record_legacy_entry(shadow, entry)       # tempo scaduto = la voce resta in coda (la mette il modulo) e si avvisa
        except Exception as e:                           # noqa: BLE001
            print(f"[spend_gate] copia in ombra su Notion non scritta: {str(e)[:120]}", file=sys.stderr)
            if not attempted:
                _queue_entry(entry)                      # registro non costruibile / cost_ledger vecchio / tempo finito prima di scrivere: la voce resta in coda


def spent_today_usd(now=None, path=None):
    """Somma dei costi del giorno (ora locale). Stessa regola dell'hook spend-guard."""
    led = _shared_for(path)
    if led is not None:                       # registro condiviso (prenotato + confermato, giorno di ROMA calcolato dentro cost_ledger)
        try:
            return led.totals(day="today", now=now)         # MAI .date() dell'orologio locale: nel cloud e' UTC
        except Exception as e:                # noqa: BLE001
            raise LedgerError(f"registro condiviso illeggibile: {str(e)[:260]}")
    day = (now or datetime.datetime.now().astimezone()).date()
    total = 0.0
    for e in _gate_entries(path):                 # v8.4 (N3): costs.json + spese pagate e non registrate (file '.unrecorded-')
        try:
            if datetime.datetime.fromisoformat(str(e.get("created"))).astimezone().date() == day:
                total += entry_cost_usd(e)            # v8.5: NaN / infinito / negativo / non numerico = LedgerError (nega); LedgerError non e' ValueError: sale
        except (ValueError, TypeError):
            continue
    return round(total, 4)


def daily_gate(quote_total, retry, now=None, path=None):
    """None se la spesa di questo run sta nel tetto giornaliero; altrimenti SpendGateError.
    retry=True: tentativo ripetuto (tetto 10 USD); False: prima generazione (tetto 8 USD)."""
    if quote_total <= 0:
        return None
    cap = DAILY_CAP_USD if retry else NEW_GEN_CAP_USD
    today = spent_today_usd(now, path)          # LedgerError (fail closed) se il registro e' rovinato
    if today + quote_total > cap + 1e-9:
        raise SpendGateError(
            f"DAILY CAP EXCEEDED: gia' spesi oggi ${today:.2f} + ${quote_total:.2f} di questo run > tetto giornaliero "
            f"${cap:.2f} ({'tentativo ripetuto' if retry else 'prima generazione: 2 USD di riserva per i tentativi'}). "
            f"Nessuna chiamata eseguita. Serve la decisione di Cipriano: --allow-over-cap solo se approva l'eccezione.")
    return None


def lab_limits(path=None):
    """(mensile, per prova, avvisi) da Sistema/lab/budget.yaml. File ASSENTE: i valori decisi da Cipriano (50, 5), come sempre. v8.3: un file che esiste ma non si legge NEGA. v8.4: NEGA
    (SpendGateError) anche se il file non e' YAML valido, non e' una mappa o ha valori non numerici/negativi/booleani (prima: valori di default, piu' larghi se Cipriano ha abbassato il
    budget nel file; e un YAML sintatticamente rotto sollevava `yaml.ParserError`, che non e' una SpendGateError e arrivava a `vp_common` come traceback)."""
    monthly, per_test, alerts = LAB_MONTHLY_USD, LAB_PER_TEST_USD, LAB_ALERTS
    try:
        text = Path(path or LAB_BUDGET_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return monthly, per_test, alerts          # budget.yaml assente: i valori decisi da Cipriano (50, 5)
    except (OSError, ValueError) as e:            # esiste ma non si legge (permesso negato, cartella al posto del file, non UTF-8...): NON si ripiega sul default
        raise SpendGateError(f"budget del lab non leggibile ({type(e).__name__}): cancello CHIUSO, nessuna chiamata eseguita")
    try:
        import yaml
    except ImportError:
        raise SpendGateError("budget del lab: il file esiste ma PyYAML non e' installato, quindi non si puo' leggere: cancello CHIUSO, nessuna chiamata eseguita")

    def num(v, name):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f"{name} deve essere un numero finito non negativo")
        return float(v)
    try:
        top = yaml.compose(text, Loader=yaml.SafeLoader)           # v8.5: l'albero, prima dei valori: una chiave scritta DUE volte NEGA (safe_load prendeva l'ULTIMA, cost_ledger.load_policy la PRIMA)
        if isinstance(top, yaml.MappingNode):
            names = [k.value for k, _ in top.value if isinstance(k, yaml.ScalarNode)]
            dup = sorted({n for n in names if names.count(n) > 1})
            if dup:
                raise ValueError("chiave scritta piu' di una volta: " + ", ".join(dup)[:60])
        d = yaml.safe_load(text)
        if d is None:
            d = {}
        if not isinstance(d, dict):
            raise ValueError("il file non e' una mappa")
        monthly = num(d.get("lab_monthly_usd", monthly), "lab_monthly_usd")
        per_test = num(d.get("per_test_usd", per_test), "per_test_usd")
        raw = d.get("alerts", alerts)
        if not isinstance(raw, (list, tuple)):
            raise ValueError("alerts deve essere una lista")
        alerts = tuple(num(x, "alerts") for x in raw)
        if any(not 0 < x <= 1 for x in alerts):
            raise ValueError("alerts: ogni soglia deve essere tra 0 (escluso) e 1")
    except (yaml.YAMLError, ValueError, TypeError) as e:
        raise SpendGateError(f"budget del lab NON valido ({type(e).__name__}: {str(e)[:100]}): cancello CHIUSO, nessuna chiamata eseguita. Non si ripiega sui valori di default")
    return monthly, per_test, alerts


def lab_month_spent_usd(now=None, path=None):
    """Somma dei costi del mese di calendario (ora locale) delle voci con project = "Lab"."""
    ref = (now or datetime.datetime.now().astimezone())
    led = _shared_for(path)
    if led is not None:
        try:
            return led.totals(month="this", business="Sistema/Lab", now=ref)         # mese di ROMA di `ref`, non della macchina
        except Exception as e:                # noqa: BLE001
            raise LedgerError(f"registro condiviso illeggibile: {str(e)[:260]}")
    total = 0.0
    for e in _gate_entries(path):                 # v8.4 (N3): costs.json + spese pagate e non registrate
        if str(e.get("project")) != LAB_PROJECT:
            continue
        try:
            d = datetime.datetime.fromisoformat(str(e.get("created"))).astimezone()
            if (d.year, d.month) == (ref.year, ref.month):
                total += entry_cost_usd(e)            # v8.5: come spent_today_usd
        except (ValueError, TypeError):
            continue
    return round(total, 4)


def lab_gate(quote_total, now=None, path=None, budget_path=None):
    """Budget del lab. Ritorna {'spent','after','monthly','alerts':[soglie superate dopo la spesa]}; alza SpendGateError se
    la prova supera i 5 USD o il mese supera i 50 USD (serve l'ok esplicito di Cipriano nel piano). Registro illeggibile = errore."""
    monthly, per_test, alerts = lab_limits(budget_path)
    if quote_total <= 0:
        spent = lab_month_spent_usd(now, path)
        return {"spent": spent, "after": spent, "monthly": monthly, "alerts": []}
    if quote_total > per_test + 1e-9:
        raise SpendGateError(f"LAB PER-TEST CAP EXCEEDED: prova da ${quote_total:.2f} > tetto per prova ${per_test:.2f}. Nessuna chiamata eseguita.")
    spent = lab_month_spent_usd(now, path)
    if spent + quote_total > monthly + 1e-9:
        raise SpendGateError(f"LAB MONTHLY BUDGET EXCEEDED: gia' spesi ${spent:.2f} questo mese + ${quote_total:.2f} > budget del lab ${monthly:.2f}. "
                             f"Nessuna chiamata eseguita. Serve l'ok esplicito di Cipriano nel piano.")
    after = spent + quote_total
    return {"spent": spent, "after": round(after, 4), "monthly": monthly, "alerts": [a for a in alerts if after >= a * monthly - 1e-9 and spent < a * monthly - 1e-9]}


# --- registro condiviso: spesa per contenuto e prenotazione per clip (punto 58) --------------------------------------
def content_spent_usd(content_key):
    """Spesa (prenotata + confermata) di un contenuto dal registro condiviso; None se il backend e' locale."""
    led = _shared()
    if led is None:
        return None
    try:
        return led.totals(content_key=content_key)
    except Exception as e:                    # noqa: BLE001
        raise LedgerError(f"registro condiviso illeggibile: {str(e)[:260]}")


def reserve_clip(*, amount_usd, project, content_key, asset, model, provider, scheda_url="", retry=None, allow_over_cap=False):
    """Prenota PRIMA di chiamare il fornitore. Ritorna la run key; None se il backend e' locale (o importo 0): nessuna prenotazione.
    Tetto superato o registro non affidabile = SpendGateError / LedgerError: NON si spende.
    retry: True/False deciso UNA volta per run dal chiamante (come approval_gate); None = automatico."""
    led = _shared()
    if led is None or not amount_usd or amount_usd <= 0:
        return None
    import cost_ledger
    try:
        return led.reserve(amount_usd=amount_usd, business=cost_ledger.norm_business(project) or cost_ledger.TEST_BUSINESS,
                           content_key=content_key, asset=asset, tipo="Video", model=str(model or ""),
                           provider=cost_ledger.fornitore_of(provider), scheda_url=scheda_url, retry=retry, allow_over_cap=allow_over_cap)
    except cost_ledger.CapExceeded as e:
        raise SpendGateError(str(e))
    except cost_ledger.LedgerError as e:
        raise LedgerError(str(e)[:200])
    except Exception as e:                    # noqa: BLE001 - mai un'eccezione "nuda" prima di una spesa: si nega
        raise LedgerError(f"errore imprevisto nel registro condiviso ({type(e).__name__}): {str(e)[:260]}")


def confirm_clip(run_key, actual_usd):
    """Dopo la generazione: costo reale. Non solleva (il clip e' gia' pagato e salvato): se Notion non risponde la conferma resta
    in coda e la riga resta 'Prenotato' (conta: prudente)."""
    if run_key is None:
        return
    try:
        _shared().confirm(run_key, actual_usd)
    except Exception as e:                    # noqa: BLE001
        print(f"[spend_gate] ATTENZIONE: conferma del costo ${actual_usd} non scritta (run key {run_key}): {str(e)[:120]}", file=sys.stderr)


def release_clip(run_key, reason):
    """Nessuna spesa avvenuta: la prenotazione non conta piu'. Non solleva (vedi confirm_clip)."""
    if run_key is None:
        return
    try:
        _shared().release(run_key, reason)
    except Exception as e:                    # noqa: BLE001
        print(f"[spend_gate] ATTENZIONE: prenotazione {run_key} non rilasciata: {str(e)[:120]}", file=sys.stderr)
