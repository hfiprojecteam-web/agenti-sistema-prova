"""
cost_ledger.py - REGISTRO COSTI CONDIVISO PC/cloud (punto 58, 20/10/2026). STAGING v8 (22/10/2026): non installato in produzione.

Interfaccia (vedi DESIGN.md):
    run_key = reserve(amount_usd=..., business=..., content_key=..., asset=..., ...)   # PRIMA di spendere
    confirm(run_key, actual_usd)                                                      # DOPO, con il costo reale
    release(run_key, reason)                                                          # se non si e' speso nulla
    totals(day=..., month=..., content_key=..., scheda=..., business=..., group_by=...)

Due backend:
    'local'  - generations/costs.json di oggi (compatibilita': le voci vecchie restano valide, i lettori vecchi
               continuano a sommare cost_usd; le voci rilasciate valgono 0)
    'notion' - database "Registro costi" su Notion via api.notion.com (solo libreria standard, nessun requests).
               Se NOTION_TOKEN esiste lo usa; altrimenti manda la richiesta SENZA Authorization (modalita' proxy del
               cloud: la chiave la aggiunge il proxy dell'ambiente) e lo scrive nel log. Nessuna chiave nel codice, nei log
               o nei messaggi di errore (redazione di token e di sequenze secret_/ntn_/Bearer).

FAIL CLOSED: se il registro non risponde, risponde in modo strano o e' incompleto, reserve() solleva LedgerError
e NON si spende. I tetti sono gli stessi di spend_gate.py / spend-guard.py (valori letti da budget.yaml e models.yaml).
Python >= 3.9, solo libreria standard (PyYAML opzionale).
"""
import datetime
import http.client
import json
import logging
import math
import os
import random
import re
import stat as stat_mod
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("cost_ledger")
__version__ = "8"            # live_check.py e le patch di spend_gate richiedono >= 5; v7: scrittori senza blocco (file unico per voce), blocco del SO per la presa;
#                              v8: controllo "coda vuota" SOTTO il blocco (D-A), .tmp propri mai lasciati (M1), righe divise solo su a-capo (M2), coda fuori da OneDrive (M4)
__revision__ = "8.6"         # il NUMERO di versione resta 8 (live_check.py fa int(__version__)). 8.6: `LocalBackend` legge `cost_usd` con la STESSA regola di `spend_gate.entry_cost_usd` (`[]`, `{}` e `true` NEGANO; una voce che non e' un oggetto NEGA). 8.5: `load_policy` NEGA se una chiave dei tetti e' scritta DUE volte (prima la PRIMA; `spend_gate.lab_limits` prendeva l'ULTIMA); `LocalBackend` rifiuta un `cost_usd` negativo o troppo grande (prima abbassava i totali). 8.4: `load_policy` NEGA (LedgerUnavailable) se budget.yaml/models.yaml esistono ma non si leggono o non sono validi (prima: default, piu' larghi se Cipriano abbassa un tetto nel file); assenti = default come sempre. 8.3: `spool-archive` senza file da spostare non crea nulla (m-b); `LocalBackend._read` senza `Path.exists()` (N2, come spend_gate.read_ledger). 8.2: elenchi della coda con os.scandir (una cartella non elencabile NEGA, N1), inventari onesti. 8.1: la VECCHIA posizione della coda passa dalla stessa funzione sotto blocco
#                              della principale (G1), eta' negative di file in rinvio e .tmp = scaduti (M-a, regola Ops 32)

# ----------------------------------------------------------------------------- costanti

STATE_RESERVED, STATE_CONFIRMED, STATE_CANCELLED, STATE_FAILED = "Prenotato", "Confermato", "Annullato", "Fallito"
ACTIVE_STATES = (STATE_RESERVED, STATE_CONFIRMED)          # contano nei totali
RELEASE_STATES = (STATE_CANCELLED, STATE_FAILED)
BUSINESSES = ("Life Wisdom", "Cavemen AI", "PCS", "Self-Publishing", "Sistema/Lab", "Test")      # "Test" aggiunta da Cipriano il 20/10
LAB_BUSINESS = "Sistema/Lab"
TEST_BUSINESS = "Test"      # prove e progetti di prova: contano nei tetti giornalieri ma NON nel budget del lab
TIPI = ("Video", "Immagine", "Voce", "Chat/testo", "Altro")
FORNITORI = ("kie.ai", "fal.ai", "Higgsfield", "ElevenLabs", "Altro")
ORIGINI = ("PC", "Cloud", "Sessione")
# Il costs.json di oggi chiama "Lab" il progetto del lab (spend_gate.LAB_PROJECT): mappa nei due sensi.
PROJECT_FROM_BUSINESS = {"Life Wisdom": "Life Wisdom", "Cavemen AI": "cavemenai", "PCS": "PCS",
                         "Self-Publishing": "Self-Publishing", "Sistema/Lab": "Lab", "Test": "test"}

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION_DATABASE = "2022-06-28"      # come notion_client.py; query su /databases/{id}/query (serve l'ID del DATABASE)
NOTION_VERSION_DATA_SOURCE = "2025-09-03"   # query su /data_sources/{id}/query (serve l'ID del DATA SOURCE, come nei tool MCP)
TEST_DATA_SOURCE_ID = "6068f89a-f791-4242-97a1-25d890226041"   # "Registro costi (database di prova)"; identificativo, non e' un segreto

DAILY_CAP_USD = 10.0     # uguale a spend_gate.DAILY_CAP_USD e spend-guard.DAILY_CAP_USD
NEW_GEN_CAP_USD = 8.0    # prima generazione: 20% di riserva per i tentativi
EPS = 1e-9


class LedgerError(Exception):
    """Base: il registro non puo' garantire il tetto. Chi spende deve trattarla come 'NO'."""


class LedgerUnavailable(LedgerError):
    """Registro non raggiungibile / rifiuta la richiesta / limite di tentativi esaurito."""


class BadResponse(LedgerError):
    """Il registro risponde ma in modo inatteso o incompleto (non si indovina: si nega)."""


class UnknownRunKey(LedgerError):
    """La prenotazione non si trova (puo' essere l'indice di Notion in ritardo). Per un costo gia' pagato NON e' un errore definitivo:
    l'operazione va in coda e il cancello nega finche' non si risolve."""


class CapExceeded(LedgerError):
    def __init__(self, message, cap_name="", spent=0.0, requested=0.0, cap=0.0, race=False):
        super().__init__(message)
        self.cap_name, self.spent, self.requested, self.cap, self.race = cap_name, spent, requested, cap, race


# ----------------------------------------------------------------------------- sicurezza dei messaggi

_SECRET_RE = re.compile(r"(secret_[A-Za-z0-9_\-]{6,}|ntn_[A-Za-z0-9_\-]{6,}|(?i:bearer)\s+[A-Za-z0-9._\-]{6,}|(?i:key)\s+[A-Za-z0-9._\-]{16,})")


def redact(text, token=None):
    """Toglie da un testo il token noto e le sequenze che somigliano a chiavi. Usata su ogni messaggio d'errore e di log."""
    s = str(text)
    if token:
        s = s.replace(token, "***")
    return _SECRET_RE.sub("***", s)


# ----------------------------------------------------------------------------- policy (tetti)

def find_workspace(start=None):
    """Radice del workspace: la prima cartella antenata che contiene 'Sistema'. None se non trovata (es. cloud senza struttura)."""
    env = os.environ.get("COST_LEDGER_WORKSPACE")
    if env:
        return Path(env)
    here = Path(start or __file__).resolve()
    for p in [here] + list(here.parents):
        if (p / "Sistema").is_dir():
            return p
    return None


@dataclass
class Policy:
    content_cap_usd: float = 5.0          # models.yaml: budget_cap_per_content_usd (tetto assoluto per contenuto, retry inclusi)
    daily_cap_usd: float = DAILY_CAP_USD
    new_gen_cap_usd: float = NEW_GEN_CAP_USD
    lab_monthly_usd: float = 50.0         # Sistema/lab/budget.yaml
    lab_per_test_usd: float = 5.0
    lab_alerts: tuple = (0.5, 0.8)
    source: str = "default"


def _policy_text(p):
    """Testo di un file dei tetti. None = il file NON c'e' (valgono i default decisi da Cipriano, come sempre). Un file che ESISTE ma non si legge (permesso negato, una cartella
    al posto del file, disco, testo non UTF-8) e' LedgerUnavailable: v8.4 (regola Ops 33/34). Prima: `is_file()` + `except (OSError, ValueError): pass` -> valori di default, che sono
    PIU' LARGHI del file se Cipriano ha abbassato un tetto nel file (models.yaml a 3.0 con permesso negato dava 5.0; budget.yaml a 20/2 dava 50/5)."""
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise LedgerUnavailable(f"file dei tetti NON leggibile ({p.name}, {type(e).__name__}): cancello CHIUSO. Non si ripiega sui valori di default "
                                f"(potrebbero essere piu' larghi di quelli scritti nel file). Ripristinare l'accesso al file.")


def _policy_number(txt, key, fname):
    """Valore numerico di `key:` in un file dei tetti. None se la chiave non c'e' (resta il default). La chiave c'e' ma il valore non e' un numero
    (`50abc`, `"50"`, `-5`, `abc`, `1.2.3`): LedgerUnavailable (v8.4: prima si ignorava in silenzio e restava il default, magari piu' largo)."""
    n = len(re.findall(rf"^{key}:", txt, re.M))
    if n == 0:
        return None
    if n > 1:                             # v8.5: scritta DUE volte (es. `lab_monthly_usd: 100` poi `20`): prima si prendeva la PRIMA, lab_limits (YAML) l'ULTIMA. Non si indovina: NEGA
        raise LedgerUnavailable(f"file dei tetti NON valido ({fname}): '{key}' e' scritta {n} volte. Cancello CHIUSO (non si sceglie tra i valori: lasciarne una sola).")
    m = re.search(rf"^{key}:[ \t]*([0-9]+(?:\.[0-9]+)?|\.[0-9]+)[ \t\r]*(?:#.*)?$", txt, re.M)
    if not m:
        raise LedgerUnavailable(f"file dei tetti NON valido ({fname}): '{key}' non e' un numero. Cancello CHIUSO (non si ripiega sui valori di default).")
    try:
        v = float(m.group(1))
    except ValueError:
        raise LedgerUnavailable(f"file dei tetti NON valido ({fname}): '{key}' non e' un numero. Cancello CHIUSO (non si ripiega sui valori di default).")
    if not math.isfinite(v) or v < 0:
        raise LedgerUnavailable(f"file dei tetti NON valido ({fname}): '{key}' = {v}. Cancello CHIUSO.")
    return v


def load_policy(workspace=None, budget_yaml=None, models_yaml=None):
    """Legge i valori VERI da Sistema/lab/budget.yaml e .claude/skills/video-bootcamp/models.yaml. File ASSENTE = i valori decisi da Cipriano (come sempre).
    v8.4: un file che esiste ma NON si legge (o non e' valido) NON ripiega sui default: LedgerUnavailable (come `spend_gate.lab_limits` dalla v8.3). Chi costruisce un
    registro (CostLedger) riceve l'errore: i cancelli del modo notion NEGANO con il messaggio, `selfcheck` lo riporta."""
    ws = Path(workspace) if workspace else find_workspace()
    pol = Policy()
    used = []
    b = Path(budget_yaml) if budget_yaml else (ws / "Sistema" / "lab" / "budget.yaml" if ws else None)
    m = Path(models_yaml) if models_yaml else (ws / ".claude" / "skills" / "video-bootcamp" / "models.yaml" if ws else None)
    txt = _policy_text(b) if b else None
    if txt is not None:
        for key, attr in (("lab_monthly_usd", "lab_monthly_usd"), ("per_test_usd", "lab_per_test_usd")):
            v = _policy_number(txt, key, b.name)
            if v is not None:
                setattr(pol, attr, v)
        if len(re.findall(r"^alerts:", txt, re.M)) > 1:       # v8.5: come per le altre chiavi
            raise LedgerUnavailable(f"file dei tetti NON valido ({b.name}): 'alerts' e' scritta piu' di una volta. Cancello CHIUSO (non si sceglie tra i valori: lasciarne una sola).")
        if re.search(r"^alerts:", txt, re.M):
            ma = re.search(r"^alerts:[ \t]*\[([^\]]*)\]", txt, re.M)
            try:
                alerts = tuple(float(x) for x in ma.group(1).split(",") if x.strip()) if ma else None
            except ValueError:
                alerts = None
            if alerts is None or not all(math.isfinite(a) and 0 < a <= 1 for a in alerts):
                raise LedgerUnavailable(f"file dei tetti NON valido ({b.name}): 'alerts' non e' una lista di numeri tra 0 e 1. Cancello CHIUSO.")
            pol.lab_alerts = alerts
        used.append(str(b.name))
    txt = _policy_text(m) if m else None
    if txt is not None:
        v = _policy_number(txt, "budget_cap_per_content_usd", m.name)
        if v is not None:
            pol.content_cap_usd = v
            used.append(str(m.name))
    pol.source = "+".join(used) if used else "default"
    return pol


# ----------------------------------------------------------------------------- utilita'

def _tz():
    """Fuso dei giorni/mesi del registro: Europe/Rome (Cipriano). Il cloud e' in UTC: il giorno si calcola SEMPRE qui."""
    name = os.environ.get("COST_LEDGER_TZ", "Europe/Rome")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as e:                               # noqa: BLE001
        # NESSUN ripiego sul fuso della macchina (nel cloud e' UTC: i giorni sarebbero sbagliati in silenzio). Si nega.
        raise LedgerUnavailable(f"fuso orario '{name}' non disponibile ({type(e).__name__}): manca tzdata. Nel cloud: `pip install tzdata` "
                                f"oppure il pacchetto di sistema tzdata; verifica con `python cost_ledger.py selfcheck`")


def parse_dt(s):
    """ISO 8601 -> datetime con fuso. Data senza ora = mezzanotte nel fuso del registro. Errore = ValueError."""
    s = str(s).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    d = datetime.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=_tz())


def _finite_nonneg(x, what):
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: non e' un numero ({x!r})")
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"{what}: deve essere un numero finito >= 0 ({x!r})")
    return v


def local_cost_usd(raw, idx):
    """v8.6: costo di UNA voce di costs.json letta da LocalBackend, con la STESSA regola di `spend_gate.entry_cost_usd` (regola Ops 38: due lettori dello stesso dato rispondono allo stesso
    modo sulle stesse stranezze). Assente, None, False, stringa vuota e 0 valgono 0.0; `true` NON e' un costo; altrimenti `float(valore)` deve essere FINITO e >= 0, altrimenti BadResponse
    (nega). Prima: `float(x or 0)`: una lista o una mappa VUOTE valevano 0 e `true` 1 USD, mentre spend_gate le negava (il test `test_v86_patched` confronta i due su ogni valore)."""
    if raw is None or raw is False or raw == "" or raw == 0:
        return 0.0
    if raw is True:
        raise BadResponse(f"registro locale: cost_usd booleano (true) alla voce {idx}")
    try:
        amount = float(raw)
    except (ValueError, TypeError, OverflowError):
        raise BadResponse(f"registro locale: cost_usd non numerico alla voce {idx}")
    if not math.isfinite(amount):
        raise BadResponse(f"registro locale: cost_usd non finito alla voce {idx}")
    if amount < 0:                    # v8.5: un negativo abbassava i totali (7,5 - 5 = 2,5); le voci rilasciate valgono 0.0, mai meno
        raise BadResponse(f"registro locale: cost_usd negativo alla voce {idx}")
    return amount


def _asset_of(run_key):
    return run_key.rsplit("~", 1)[0]


def norm_business(project):
    """Nome progetto di costs.json -> select Business. None se non riconosciuto (la migrazione lo segnala, mai indovinare)."""
    p = re.sub(r"[^a-z0-9]", "", str(project or "").lower())
    table = {"lifewisdom": "Life Wisdom", "cavemenai": "Cavemen AI", "pcs": "PCS", "selfpublishing": "Self-Publishing",
             "cover": "Self-Publishing", "lab": LAB_BUSINESS, "sistemalab": LAB_BUSINESS}
    if p in table:
        return table[p]
    if "test" in p:                       # test, channel-test, Test architettura, ...: prove, NON budget del lab (decisione di Cipriano 20/10)
        return TEST_BUSINESS
    return None


def _select(value, allowed, default="Altro"):
    return value if value in allowed else default


def new_run_key(content_key, asset, now):
    prefix = f"{content_key}/" if content_key else ""
    stamp = now.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}{asset or 'item'}~{stamp}-{random.getrandbits(24):06x}"


def usd_from_kie_credits(credits):
    """Costo reale da crediti kie: 1 credito = 0.005 USD (01_generate.CREDIT_USD). Crediti assenti -> None (usare la stima, dichiarandolo)."""
    if credits in (None, ""):
        return None
    return round(float(credits) * 0.005, 4)


# ----------------------------------------------------------------------------- coda: file unico per voce, blocco del SO per la presa

def _env_float(name, default):
    try:
        v = float(os.environ.get(name, default))
        return v if math.isfinite(v) and v > 0 else default
    except (TypeError, ValueError):
        return default


QUEUE_LOCK_TIMEOUT_S = _env_float("COST_LEDGER_QUEUE_LOCK_TIMEOUT_S", 10.0)   # attesa massima del blocco di PRESA (mai chi scrive); poi si NEGA
GATE_FLUSH_MAX_S = _env_float("COST_LEDGER_GATE_FLUSH_MAX_S", 45.0)           # v8: tempo massimo del rinvio fatto da un CANCELLO (poi nega; il lavoro fatto resta fatto)
ORPHAN_TMP_AGE_S = _env_float("COST_LEDGER_ORPHAN_TMP_AGE_S", 60.0)           # un .tmp piu' vecchio e' di un processo morto tra scrittura e rinomina


def _age_expired(age, limit):
    """Regola Ops 32 (v8.1): un'eta' calcolata da una DATA di file (o dall'istante scritto nel nome) e' SCADUTA anche se NEGATIVA oltre la tolleranza. Una data nel
    futuro (orologio tornato indietro, file con data sfalsata, portatile che si risveglia) non puo' essere di un processo vivo che lavora da meno di `limit`:
    prima l'eta' negativa risultava 'recente' per tutto il tempo del salto (un file in rinvio con la presa nel futuro di un'ora chiudeva il cancello per un'ora).
    Tolleranza = `limit` stesso: una data nel futuro fino a `limit` (salto piccolo, rinvio forse ancora vivo) resta 'recente', oltre e' scaduta."""
    return age > limit or age < -limit


def _try_lock(fd):
    """Prova a prendere il blocco del sistema operativo sul file `fd` senza attendere. True/False; OSError solo per un file system che i blocchi
    non li supporta (si NEGA: fail closed)."""
    if os.name == "nt":
        import msvcrt
        try:
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import errno
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        raise


def _unlock(fd):
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class _QueueLock:
    """Blocco tra PROCESSI per la PRESA (rinvio) della coda dei costi: un blocco del SISTEMA OPERATIVO (`LockFile` su Windows,
    `flock` su POSIX) su `<coda>.lock`. Lo rilascia il sistema operativo anche se il processo muore (kill -9): nessun blocco orfano,
    nessuna scadenza da stimare con l'orologio (un orologio che salta non puo' romperlo), nessun file da cancellare (un antivirus o OneDrive che
    tiene aperto il file non lo disturba). Il file `.lock` resta li' (vuoto) di proposito. Chi SCRIVE nella coda non lo usa mai e non lo
    aspetta mai (vedi `append_pending`): lo usa solo chi prende i file per rinviarli. L'attesa e' limitata (`timeout`, e dal budget dell'ombra
    quando c'e'); scaduta, solleva LedgerUnavailable e chi chiama NEGA (cancello) o salta il rinvio (ombra)."""

    def __init__(self, spool_path, timeout=None):
        self.path = Path(str(spool_path) + ".lock")
        self.timeout = QUEUE_LOCK_TIMEOUT_S if timeout is None else float(timeout)
        self.fd = None

    def __enter__(self):
        if self.timeout <= 0:
            raise LedgerUnavailable("tempo esaurito prima di poter prendere la coda dei costi (blocco non atteso)")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        except OSError as e:
            raise LedgerUnavailable(f"blocco della coda dei costi non apribile ({type(e).__name__}): cancello CHIUSO")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                got = _try_lock(fd)
            except OSError as e:
                os.close(fd)
                raise LedgerUnavailable(f"blocco del sistema non disponibile su questo file system ({type(e).__name__}): cancello CHIUSO")
            if got:
                self.fd = fd
                return self
            if time.monotonic() > deadline:
                os.close(fd)
                raise LedgerUnavailable(f"coda dei costi presa da un altro processo da oltre {self.timeout:.1f} s ({self.path.name}): "
                                        f"nessun rinvio ora, riprova")
            time.sleep(0.005 + random.random() * 0.015)

    def __exit__(self, *exc):
        if self.fd is not None:
            _unlock(self.fd)
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        return False


_ITEM_SEQ = 0


def _split_lines(text):
    """Righe di una coda. Si divide SOLO su `\\n` (e si toglie un `\\r` finale): `str.splitlines()` divide anche su U+2028, U+2029, U+0085, `\\v`, `\\f`,
    `\\x1c-\\x1e`, e una riga JSON con `ensure_ascii=False` puo' contenere U+2028/U+2029/U+0085 dentro una stringa (nome di asset, nota): la riga
    si spezzava in frammenti che non si rinviano mai (cancello chiuso per sempre, v5-v7). Le righe vuote o di soli spazi si scartano."""
    return [l for l in (x.rstrip("\r") for x in str(text).split("\n")) if l.strip()]


def _scan_names(directory, prefix=""):
    """Nomi dei file di `directory` che iniziano per `prefix`, elencati con `os.scandir` (regola Ops 33, v8.2). NON si usa `Path.glob`: su una cartella che non si
    puo' ELENCARE (permesso negato, ACL su Windows) `glob` restituisce una lista vuota SENZA errore e un elenco che autorizza una spesa vedrebbe 'coda vuota' con
    5 USD dentro (N1 della verifica della v8.1: gia' nella v7, provato con icacls). Qui ogni errore SALE come OSError e chi chiama NEGA. UNICA eccezione: la cartella
    che non ESISTE (FileNotFoundError all'apertura dell'elenco) = nessuna coda = lista vuota (e non viene creata). Un errore a META' dell'elenco sale sempre.
    Il confronto del prefisso segue il file system (senza distinzione di maiuscole su Windows)."""
    pre = os.path.normcase(prefix)
    try:
        it = os.scandir(str(directory))
    except FileNotFoundError:
        return []
    out = []
    with it:
        for e in it:
            if os.path.normcase(e.name).startswith(pre):
                out.append(e.name)
    return out


def _stat_or_none(path):
    """`os.stat` che dice solo 'non c'e'' (FileNotFoundError -> None). Ogni altro errore (accesso negato, un file al posto della cartella, nome non valido) SALE:
    `Path.exists()` e `Path.is_file()` li trasformano in False e un controllo 'coda vuota' che li usa fa passare il cancello (regola Ops 33)."""
    try:
        return os.stat(str(path))
    except FileNotFoundError:
        return None


def _discard(path):
    """Toglie un file temporaneo PROPRIO. Un `.tmp` completo lasciato li' e' un doppione in coda; uno troncato e' una riga illeggibile che
    chiude il cancello per sempre: chi lo ha creato lo toglie prima di riprovare. Ritorna True se non c'e' piu'."""
    for k in range(5):
        try:
            os.unlink(str(path))
            return True
        except FileNotFoundError:
            return True
        except OSError:                                        # antivirus/indicizzatore che lo tiene aperto per un istante
            time.sleep(0.02 * (k + 1))
    log.error("file temporaneo di coda NON rimosso (%s): Ops deve toglierlo, altrimenti puo' chiudere il cancello", Path(path).name)
    return False


def _write_item(spool_path, lines):
    """Scrive le righe in un file UNICO `<coda>.item-<pid>-<ns>-<n>-<casuale>`: prima in un `.tmp` (creazione esclusiva, scrittura completa,
    fsync) e poi rinominato sul nome definitivo (su Windows `os.rename` non sovrascrive; il nome e' comunque univoco e si controlla exists()).
    NESSUN blocco, nessuna attesa: chi scrive una voce GIA' PAGATA non dipende da nessun altro processo. Se un tentativo fallisce (scrittura,
    fsync, nome gia' presente, rinomina) il proprio `.tmp` si TOGLIE prima di riprovare (v8, M1): non restano ne' doppioni ne' file troncati.
    Ritorna il percorso."""
    global _ITEM_SEQ
    lines = [str(l).rstrip("\r\n") for l in lines if l is not None and str(l).strip()]
    if not lines:
        return None
    spool = Path(spool_path)
    data = "".join(l + "\n" for l in lines).encode("utf-8")
    spool.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in range(12):
        _ITEM_SEQ += 1
        name = f"{spool.name}.item-{os.getpid()}-{time.time_ns()}-{_ITEM_SEQ}-{random.getrandbits(24):06x}"
        final, tmp = spool.with_name(name), spool.with_name(name + ".tmp")
        made = False                                           # il .tmp e' MIO solo se l'open esclusivo e' riuscito
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
            made = True
            try:
                view = memoryview(data)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                os.fsync(fd)
            finally:
                os.close(fd)
            if final.exists():
                raise FileExistsError(f"nome gia' presente: {final.name}")
            os.rename(tmp, final)
            return final
        except FileExistsError as e:
            last = e
            if made:
                _discard(tmp)
        except OSError as e:                                   # un antivirus/indicizzatore puo' tenere occupato il file per un istante
            last = e
            if made:
                _discard(tmp)
            time.sleep(0.02 * (attempt + 1))
        except BaseException:                                  # interruzione (Ctrl-C...): niente file a meta'
            if made:
                _discard(tmp)
            raise
    raise OSError(f"voce non scrivibile nella coda ({type(last).__name__ if last else '?'})")


def append_pending(spool_path, lines):
    """Mette righe (testo JSON) nella coda dei costi in sospeso scrivendole in un file UNICO (vedi `_write_item`): nessun append su file
    condivisi, nessun blocco da attendere. E' l'unico modo ammesso di scrivere nella coda (spend_gate._queue_entry compreso). Il rinvio
    (CostLedger.flush_spool) legge anche i file v5/v6 gia' presenti (`<coda>` in append, `.flush*`)."""
    return _write_item(spool_path, lines)


# ----------------------------------------------------------------------------- record normalizzato

@dataclass
class Rec:
    run_key: str
    state: str
    amount_usd: float
    when: datetime.datetime
    business: str = ""
    tipo: str = "Altro"
    model: str = ""
    provider: str = "Altro"
    scheda: str = ""
    origin: str = "PC"
    note: str = ""
    ref: str = ""            # id pagina Notion / indice locale
    extra: dict = field(default_factory=dict)

    @property
    def asset(self):
        return _asset_of(self.run_key)


# ----------------------------------------------------------------------------- backend LOCAL (costs.json)

class LocalBackend:
    """costs.json di oggi. Stesso blocco e stessa scrittura atomica di spend_gate (file .lock), quindi i due non si pestano.
    Voci senza 'state' = Confermato. Una voce rilasciata ha cost_usd = 0.0, cosi' i lettori vecchi non la contano."""
    name = "local"

    def __init__(self, path=None, must_exist=True, lock_timeout_s=30.0, lock_stale_s=120.0):
        if path is None:
            env = os.environ.get("COST_LEDGER_LOCAL_PATH")
            ws = find_workspace()
            path = env or (ws / "Sistema" / "scripts" / "video-pipeline" / "generations" / "costs.json" if ws else None)
        if path is None:
            raise LedgerUnavailable("registro locale: percorso di costs.json non determinabile")
        self.path, self.must_exist = Path(path), must_exist
        self.lock_timeout_s, self.lock_stale_s = lock_timeout_s, lock_stale_s
        self.skipped = 0

    # -- file
    def _lock(self):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            lock = Path(str(self.path) + ".lock")
            lock.parent.mkdir(parents=True, exist_ok=True)
            deadline, fd = time.time() + self.lock_timeout_s, None
            while fd is None:
                try:
                    fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode())
                except (FileExistsError, PermissionError):
                    try:
                        if time.time() - lock.stat().st_mtime > self.lock_stale_s:
                            lock.unlink()
                            continue
                    except OSError:
                        pass
                    if time.time() > deadline:
                        raise LedgerUnavailable(f"registro locale occupato da oltre {self.lock_timeout_s:.0f} s: nessuna scrittura")
                    time.sleep(0.05)
            try:
                yield
            finally:
                os.close(fd)
                try:
                    lock.unlink()
                except OSError:
                    pass
        return cm()

    def _read(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:             # v8.3 (N2, stessa classe di spend_gate.read_ledger): SOLO "il file non c'e'" vale assente. `Path.exists()` inghiotte 'accesso negato':
            if self.must_exist:               # con must_exist=False un costs.json con permessi negati valeva '[]' e create() lo RISCRIVEVA con la sola voce nuova (voci perse)
                raise LedgerUnavailable(f"registro locale assente ({self.path.name}): fail closed (in un ambiente nuovo/cloud "
                                        f"questo e' il segnale che serve il backend 'notion', non una spesa a zero)")
            return []
        except (OSError, ValueError) as e:
            raise BadResponse(f"registro locale illeggibile ({self.path.name}): {redact(e)}")
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise BadResponse(f"registro locale illeggibile ({self.path.name}): {redact(e)}")
        if not isinstance(data, list):
            raise BadResponse(f"registro locale con formato inatteso ({self.path.name}): non e' una lista")
        return data

    def _write(self, entries):
        tmp = Path(str(self.path) + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        for _ in range(10):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                time.sleep(0.2)
        os.replace(tmp, self.path)

    # -- conversione
    def _to_rec(self, e, idx):
        if not isinstance(e, dict):       # v8.6: come spend_gate (voce che non e' un oggetto = nega con un messaggio, non un traceback)
            raise BadResponse(f"registro locale: la voce {idx} non e' un oggetto")
        try:
            when = parse_dt(e.get("created"))
        except (ValueError, TypeError):
            self.skipped += 1               # come spend_gate: voce senza data valida ignorata (segnalata nel contatore)
            return None
        state = e.get("state") or STATE_CONFIRMED
        amount = local_cost_usd(e.get("cost_usd", 0), idx)         # v8.6: UNA regola (come spend_gate.entry_cost_usd)
        business = e.get("business") or norm_business(e.get("project")) or ""
        rk = e.get("run_key") or f"legacy-{idx:05d}:{e.get('asset', '')}"
        return Rec(run_key=rk, state=state, amount_usd=amount, when=when, business=business,
                   tipo=e.get("tipo") or "Altro", model=str(e.get("model") or ""), provider=str(e.get("provider") or ""),
                   scheda=e.get("scheda") or "", origin=e.get("origin") or "PC", note=e.get("note") or "", ref=str(idx),
                   extra={"project": e.get("project"), "asset": e.get("asset")})

    @staticmethod
    def _to_entry(r):
        return {"project": PROJECT_FROM_BUSINESS.get(r.business, r.business), "asset": r.asset,
                "cost_usd": r.amount_usd if r.state in ACTIVE_STATES else 0.0, "provider": r.provider, "model": r.model,
                "created": r.when.isoformat(), "state": r.state, "run_key": r.run_key, "origin": r.origin,
                "business": r.business, "tipo": r.tipo, "scheda": r.scheda, "note": r.note}

    # -- interfaccia backend
    def create(self, r):
        with self._lock():
            entries = self._read()
            if any(e.get("run_key") == r.run_key for e in entries):
                raise BadResponse("run key gia' presente")
            entries.append(self._to_entry(r))
            self._write(entries)

    def find(self, run_key):
        for i, e in enumerate(self._read()):
            if e.get("run_key") == run_key:
                return self._to_rec(e, i)
        return None

    def update(self, run_key, state=None, amount_usd=None, note=None):
        with self._lock():
            entries = self._read()
            for e in entries:
                if e.get("run_key") == run_key:
                    st = state or e.get("state") or STATE_CONFIRMED
                    amt = local_cost_usd(e.get("cost_usd", 0), "?") if amount_usd is None else amount_usd      # v8.6: stessa regola
                    e["state"] = st
                    e["cost_usd"] = amt if st in ACTIVE_STATES else 0.0
                    if note is not None:
                        e["note"] = note
                    self._write(entries)
                    return
            raise UnknownRunKey("run key sconosciuta")

    def query(self, states=ACTIVE_STATES, since=None, until=None, prefix=None, business=None, scheda=None):
        out = []
        for i, e in enumerate(self._read()):
            r = self._to_rec(e, i)
            if r is None or r.state not in states:
                continue
            if since and r.when < since or until and r.when >= until:
                continue
            if prefix and not r.run_key.startswith(prefix) and not str(r.extra.get("asset") or "").startswith(prefix):
                continue
            if business and r.business != business:
                continue
            if scheda and r.scheda != scheda:
                continue
            out.append(r)
        return out


# ----------------------------------------------------------------------------- backend NOTION

def _urllib_transport(method, url, headers, body, timeout):
    """(status, headers, body_bytes). Gli errori HTTP tornano come risposta; gli errori di rete sono OSError.
    Il tempo `timeout` e' ASSOLUTO per l'intera richiesta (connessione, invio, lettura): il timeout di urllib vale per ogni singola
    operazione sul socket e un server che risponde un byte alla volta lo aggirerebbe. La richiesta gira in un thread di servizio: scaduto
    il tempo si abbandona (il thread muore da solo) e si solleva OSError. Per una scrittura l'esito resta 'ambiguo' (si verifica con find)."""
    box = {}

    def work():
        try:
            box["v"] = _urllib_request(method, url, headers, body, timeout)
        except BaseException as e:                       # noqa: BLE001 - riportata al thread chiamante
            box["e"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise OSError(f"rete: tempo massimo della richiesta ({timeout:.1f} s) esaurito (server bloccato o risposta a goccia)")
    if "e" in box:
        raise box["e"]
    return box["v"]


def _urllib_request(method, url, headers, body, timeout):
    try:
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers.items()), resp.read()
        except urllib.error.HTTPError as e:
            try:
                data = e.read() if e.fp else b""
            except (http.client.HTTPException, OSError):
                data = b""
            return e.code, dict(e.headers.items()) if e.headers else {}, data
        except urllib.error.URLError as e:
            raise OSError(f"rete: {type(e.reason).__name__ if not isinstance(e.reason, str) else e.reason}")
    except (http.client.HTTPException, ValueError) as e:          # IncompleteRead, RemoteDisconnected, URL malformato...: sono errori di rete
        raise OSError(f"rete: {type(e).__name__}")


class _Ambiguous(Exception):
    """Una scrittura (create) puo' essere arrivata o no: chi chiama verifica con find() prima di riprovare."""


def _is_incomplete(rs):
    """request_status di Notion: stringa ('incomplete') o oggetto ({'type': 'incomplete'} / {'state': ...}). Nel dubbio sull'oggetto
    si guarda ogni valore stringa; un valore diverso da assente/'complete' che contiene 'incomplete' = incompleto."""
    if rs is None:
        return False
    if isinstance(rs, str):
        return rs.strip().lower() == "incomplete"
    if isinstance(rs, dict):
        return any(isinstance(v, str) and "incomplete" in v.lower() for v in rs.values())
    return False


def _txt(prop):
    arr = (prop or {}).get("title") or (prop or {}).get("rich_text") or []
    return "".join(x.get("plain_text", "") for x in arr if isinstance(x, dict))


def _rich(text, limit=1900):
    return [{"type": "text", "text": {"content": str(text)[:limit]}}] if text else []


class NotionBackend:
    """Registro su Notion. Una pagina = una riga. Campi: Voce, Data, Importo USD, Business, Tipo, Modello, Fornitore, Scheda,
    Stato, Origine, Run key, Nota. Le opzioni di select sconosciute NON vengono create: Tipo/Fornitore sconosciuti -> 'Altro',
    Business sconosciuto -> errore (senza business non si applicano i tetti)."""
    name = "notion"

    def __init__(self, data_source_id=None, database_id=None, token=None, transport=None, sleep=time.sleep,
                 min_interval_s=0.35, timeout_s=20.0, max_attempts=4, deadline_s=None, version=None, page_size=100,
                 max_pages=200, token_source=None):
        data_source_id = data_source_id or os.environ.get("COST_LEDGER_NOTION_DATA_SOURCE_ID")
        database_id = database_id or os.environ.get("COST_LEDGER_NOTION_DATABASE_ID")
        if database_id and not data_source_id:
            self.mode, self.container, self.version = "database", database_id, version or NOTION_VERSION_DATABASE
        elif data_source_id:
            self.mode, self.container, self.version = "data_source", data_source_id, version or NOTION_VERSION_DATA_SOURCE
        else:
            raise LedgerUnavailable("backend notion: manca COST_LEDGER_NOTION_DATA_SOURCE_ID (o ..._DATABASE_ID)")
        tok = token if token is not None else os.environ.get("NOTION_TOKEN")
        self._token = (tok or "").strip() or None
        if not self._token:
            self.auth_source = "proxy"
        elif token is not None:
            self.auth_source = token_source or "parametro del chiamante"
        else:
            self.auth_source = "ambiente (NOTION_TOKEN)"
        self.transport, self.sleep = transport or _urllib_transport, sleep
        self.min_interval_s, self.timeout_s = min_interval_s, timeout_s
        self.max_attempts = max_attempts
        try:                                   # un hook non puo' aspettare 90 s: COST_LEDGER_DEADLINE_S lo accorcia
            self.deadline_s = float(deadline_s if deadline_s is not None else os.environ.get("COST_LEDGER_DEADLINE_S", 90.0))
        except ValueError:
            self.deadline_s = 90.0
        self.page_size, self.max_pages = min(int(page_size), 100), max_pages
        self._last = 0.0
        self._auth_logged = False
        self._budget_end = None                  # istante (monotonic) oltre il quale NESSUNA richiesta parte: vedi time_budget()

    @property
    def auth_mode(self):
        return "token" if self._token else "proxy"

    @contextmanager
    def time_budget(self, seconds):
        """Tempo massimo TOTALE per tutto cio' che questo backend fa dentro il blocco (richieste, attese tra i tentativi, rinvii). Scaduto:
        nessuna nuova richiesta, il timeout della richiesta in corso e' ridotto al tempo che resta, `LedgerUnavailable` e l'operazione
        resta in coda. Serve alla copia in ombra: non deve mai fermare un run piu' di qualche secondo."""
        try:
            secs = float(seconds)
        except (TypeError, ValueError):
            raise ValueError(f"time_budget: tempo non numerico ({seconds!r})")
        if not math.isfinite(secs) or secs <= 0:             # nan/inf/zero/negativi disattiverebbero o romperebbero il budget: si rifiutano
            raise ValueError(f"time_budget: il tempo deve essere un numero finito > 0 ({seconds!r})")
        prev = self._budget_end
        end = time.monotonic() + secs
        self._budget_end = end if prev is None else min(prev, end)
        try:
            yield
        finally:
            self._budget_end = prev

    def _budget_left(self):
        return None if self._budget_end is None else self._budget_end - time.monotonic()

    def _sleep_bounded(self, seconds):
        left = self._budget_left()
        if left is not None and seconds >= left:
            raise LedgerUnavailable("tempo massimo (budget) esaurito durante l'attesa tra due tentativi: operazione rimandata")
        self.sleep(seconds)

    # -- HTTP
    def _headers(self):
        h = {"Notion-Version": self.version, "Content-Type": "application/json", "User-Agent": "cost-ledger/1"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if not self._auth_logged:
            self._auth_logged = True
            if self._token:
                log.info("notion: autenticazione con chiave da %s", self.auth_source)
            else:
                log.warning("notion: nessuna chiave (ne' NOTION_TOKEN ne' .env), richiesta SENZA Authorization (fonte: proxy; modalita' "
                            "proxy del cloud: la chiave la aggiunge il proxy dell'ambiente)")
        return h

    def _throttle(self):
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep_bounded(wait)                        # il ritmo (3 richieste/s) conta nel budget: se non ci sta, non si parte
        self._last = time.monotonic()

    def _request(self, method, path, body=None, idempotent=True):
        url = NOTION_API_BASE + path
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        start, last_err = time.monotonic(), "nessuna risposta"
        for attempt in range(1, self.max_attempts + 1):
            if time.monotonic() - start > self.deadline_s:
                break
            left = self._budget_left()
            if left is not None and left <= 0:
                raise LedgerUnavailable("tempo massimo (budget) esaurito: operazione rimandata (resta in coda)")
            self._throttle()
            left = self._budget_left()
            if left is not None and left < 0.2:              # sotto i 200 ms una richiesta non ha senso: non si supera il budget con un minimo
                raise LedgerUnavailable("tempo massimo (budget) esaurito: troppo poco tempo per una richiesta, operazione rimandata (resta in coda)")
            timeout = self.timeout_s if left is None else min(self.timeout_s, left)
            try:
                status, hdrs, data = self.transport(method, url, self._headers(), raw, timeout)
            except Exception as e:                       # noqa: BLE001 - OSError, IncompleteRead, errori imprevisti: tutti "rete"
                last_err = f"errore di rete ({type(e).__name__}: {redact(e, self._token)[:100]})"
                if not idempotent:
                    raise _Ambiguous(last_err)
                self._sleep_bounded(min(2 ** attempt * 0.5, 8))
                continue
            if status == 429:
                try:
                    ra = float({k.lower(): v for k, v in hdrs.items()}.get("retry-after", 1))
                except (TypeError, ValueError):
                    ra = 1.0
                last_err = "HTTP 429 (limite di velocita')"
                self._sleep_bounded(min(max(ra, 0.0), 30.0))
                continue
            if status >= 500:
                last_err = f"HTTP {status}"
                if not idempotent:
                    raise _Ambiguous(last_err)
                self._sleep_bounded(min(2 ** attempt * 0.5, 8))
                continue
            if 200 <= status < 300:
                try:
                    obj = json.loads(data.decode("utf-8")) if data else {}
                except (ValueError, UnicodeDecodeError):
                    raise BadResponse(f"Notion HTTP {status}: corpo non JSON")
                if not isinstance(obj, dict):
                    raise BadResponse(f"Notion HTTP {status}: risposta non e' un oggetto")
                return obj
            code = msg = ""
            try:
                err = json.loads(data.decode("utf-8"))
                code, msg = str(err.get("code", "")), str(err.get("message", ""))
            except (ValueError, UnicodeDecodeError, AttributeError):
                pass
            hint = " (nessuna chiave: modalita' proxy; il proxy non ha autenticato o il database non e' condiviso con l'integrazione)" \
                if status in (401, 403, 404) and not self._token else ""
            raise LedgerUnavailable(redact(f"Notion HTTP {status} {code}: {msg[:160]}{hint}", self._token))
        raise LedgerUnavailable(f"Notion non risponde dopo {self.max_attempts} tentativi: {last_err}")

    # -- conversione
    def _props(self, r, full=True):
        p = {}
        if full:
            p["Voce"] = {"title": _rich(r.asset[:180] or r.run_key[:180])}
            p["Data"] = {"date": {"start": r.when.isoformat()}}
            p["Business"] = {"select": {"name": r.business}}
            p["Tipo"] = {"select": {"name": _select(r.tipo, TIPI)}}
            p["Modello"] = {"rich_text": _rich(r.model, 200)}
            p["Fornitore"] = {"select": {"name": _select(r.provider, FORNITORI)}}
            p["Scheda"] = {"url": r.scheda or None}
            p["Origine"] = {"select": {"name": _select(r.origin, ORIGINI, "Sessione")}}
            p["Run key"] = {"rich_text": _rich(r.run_key, 500)}
        p["Importo USD"] = {"number": r.amount_usd}
        p["Stato"] = {"select": {"name": r.state}}
        p["Nota"] = {"rich_text": _rich(r.note)}
        return p

    def _page_to_rec(self, page):
        try:
            return self._page_to_rec_inner(page)
        except (AttributeError, TypeError, KeyError, IndexError) as e:
            raise BadResponse(f"riga con forma inattesa ({type(e).__name__})")

    def _page_to_rec_inner(self, page):
        if not isinstance(page, dict) or not isinstance(page.get("properties"), dict) or not isinstance(page.get("id"), str):
            raise BadResponse("pagina senza id/properties")
        pr = page["properties"]

        def need(name, kind):
            v = pr.get(name)
            if not isinstance(v, dict) or v.get("type") != kind:
                raise BadResponse(f"riga {page['id'][:8]}: campo '{name}' mancante o di tipo inatteso")
            return v

        state = (need("Stato", "select").get("select") or {}).get("name")
        amount = need("Importo USD", "number").get("number")
        rk = _txt(need("Run key", "rich_text"))
        d = (need("Data", "date").get("date") or {}).get("start")
        if state not in ACTIVE_STATES + RELEASE_STATES:
            raise BadResponse(f"riga {page['id'][:8]}: Stato '{state}' non riconosciuto")
        if state in ACTIVE_STATES:                            # una riga che conta con importo/data/chiave rotti non si ignora
            if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not math.isfinite(amount) or amount < 0:
                raise BadResponse(f"riga {page['id'][:8]} ({state}): Importo USD vuoto o non valido")
            if not rk:
                raise BadResponse(f"riga {page['id'][:8]} ({state}): Run key vuota")
        try:
            when = parse_dt(d)
        except (ValueError, TypeError):
            raise BadResponse(f"riga {page['id'][:8]}: Data vuota o non valida")

        def sel(name):
            v = pr.get(name)
            return ((v or {}).get("select") or {}).get("name") or "" if isinstance(v, dict) else ""
        return Rec(run_key=rk, state=state, amount_usd=float(amount or 0), when=when, business=sel("Business"),
                   tipo=sel("Tipo") or "Altro", model=_txt(pr.get("Modello")), provider=sel("Fornitore"),
                   scheda=(pr.get("Scheda") or {}).get("url") or "", origin=sel("Origine") or "Sessione",
                   note=_txt(pr.get("Nota")), ref=page["id"])

    # -- query
    def _query_path(self):
        return f"/data_sources/{self.container}/query" if self.mode == "data_source" else f"/databases/{self.container}/query"

    def _raw_query(self, flt):
        out, cursor, seen = [], None, set()
        for _ in range(self.max_pages):
            body = {"page_size": self.page_size}
            if flt:
                body["filter"] = flt
            if cursor:
                body["start_cursor"] = cursor
            res = self._request("POST", self._query_path(), body)
            results = res.get("results")
            if not isinstance(results, list):
                raise BadResponse("risposta della query senza 'results'")
            if _is_incomplete(res.get("request_status")):
                raise BadResponse("Notion ha dichiarato la query incompleta (oltre 10.000 risultati): totale non affidabile")
            out.extend(results)
            if not res.get("has_more"):
                return out
            cursor = res.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise BadResponse("paginazione incoerente: has_more senza next_cursor valido")
            seen.add(cursor)
        raise BadResponse(f"paginazione oltre {self.max_pages} pagine: totale non affidabile")

    @staticmethod
    def _filter(states, since, until, prefix, business, scheda, run_key=None):
        f = []
        if run_key is not None:
            f.append({"property": "Run key", "rich_text": {"equals": run_key}})
        if states is not None:
            f.append({"or": [{"property": "Stato", "select": {"equals": s}} for s in states]} if len(states) > 1
                     else {"property": "Stato", "select": {"equals": states[0]}})
        if since:
            f.append({"property": "Data", "date": {"on_or_after": since.isoformat()}})
        if until:
            f.append({"property": "Data", "date": {"before": until.isoformat()}})
        if prefix:
            f.append({"property": "Run key", "rich_text": {"starts_with": prefix}})
        if business:
            f.append({"property": "Business", "select": {"equals": business}})
        if scheda:
            f.append({"property": "Scheda", "url": {"equals": scheda}})
        return None if not f else (f[0] if len(f) == 1 else {"and": f})

    def query(self, states=ACTIVE_STATES, since=None, until=None, prefix=None, business=None, scheda=None):
        recs = [self._page_to_rec(p) for p in self._raw_query(self._filter(states, since, until, prefix, business, scheda))]
        # il filtro e' rifatto qui: non ci si fida del solo lato server
        return [r for r in recs if r.state in states and not (since and r.when < since) and not (until and r.when >= until)
                and not (prefix and not r.run_key.startswith(prefix)) and not (business and r.business != business)
                and not (scheda and r.scheda != scheda)]

    def find(self, run_key):
        recs = [self._page_to_rec(p) for p in self._raw_query(self._filter(None, None, None, None, None, None, run_key=run_key))]
        recs = [r for r in recs if r.run_key == run_key]
        if not recs:
            return None
        recs.sort(key=lambda r: (r.state not in ACTIVE_STATES, r.ref))
        return recs[0]

    def create(self, r):
        if r.business not in BUSINESSES:
            raise ValueError(f"Business sconosciuto: {r.business!r}")
        parent = ({"type": "data_source_id", "data_source_id": self.container} if self.mode == "data_source"
                  else {"database_id": self.container})
        try:
            self._request("POST", "/pages", {"parent": parent, "properties": self._props(r)}, idempotent=False)
        except _Ambiguous as e:
            # la scrittura potrebbe essere arrivata: prima si guarda, poi (una sola volta) si riprova
            if self.find(r.run_key) is not None:
                return
            try:
                self._request("POST", "/pages", {"parent": parent, "properties": self._props(r)}, idempotent=False)
            except _Ambiguous as e2:
                raise LedgerUnavailable(f"creazione riga non confermata: {redact(e2, self._token)}")

    def update(self, run_key, state=None, amount_usd=None, note=None):
        cur = self.find(run_key)
        if cur is None:
            raise UnknownRunKey("run key sconosciuta")
        r = Rec(run_key=run_key, state=state or cur.state, amount_usd=cur.amount_usd if amount_usd is None else amount_usd,
                when=cur.when, note=cur.note if note is None else note)
        self._request("PATCH", f"/pages/{cur.ref}", {"properties": self._props(r, full=False)})


# ----------------------------------------------------------------------------- il registro

def day_bounds(day, tz=None):
    tz = tz or _tz()
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = day + datetime.timedelta(days=1)
    return start, datetime.datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)


def month_bounds(day, tz=None):
    tz = tz or _tz()
    start = datetime.datetime(day.year, day.month, 1, tzinfo=tz)
    ny, nm = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return start, datetime.datetime(ny, nm, 1, tzinfo=tz)


class CostLedger:
    def __init__(self, backend, policy=None, origin="PC", clock=None, settle_s=1.0, sleep=time.sleep, spool_path=None, legacy_spool_path=None):
        self.backend = backend
        self.policy = policy or load_policy()
        self.origin = origin if origin in ORIGINI else "Sessione"
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.settle_s, self.sleep = settle_s, sleep
        self.spool_path = Path(spool_path) if spool_path else None
        self.legacy_spool_path = Path(legacy_spool_path) if legacy_spool_path else None      # vecchia posizione della coda (vedi COST_LEDGER_SPOOL_DIR)
        self._flush_lock = threading.RLock()                 # un solo rinvio alla volta per questa istanza (thread)
        self._inst = f"{random.getrandbits(32):08x}"         # identita' dell'istanza: due thread con registri separati non si scambiano
        self._claim_seq = 0
        self.last_info = {}

    @contextmanager
    def time_budget(self, seconds):
        """Vedi NotionBackend.time_budget. Per il backend locale non fa nulla (non ha attese di rete)."""
        tb = getattr(self.backend, "time_budget", None)
        if tb is None:
            yield
        else:
            with tb(seconds):
                yield

    # -- totali
    def _today(self, now=None):
        """Giorno di Roma di `now` (default: orologio del registro, UTC). Mai l'orologio locale della macchina: nel cloud e' UTC.
        Un `now` senza fuso si intende gia' ora di Roma."""
        n = now or self.clock()
        if n.tzinfo is None:
            n = n.replace(tzinfo=_tz())
        return n.astimezone(_tz()).date()

    def _rome_date(self, x, now=None, today_word="today"):
        """today/this -> giorno di Roma di `now`; datetime -> il suo giorno di Roma; date -> cosi' com'e' (e' GIA' una data di Roma);
        'AAAA-MM-GG' / 'AAAA-MM' -> idem."""
        if isinstance(x, str):
            if x in ("today", "this"):
                return self._today(now)
            return datetime.date.fromisoformat(x + "-01" if len(x) == 7 else x)
        if isinstance(x, datetime.datetime):
            return self._today(x)
        return x

    def totals(self, *, day=None, month=None, content_key=None, scheda=None, business=None, group_by=None, states=ACTIVE_STATES, now=None):
        """Somma di importo (Prenotato + Confermato di default). day: date | 'today'; month: date | 'YYYY-MM' | 'this'.
        content_key: la 'chiave contenuto' (es. 2026-11-03-a2a-3e263594); scheda: URL della scheda Notion.
        group_by: None -> float; 'day' | 'month' | 'business' | 'scheda' | 'content' | 'tipo' | 'provider' -> dict."""
        self._ensure_spool_clean()
        return self._totals(day=day, month=month, content_key=content_key, scheda=scheda, business=business, group_by=group_by,
                            states=states, now=now)

    def _totals(self, *, day=None, month=None, content_key=None, scheda=None, business=None, group_by=None, states=ACTIVE_STATES, now=None):
        since = until = None
        if day is not None:
            since, until = day_bounds(self._rome_date(day, now))
        if month is not None:
            ms, mu = month_bounds(self._rome_date(month, now))
            since, until = (ms, mu) if since is None else (max(since, ms), min(until, mu))
        recs = self.backend.query(states=tuple(states), since=since, until=until,
                                  prefix=(content_key + "/") if content_key else None, business=business, scheda=scheda)
        if group_by is None:
            return round(sum(r.amount_usd for r in recs), 4)
        keyf = {"day": lambda r: r.when.astimezone(_tz()).date().isoformat(),
                "month": lambda r: r.when.astimezone(_tz()).strftime("%Y-%m"),
                "business": lambda r: r.business or "(senza business)",
                "scheda": lambda r: r.scheda or "(senza scheda)",
                "content": lambda r: r.run_key.split("/", 1)[0] if "/" in r.run_key else "(senza contenuto)",
                "tipo": lambda r: r.tipo, "provider": lambda r: r.provider or "(n.d.)"}.get(group_by)
        if keyf is None:
            raise ValueError(f"group_by sconosciuto: {group_by}")
        out = {}
        for r in recs:
            out[keyf(r)] = round(out.get(keyf(r), 0.0) + r.amount_usd, 4)
        return out

    # -- tetti
    def _checks(self, amount, business, content_key, retry, now):
        """Lista di (nome, speso_senza_questa, tetto, messaggio) da verificare.
        retry=None: automatico (tentativo ripetuto se la card ha gia' spesa); retry=True/False: deciso dal chiamante UNA volta per run
        (come approval_gate), cosi' il 2o clip di una prima generazione NON passa al tetto da 10 USD. Senza content_key: sempre 10."""
        pol, checks = self.policy, []
        cspent = self._totals(content_key=content_key, now=now) if content_key else 0.0
        if content_key:
            checks.append(("content", cspent, pol.content_cap_usd, "CONTENT CAP EXCEEDED: gia' speso/prenotato ${spent:.2f} su questo contenuto + "
                           "${req:.2f} di questo run > tetto per contenuto ${cap:.2f}"))
        is_retry = (not content_key) or (bool(retry) if retry is not None else cspent > 0)
        dcap = pol.daily_cap_usd if is_retry else pol.new_gen_cap_usd
        checks.append(("daily", self._totals(day="today", now=now), dcap, "DAILY CAP EXCEEDED: gia' spesi/prenotati oggi ${spent:.2f} + "
                       "${req:.2f} di questo run > tetto giornaliero ${cap:.2f} (" + ("tentativo ripetuto" if is_retry else "prima generazione: 2 USD di riserva per i tentativi") + ")"))
        if business == LAB_BUSINESS:
            checks.append(("lab_month", self._totals(month="this", business=LAB_BUSINESS, now=now), pol.lab_monthly_usd,
                           "LAB MONTHLY BUDGET EXCEEDED: gia' spesi/prenotati ${spent:.2f} questo mese + ${req:.2f} > budget del lab ${cap:.2f}"))
        return is_retry, checks

    def _enforce(self, amount, business, content_key, retry, now, post=False):
        is_retry, checks = self._checks(amount, business, content_key, retry, now)
        info = {"retry": is_retry}
        for name, spent, cap, msg in checks:
            used = spent if post else spent + amount     # dopo la scrittura 'spent' contiene gia' questa prenotazione
            info[name + "_after"] = round(used, 4)
            if used > cap + EPS:
                raise CapExceeded(msg.format(spent=spent - (amount if post else 0.0), req=amount, cap=cap) +
                                  (". Sforamento causato da un'altra prenotazione contemporanea: prenotazione annullata." if post else
                                   ". Nessuna chiamata eseguita."),
                                  cap_name=name, spent=spent, requested=amount, cap=cap, race=post)
        if business == LAB_BUSINESS:
            mult = self.policy.lab_monthly_usd
            after = info.get("lab_month_after", 0.0)
            before = after - amount
            info["alerts"] = [a for a in self.policy.lab_alerts if after >= a * mult - EPS and before < a * mult - EPS]
        return info

    def check(self, *, amount_usd, business, content_key=None, retry=None, allow_over_cap=False, now=None):
        """Come reserve() ma SENZA scrivere: usata da approval_gate per dire di no prima. Alza CapExceeded / LedgerError."""
        amount = _finite_nonneg(amount_usd, "amount_usd")
        if business not in BUSINESSES:
            raise ValueError(f"business sconosciuto: {business!r}")
        self._lab_per_test(amount, business, allow_over_cap)
        if allow_over_cap or amount <= 0:
            return {"skipped": "allow_over_cap" if allow_over_cap else "importo zero"}
        self._ensure_spool_clean()
        try:
            return self._enforce(amount, business, content_key, retry, now or self.clock())
        except (LedgerError, ValueError):
            raise
        except Exception as e:                                           # noqa: BLE001
            raise LedgerUnavailable(f"errore imprevisto nel registro ({type(e).__name__}): {redact(e)[:120]}")

    def _lab_per_test(self, amount, business, allow_over_cap):
        if business == LAB_BUSINESS and not allow_over_cap and amount > self.policy.lab_per_test_usd + EPS:
            raise CapExceeded(f"LAB PER-TEST CAP EXCEEDED: prova da ${amount:.2f} > tetto per prova ${self.policy.lab_per_test_usd:.2f}. "
                              f"Nessuna chiamata eseguita.", cap_name="lab_per_test", requested=amount, cap=self.policy.lab_per_test_usd)

    # -- prenota / conferma / rilascia
    def reserve(self, *, amount_usd, business, content_key=None, asset="", tipo="Video", model="", provider="Altro",
                scheda_url="", retry=None, allow_over_cap=False, note=""):
        """Prenota `amount_usd` PRIMA di chiamare il fornitore. Ritorna la run key. Alza CapExceeded (tetto) o LedgerError
        (registro non affidabile): in entrambi i casi NON si spende. Con allow_over_cap la decisione e' di Cipriano (riga
        `spesa-oltre-tetto` verificata dall'hook, fuori di qui): i tetti non si applicano, la prenotazione si scrive comunque."""
        amount = _finite_nonneg(amount_usd, "amount_usd")
        if amount <= 0:
            raise ValueError("amount_usd deve essere > 0 (una spesa a zero si scrive con record())")
        if business not in BUSINESSES:
            raise ValueError(f"business sconosciuto: {business!r}")
        self._ensure_spool_clean()                  # spese gia' sostenute e rimaste in coda: se non partono, NIENTE nuova spesa
        try:
            return self._reserve_inner(amount, business, content_key, asset, tipo, model, provider, scheda_url, retry, allow_over_cap, note)
        except (LedgerError, ValueError):
            raise
        except Exception as e:                                           # noqa: BLE001 - mai un'eccezione "nuda" prima di una spesa
            raise LedgerUnavailable(f"errore imprevisto nel registro ({type(e).__name__}): {redact(e)[:120]}")

    def _reserve_inner(self, amount, business, content_key, asset, tipo, model, provider, scheda_url, retry, allow_over_cap, note):
        now = self.clock()
        self._lab_per_test(amount, business, allow_over_cap)
        info = {} if allow_over_cap else self._enforce(amount, business, content_key, retry, now)
        rk = new_run_key(content_key, asset, now)
        rec = Rec(run_key=rk, state=STATE_RESERVED, amount_usd=amount, when=now.astimezone(_tz()), business=business, tipo=tipo, model=model,
                  provider=provider, scheda=scheda_url or "", origin=self.origin,
                  note=(note + " " if note else "") + ("[allow_over_cap] " if allow_over_cap else "") + f"prenotato {amount:.4f}")
        self.backend.create(rec)
        if not allow_over_cap:
            # finestra di gara PC/cloud: dopo aver scritto, si rilegge TUTTO (anche le prenotazioni altrui) e si rifa' il conto
            self.sleep(self.settle_s)
            try:
                for attempt in range(3):          # l'indice di Notion puo' essere in ritardo: si aspetta di rivedere la propria riga
                    if self.backend.find(rk) is not None:
                        break
                    self.sleep(max(self.settle_s, 0.5))
                else:
                    raise LedgerUnavailable("la propria prenotazione non e' ancora visibile nel registro: conteggio non affidabile")
                info = self._enforce(amount, business, content_key, info.get("retry", retry), self.clock(), post=True)
            except CapExceeded:
                self._release_quiet(rk, "sforamento da prenotazioni concorrenti")
                raise
            except Exception as e:                                       # noqa: BLE001 - LedgerError e imprevisti: si annulla e si nega
                self._release_quiet(rk, "ricontrollo non riuscito")
                raise LedgerUnavailable(f"ricontrollo dopo la prenotazione non riuscito: {type(e).__name__}: {redact(e)[:120]}")
        self.last_info = info
        return rk

    def _release_quiet(self, run_key, reason):
        try:
            self.release(run_key, reason)
        except Exception as e:                                           # noqa: BLE001 - la riga resta Prenotato: conta (prudente) e va liberata a mano
            log.error("prenotazione %s NON rilasciata (%s): resta contata finche' non la libera Ops", run_key, redact(e))

    def confirm(self, run_key, actual_usd, note=""):
        """Dopo la generazione: costo REALE (es. crediti kie x 0.005). Prenotato -> Confermato. Idempotente se stesso importo.
        Se il registro non risponde il costo viene messo nel file di coda (spool_path) per il rinvio e l'errore sale."""
        actual = _finite_nonneg(actual_usd, "actual_usd")
        with self._spooling({"op": "confirm", "run_key": run_key, "actual_usd": actual, "note": note}):
            return self._confirm_impl(run_key, actual, note)

    def _confirm_impl(self, run_key, actual, note=""):
        if True:
            cur = self.backend.find(run_key)
            if cur is None:
                raise UnknownRunKey("run key sconosciuta: nessuna prenotazione da confermare")
            if cur.state == STATE_CONFIRMED:
                if abs(cur.amount_usd - actual) <= 1e-6:
                    return actual
                raise LedgerError(f"gia' confermata con importo diverso ({cur.amount_usd} vs {actual}): non si riscrive in silenzio")
            if cur.state != STATE_RESERVED:
                raise LedgerError(f"stato {cur.state}: solo una prenotazione si conferma")
            extra = f" | prenotato {cur.amount_usd:.4f}, reale {actual:.4f}" + (" (SUPERA la prenotazione)" if actual > cur.amount_usd + 1e-6 else "")
            self.backend.update(run_key, state=STATE_CONFIRMED, amount_usd=actual, note=((note + " ") if note else "") + extra.strip(" |"))
            return actual

    def release(self, run_key, reason, state=STATE_CANCELLED):
        """Nessuna spesa (errore prima della chiamata, rifiuto del validatore, nessun addebito): la prenotazione non conta piu'."""
        if state not in RELEASE_STATES:
            raise ValueError(f"stato di rilascio non valido: {state}")
        with self._spooling({"op": "release", "run_key": run_key, "reason": str(reason)[:300], "state": state}):
            return self._release_impl(run_key, reason, state)

    def _release_impl(self, run_key, reason, state):
        if True:
            cur = self.backend.find(run_key)
            if cur is None:
                raise UnknownRunKey("run key sconosciuta: nessuna prenotazione da rilasciare")
            if cur.state in RELEASE_STATES:
                return
            if cur.state != STATE_RESERVED:
                raise LedgerError(f"stato {cur.state}: una spesa confermata non si rilascia (serve una rettifica dell'Analista)")
            self.backend.update(run_key, state=state, amount_usd=0.0,
                                note=f"{state.lower()}: {str(reason)[:300]} | era prenotato {cur.amount_usd:.4f}")

    def record(self, *, amount_usd, business, content_key=None, asset="", tipo="Altro", model="", provider="Altro",
               scheda_url="", note="", when=None, run_key=None):
        """Voce gia' sostenuta (es. ElevenLabs a 0, rettifiche): Confermato subito, senza controllo dei tetti (i soldi sono gia' usciti).
        `run_key` fissa la chiave (serve al rinvio dalla coda senza duplicati)."""
        amount = _finite_nonneg(amount_usd, "amount_usd")
        if business not in BUSINESSES:
            raise ValueError(f"business sconosciuto: {business!r}")
        now = when or self.clock()
        rk = run_key or new_run_key(content_key, asset, now)
        self.backend.create(Rec(run_key=rk, state=STATE_CONFIRMED, amount_usd=amount, when=now.astimezone(_tz()), business=business, tipo=tipo,
                                model=model, provider=provider, scheda=scheda_url or "", origin=self.origin, note=note))
        return rk

    # -- code e prenotazioni vecchie
    @contextmanager
    def _spooling(self, item):
        """Operazione su un costo GIA' sostenuto: se il registro non risponde (o altro imprevisto) l'operazione va in coda e l'errore sale
        sempre come LedgerError. Errori di logica (chiave sconosciuta, stato sbagliato) non vanno in coda."""
        try:
            yield
        except (LedgerUnavailable, BadResponse, UnknownRunKey):
            self._spool(item)
            raise
        except (LedgerError, ValueError):
            raise
        except Exception as e:                                           # noqa: BLE001
            self._spool(item)
            raise LedgerUnavailable(f"errore imprevisto nel registro ({type(e).__name__}): {redact(e)[:120]}")

    def _spool(self, item):
        log.error("OPERAZIONE DI COSTO IN CODA (non scritta nel registro): %s", redact(json.dumps(item, ensure_ascii=False, default=str))[:600])
        if not self.spool_path:
            return
        try:
            append_pending(self.spool_path, [json.dumps(item, ensure_ascii=False, default=str)])           # sotto blocco tra processi
        except (OSError, LedgerError):
            log.error("coda locale non scrivibile: l'operazione %s %s va rifatta a mano (vedi riga precedente)", item.get("op"), item.get("run_key"))

    STALE_CLAIM_S = _env_float("COST_LEDGER_CLAIM_STALE_S", 120.0)      # un 'in rinvio' piu' vecchio (dalla presa) e' di un processo morto
    _CLAIM_RE = re.compile(r"\.flush(\d+)-([0-9a-f]+)(?:-(\d+))?(?:-\d+)*$")

    def _claim_name(self):
        """Nome UNIVOCO di un file 'in rinvio': pid, identita' dell'istanza, istante della presa in secondi (serve a misurare l'eta': la
        rinomina conserva la data di modifica del contenuto), nanosecondi e un contatore. Nello stesso secondo, anche per lo stesso processo,
        due nomi non coincidono mai (era il difetto G1: due file presi nello stesso secondo si sovrascrivevano e le voci sparivano)."""
        self._claim_seq += 1
        return self.spool_path.with_name(f"{self.spool_path.name}.flush{os.getpid()}-{self._inst}-{int(time.time())}-{time.time_ns()}-{self._claim_seq}")

    def _take(self, src):
        """Rinomina `src` in un nome 'in rinvio' NUOVO senza mai sovrascrivere un file esistente: si sceglie un nome libero e si usa
        os.rename (su Windows fallisce se la destinazione esiste; sui sistemi POSIX non sovrascrive grazie al controllo exists() e alla
        univocita' del nome). Ritorna il nuovo percorso; OSError se `src` non c'e' piu' (preso da un altro) o e' occupato."""
        for _ in range(8):
            dst = self._claim_name()
            if dst.exists():
                continue
            try:
                os.rename(src, dst)
                return dst
            except FileExistsError:
                continue
        raise OSError("nessun nome libero per il file in rinvio")

    def _claims(self):
        """[(percorso, istante_della_presa)] di tutti i file 'in rinvio' (miei, di altri processi, di altri thread, abbandonati)."""
        out = []
        if not self.spool_path:
            return out
        try:
            for c in self._queue_names(".flush"):
                m = self._CLAIM_RE.search(c.name)
                taken = float(m.group(3)) if (m and m.group(3)) else c.stat().st_mtime      # formato senza istante: ripiego sulla data di modifica
                out.append((c, taken))
        except OSError as e:
            raise LedgerUnavailable(f"coda locale non leggibile: {redact(e)[:100]}")
        return out

    def _queue_names(self, suffix):
        """Percorsi dei file della coda il cui nome inizia per `<nome coda><suffix>`, elencati con `os.scandir` (v8.2, N1): una cartella che non si puo' elencare
        SOLLEVA LedgerUnavailable (i cancelli NEGANO) invece di dare una lista vuota come faceva `Path.glob`. Cartella assente = nessuna coda = lista vuota (non viene creata)."""
        try:
            return [self.spool_path.parent / n for n in _scan_names(self.spool_path.parent, self.spool_path.name + suffix)]
        except OSError as e:
            raise LedgerUnavailable(f"cartella della coda dei costi non elencabile ({type(e).__name__}: {redact(e)[:80]}): cancello CHIUSO")

    def _item_files(self):
        """File 'item' completi (una o piu' righe ciascuno, scritti con tmp + rinomina su un nome univoco): e' DOVE vanno tutte le voci dalla v7."""
        return [c for c in self._queue_names(".item-") if not os.path.normcase(c.name).endswith(".tmp")]

    def _tmp_files(self):
        """[(percorso, eta' in secondi)] dei file `.item-*.tmp`: una scrittura in corso (millisecondi) o un processo ucciso tra scrittura e rinomina.
        v8.2: un `.tmp` di cui non si riesce a leggere la data (accesso negato...) NON sparisce dall'elenco: conta come presente e recente (eta' 0, il cancello nega);
        sparisce solo se non esiste piu' (preso da un altro processo: e' gia' diventato item, che si elenca DOPO)."""
        out = []
        for c in self._queue_names(".item-"):
            if not os.path.normcase(c.name).endswith(".tmp"):
                continue
            try:
                out.append((c, time.time() - c.stat().st_mtime))
            except FileNotFoundError:
                pass
            except OSError:
                out.append((c, 0.0))
        return out

    def _main_size(self):
        """Dimensione del file principale della coda (v5/v6): 0 se NON esiste; ogni altro errore (accesso negato...) SALE come OSError (v8.2: `exists()` lo
        faceva diventare 'non c'e'' e il cancello passava)."""
        st = _stat_or_none(self.spool_path)
        return 0 if st is None else st.st_size

    def _check_spool_is_a_file(self):
        """Una cartella (o altro) al posto della coda NON e' 'coda vuota': i cancelli negano. v8.2: anche se non si riesce a leggere cosa c'e' (l'OSError sale)."""
        st = _stat_or_none(self.spool_path)
        if st is not None and not stat_mod.S_ISREG(st.st_mode):
            raise LedgerUnavailable(f"{self.spool_path.name} esiste ma non e' un file: la coda dei costi non e' leggibile, cancello CHIUSO (serve Ops)")

    def _main_pending(self):
        try:
            self._check_spool_is_a_file()
            return self._main_size() > 0 or bool(self._item_files()) or bool(self._tmp_files())
        except OSError as e:
            raise LedgerUnavailable(f"coda locale non leggibile: {redact(e)[:100]}")

    def _spool_files(self):
        """Tutti i file di coda: 'in rinvio' (recenti o vecchi), tmp, item, principale (v5/v6). SOLO PER INVENTARI E MESSAGGI (selfcheck, spool-status, spool-archive):
        e' un elenco SENZA blocco e non autorizza nulla. Ogni controllo 'coda vuota' che autorizza una spesa usa `_locked_listing` (regola Ops 31)."""
        if not self.spool_path:
            return []
        self._check_spool_is_a_file()
        claims = [c for c, _ in self._claims()]
        tmps = [c for c, _ in self._tmp_files()]
        items = self._item_files()
        main = [self.spool_path] if self._main_size() > 0 else []
        return claims + tmps + items + main

    def _queue_listing(self):
        """Elenco COMPLETO della coda, nell'unico ordine che non puo' perdere una voce: file 'in rinvio' PRIMA, poi `.tmp`, poi `.item-*`, infine il
        file principale. Da chiamare SOLO con `_QueueLock` tenuto (chi prende i file non puo' spostarli mentre si elenca). Ogni spostamento che avviene
        SENZA il blocco va da un nome elencato prima a uno elencato dopo (`.tmp` -> `.item`, 'in rinvio' -> `.item` e rinvio -> principale): una voce
        non puo' quindi sparire dall'elenco. Ritorna (claims, tmps, items, main_pending)."""
        claims = self._claims()
        tmps = self._tmp_files()
        items = self._item_files()
        main = self._main_size() > 0
        return claims, tmps, items, main

    def _locked_listing(self):
        """`_queue_listing()` SOTTO il blocco di presa (D-A, v8). Ogni controllo 'coda vuota' che autorizza una spesa si fa cosi': sotto lo stesso
        blocco con cui si prendono i file, guardando prima i file in rinvio e poi gli item. Senza blocco un altro processo che prende un file (item ->
        in rinvio) o rimette i resti (in rinvio -> item) in mezzo all'elenco faceva vedere una coda vuota con 5 USD ancora dentro (provato: fino al 20%
        dei cancelli). Blocco non ottenuto in tempo / non disponibile = LedgerUnavailable = il cancello NEGA."""
        try:
            self._check_spool_is_a_file()
            with _QueueLock(self.spool_path):
                return self._queue_listing()
        except OSError as e:
            raise LedgerUnavailable(f"coda locale non leggibile: {redact(e)[:100]}")

    def _check_legacy_spool(self):
        """Dopo lo spostamento della coda fuori da OneDrive (COST_LEDGER_SPOOL_DIR): se nella vecchia posizione e' rimasto qualcosa, i costi li' dentro
        non sarebbero visti da nessuno: il cancello NEGA finche' Ops non li rinvia (spool-flush --spool <vecchia>) o li archivia.
        v8.1 (G1, regola Ops 31): e' un controllo 'coda vuota' che autorizza una spesa, quindi passa dalla STESSA funzione sotto blocco della coda principale
        (`_locked_listing`: blocco di presa della VECCHIA coda, poi in rinvio, .tmp, item, principale). Nella v8 era un elenco senza blocco e nell'ordine
        sbagliato: durante la migrazione (`spool-flush --spool <vecchia>` con i cancelli attivi) un cancello vedeva la vecchia coda vuota con 5 USD dentro
        (verificatore, 23/10: 31 letture OK su 19.881). Fail closed: cartella illeggibile, blocco non ottenibile o non disponibile, coda che e' una cartella = NEGA.
        v8.2 (N1): 'illeggibile' vale davvero anche per 'accesso negato' (elenco con os.scandir; con Path.glob una cartella non elencabile dava lista vuota = PASSA, provato con icacls).
        Una cartella della vecchia posizione che NON ESISTE vale 'nessuna coda' (come prima) e NON viene creata."""
        legacy = self.legacy_spool_path
        if not legacy or Path(legacy) == self.spool_path:
            return
        try:
            try:
                os.stat(str(Path(legacy).parent))
            except FileNotFoundError:                   # cartella assente = nessuna coda (non Path.exists(): nasconde anche 'accesso negato', che deve NEGARE)
                return
            old = CostLedger(LocalBackend(Path("unused.json"), must_exist=False), policy=Policy(), spool_path=legacy)
            claims, tmps, items, main = old._locked_listing()
            files = [c for c, _ in claims] + [c for c, _ in tmps] + items + ([old.spool_path] if main else [])
        except (OSError, LedgerError) as e:
            raise LedgerUnavailable(f"vecchia posizione della coda dei costi non leggibile ({redact(e)[:80]}): cancello CHIUSO")
        if files:
            raise LedgerUnavailable(f"nella vecchia posizione della coda dei costi ({Path(legacy).parent}) sono rimasti {len(files)} file ({files[0].name}...): "
                                    f"cancello CHIUSO finche' non si rinviano (`python cost_ledger.py spool-flush --spool <vecchia coda>`) o si archiviano")

    def _ensure_spool_clean(self):
        """Prima di ogni lettura di totali e di ogni prenotazione/controllo: se in coda ci sono costi gia' sostenuti si tenta il rinvio.
        Il cancello NEGA se: resta anche una sola operazione; esiste un file 'in rinvio' o una scrittura in corso (.tmp recente) che non e' stato
        possibile prendere in carico; il blocco di presa non arriva entro il tempo; la coda non e' un file. Mai saltarlo.
        v8 (D-A): sia il controllo 'coda vuota' iniziale sia quello dopo il rinvio si fanno SOTTO il blocco di presa (`_locked_listing`)."""
        if not self.spool_path:
            return
        with self._flush_lock:
            self._check_legacy_spool()
            claims, tmps, items, main = self._locked_listing()
            if not (claims or tmps or items or main):
                return
            with self.time_budget(GATE_FLUSH_MAX_S):                # tetto di tempo del rinvio: un grande arretrato non blocca il cancello all'infinito
                done, left = self.flush_spool()
            if left:
                raise LedgerUnavailable(f"{left} operazioni di costo gia' sostenute sono ancora in coda locale ({self.spool_path.name}) e non si "
                                        f"riescono a scrivere nel registro (o il tempo massimo del rinvio, {GATE_FLUSH_MAX_S:.0f} s, e' finito): "
                                        f"totali incompleti, cancello CHIUSO finche' non si rinviano (serve Ops)")
            claims, tmps, items, main = self._locked_listing()
            if claims:
                raise LedgerUnavailable(f"rinvio della coda dei costi in corso da un altro processo ({claims[0][0].name}): totali non ancora "
                                        f"completi, cancello CHIUSO, riprova tra qualche secondo")
            if tmps or items or main:                              # resta qualcosa che non siamo riusciti a prendere (.tmp recente, file occupato, tempo finito)
                raise LedgerUnavailable(f"la coda dei costi ({self.spool_path.name}) ha ancora voci non prese in carico (scrittura in corso, file "
                                        f"occupato o tempo massimo del rinvio finito): cancello CHIUSO, riprova")

    def _append_lines(self, lines):
        if not lines:
            return
        _write_item(self.spool_path, lines)                        # file unico, nessun blocco: i resti tornano in coda senza attese

    def flush_spool(self, max_items=None):
        """Rinvia le operazioni in coda. Ritorna (rinviate, rimaste). `max_items`: tetto di operazioni tentate per volta su TUTTI i file
        insieme (le altre tornano in coda com'erano). Il file principale e i file 'in rinvio' ABBANDONATI (presa piu' vecchia di 2 minuti,
        misurata dal nome) vengono presi con una rinomina atomica verso un nome NUOVO e mai sovrascritto: due processi non rinviano la
        stessa riga e nessun file ne cancella un altro. Un file 'in rinvio' RECENTE di un altro non si tocca (il chiamante,
        _ensure_spool_clean, nega). Le rimaste tornano in coda; una riga illeggibile (anche byte non UTF-8) resta in coda e blocca."""
        if not self.spool_path:
            return 0, 0
        with self._flush_lock:
            mine = []
            left_b = self.backend._budget_left() if hasattr(self.backend, "_budget_left") else None
            # con un budget (ombra) l'attesa del blocco e' al massimo un QUARTO del tempo che resta: il resto serve a scrivere la voce vera
            lock_timeout = QUEUE_LOCK_TIMEOUT_S if left_b is None else min(QUEUE_LOCK_TIMEOUT_S, left_b * 0.25)
            try:
                self._check_spool_is_a_file()
                with _QueueLock(self.spool_path, timeout=lock_timeout):         # presa ESCLUSIVA (blocco del SO): la sola rinomina non lo e' su Windows
                    if self._main_size() > 0:      # coda in append delle v5/v6
                        try:
                            mine.append(self._take(self.spool_path))
                        except OSError:
                            pass
                    items = self._item_files()
                    if max_items is not None and len(items) > max_items:         # rinvio "a piccole dosi" (ombra): si prendono solo i piu' vecchi, il resto
                        def _age(f):                                              # resta dov'e' (una coda arretrata non rallenta ogni scrittura in ombra)
                            try:
                                return f.stat().st_mtime
                            except OSError:
                                return 0.0
                        items = sorted(items, key=_age)[:max_items]
                    for it in items:                                # file unici (v7 e ripiego di spend_gate)
                        if hasattr(self.backend, "_budget_left") and (self.backend._budget_left() is not None and self.backend._budget_left() <= 0):
                            break                                   # tempo finito: gli altri restano in coda, dove sono
                        try:
                            mine.append(self._take(it))
                        except OSError:
                            pass
                    for tmp, age in self._tmp_files():              # .tmp di un processo morto tra scrittura e rinomina: recuperato dopo ORPHAN_TMP_AGE_S
                        if _age_expired(age, ORPHAN_TMP_AGE_S):     # (v8.1: anche con la data nel futuro oltre la tolleranza)
                            try:
                                mine.append(self._take(tmp))
                            except OSError:
                                pass
                    for c, taken in self._claims():
                        if c in mine or not _age_expired(time.time() - taken, self.STALE_CLAIM_S):
                            continue                                # recente: e' di qualcuno che sta lavorando (v8.1: la presa nel futuro oltre la tolleranza NON e' recente)
                        try:
                            mine.append(self._take(c))              # recupero di un rinvio abbandonato (sempre sotto blocco)
                        except OSError:
                            pass
            except OSError as e:
                raise LedgerUnavailable(f"coda locale non leggibile: {redact(e)[:100]}")
            done, left_total, attempts = 0, 0, 0
            for claim in mine:
                try:
                    text = claim.read_bytes().decode("utf-8", errors="replace")
                except OSError as e:
                    try:
                        if not self.spool_path.exists():
                            os.rename(claim, self.spool_path)        # rimessa a posto: nulla si perde
                    except OSError:
                        pass
                    raise LedgerUnavailable(f"coda locale non leggibile: {redact(e)[:100]}")
                lines = _split_lines(text)                          # si divide solo sugli a-capo: U+2028/U+2029/U+0085 dentro una stringa JSON non spezzano la riga (M2)
                left, idx = [], 0
                try:
                    while idx < len(lines):
                        if (max_items is not None and attempts >= max_items) or                                 (hasattr(self.backend, "_budget_left") and (self.backend._budget_left() is not None and self.backend._budget_left() <= 0)):
                            left.extend(lines[idx:])               # tetto raggiunto (su tutti i file) o budget finito: il resto torna in coda com'e'
                            idx = len(lines)
                            break
                        line = lines[idx]
                        try:
                            it = json.loads(line)
                            op = it["op"]
                        except (ValueError, KeyError, TypeError):
                            left.append(line)                       # riga illeggibile: resta com'e', blocca
                            idx += 1
                            continue
                        attempts += 1
                        try:
                            if op == "confirm":
                                self._confirm_impl(it["run_key"], it["actual_usd"], it.get("note", ""))
                            elif op == "record_legacy":
                                record_legacy_entry(self, it["entry"], run_key=it.get("run_key"), spool=False, replay=True)
                            elif op == "release":
                                self._release_impl(it["run_key"], it.get("reason", ""), it.get("state", STATE_CANCELLED))
                            else:
                                raise LedgerError(f"operazione sconosciuta in coda: {op}")
                            done += 1
                        except Exception:                          # noqa: BLE001 - qualsiasi cosa: l'operazione resta in coda
                            left.append(line)
                        idx += 1
                except BaseException:                              # interruzione (Ctrl-C...): niente si perde
                    try:
                        self._append_lines(left + lines[idx:])
                        claim.unlink()
                    except (OSError, LedgerError):
                        pass
                    raise
                try:
                    self._append_lines(left)
                except OSError as e:                               # i file 'in rinvio' restano: nulla va perso (si recuperano dopo 2 minuti)
                    raise LedgerUnavailable(f"coda locale non riscrivibile: {redact(e)[:100]} (file in rinvio conservato: {claim.name})")
                left_total += len(left)
                try:
                    claim.unlink()
                except OSError:
                    pass
            return done, left_total

    def stale_reservations(self, hours=6.0, now=None):
        """Prenotazioni ancora 'Prenotato' dopo `hours` ore: un run morto o una conferma persa. Contano nei totali (prudenza);
        NON si rilasciano da sole: un run morto potrebbe aver speso. Le guarda Ops/Direttore e decidono (release o confirm)."""
        now = now or self.clock()
        lim = datetime.timedelta(hours=hours)
        return [r for r in self.backend.query(states=(STATE_RESERVED,)) if now - r.when > lim]


# ----------------------------------------------------------------------------- selezione backend e funzioni di modulo

def get_ledger(backend=None, **kw):
    """backend: 'local' | 'notion' | None (legge COST_LEDGER_BACKEND; se assente 'local' con registro che DEVE esistere: in un
    ambiente senza costs.json si ottiene un errore, mai un registro vuoto che fa passare tutto)."""
    name = (backend or os.environ.get("COST_LEDGER_BACKEND") or "local").strip().lower()
    origin = kw.pop("origin", os.environ.get("COST_LEDGER_ORIGIN", "PC"))
    if name == "local":
        be = LocalBackend(path=kw.pop("path", None), must_exist=kw.pop("must_exist", True))
    elif name == "notion":
        bkw = {k: kw.pop(k) for k in list(kw) if k in ("data_source_id", "database_id", "token", "transport",
                                                        "min_interval_s", "timeout_s", "page_size", "version", "token_source")}
        if "sleep" in kw:
            bkw["sleep"] = kw["sleep"]
        be = NotionBackend(**bkw)
        kw.setdefault("spool_path", _default_spool())     # senza coda i costi pagati e non scritti sparirebbero e i totali la ignorerebbero
        if (os.environ.get(SPOOL_DIR_ENV) or "").strip():  # coda spostata: nella vecchia posizione non deve restare nulla (i cancelli lo controllano)
            kw.setdefault("legacy_spool_path", _legacy_default_spool())
    else:
        raise LedgerUnavailable(f"backend sconosciuto: {name!r} (ammessi: local, notion)")
    return CostLedger(be, origin=origin, **kw)


_default = None


def _led():
    global _default
    if _default is None:
        _default = get_ledger()
    return _default


def reserve(**kw):
    return _led().reserve(**kw)


def confirm(run_key, actual_usd, note=""):
    return _led().confirm(run_key, actual_usd, note)


def release(run_key, reason, state=STATE_CANCELLED):
    return _led().release(run_key, reason, state)


def totals(**kw):
    return _led().totals(**kw)


# ----------------------------------------------------------------------------- migrazione di costs.json (SOLO dry-run)

def _tipo_of(e):
    a, m, p, k = str(e.get("asset", "")).lower(), str(e.get("model") or "").lower(), str(e.get("provider") or "").lower(), str(e.get("kind") or "").lower()
    if k == "chat" or a.startswith("ask_model/"):
        return "Chat/testo"
    if p == "elevenlabs" or a.endswith((".wav", ".mp3")) or "eleven" in m or "sound-generation" in m:
        return "Voce"
    if a.endswith(".mp4") or "video" in m or "seedance" in m or "avatar" in m or "veo" in m:
        return "Video"
    if a.endswith((".png", ".jpg", ".jpeg", ".webp")) or "image" in m or "banana" in m:
        return "Immagine"
    return "Altro"


def _fornitore_of(p):
    p = str(p or "").lower()
    for key, name in (("kie", "kie.ai"), ("fal", "fal.ai"), ("higgsfield", "Higgsfield"), ("eleven", "ElevenLabs")):
        if p.startswith(key):
            return name
    return "Altro"


tipo_of, fornitore_of = _tipo_of, _fornitore_of


def record_legacy_entry(led, entry, *, run_key=None, spool=True, replay=False):
    """Scrive in `led` una voce nel formato di spend_gate.ledger_append ({project, asset, cost_usd, provider, model, created, ...}).
    E' la riga della spesa GIA' sostenuta: Confermata subito, nessun tetto. Se il registro non risponde (o succede qualunque altra cosa)
    la voce va nella coda locale (spool) E in un messaggio di errore nel log, e sale un LedgerError: la spesa non sparisce senza traccia.
    La coda viene rinviata prima di ogni lettura di totali (CostLedger._ensure_spool_clean): finche' resta qualcosa, il cancello NEGA.
    La run key e' fissata alla prima scrittura: il rinvio (replay=True) non duplica una riga arrivata anche se la risposta e' andata persa."""
    asset = str(entry.get("asset", ""))
    content_key, _, rest = asset.partition("/")
    if not rest:
        content_key, rest = None, asset
    biz = norm_business(entry.get("project"))
    note = "" if biz else f"project originale '{entry.get('project')}' non riconosciuto: mappato su Test"
    try:
        when = parse_dt(entry["created"]) if entry.get("created") else None
    except (ValueError, TypeError):
        when = None
    rk = run_key or new_run_key(content_key, rest, when or led.clock())
    item = {"op": "record_legacy", "entry": entry, "run_key": rk}
    try:
        if replay and led.backend.find(rk) is not None:
            return rk
        return led.record(amount_usd=entry.get("cost_usd", 0) or 0, business=biz or TEST_BUSINESS, content_key=content_key,
                          asset=rest, tipo=_tipo_of(entry), model=str(entry.get("model") or ""),
                          provider=_fornitore_of(entry.get("provider")), note=note, when=when, run_key=rk)
    except (LedgerUnavailable, BadResponse):
        if spool:
            led._spool(item)
        raise
    except LedgerError:
        raise
    except ValueError as e:                                                # voce malformata (importo non numerico/negativo...)
        # Una spesa pagata e NON contata sarebbe il caso peggiore: la voce va in coda, dove non si potra' mai rinviare, quindi il cancello
        # resta CHIUSO finche' Ops non la sistema (spool-status / spool-archive). Oltre all'errore esplicito e alla riga nel log.
        log.error("SPESA NON REGISTRABILE (voce malformata): %s -> %s", redact(json.dumps(entry, ensure_ascii=False, default=str))[:400], redact(e))
        if spool:
            led._spool(item)
        raise LedgerError(f"voce di costo malformata, messa in coda e cancello chiuso finche' non e' sistemata: {redact(e)[:100]}")
    except Exception as e:                                                 # noqa: BLE001
        if spool:
            led._spool(item)
        raise LedgerUnavailable(f"errore imprevisto nel registro ({type(e).__name__}): {redact(e)[:120]}")


def migrate_dry_run(entries):
    """Calcola le righe che verrebbero create nel registro Notion a partire dalle voci di costs.json. NESSUNA scrittura, nessuna rete.
    Ritorna {'rows': [...], 'summary': {...}, 'problems': [...]}. Controllo di quadratura: somma righe == somma costs.json."""
    rows, problems, tot_in, unmapped, to_test = [], [], 0.0, {}, {}
    for i, e in enumerate(entries):
        try:
            cost = _finite_nonneg(e.get("cost_usd", 0) or 0, "cost_usd")
            when = parse_dt(e.get("created"))
        except (ValueError, TypeError) as ex:
            problems.append(f"voce {i}: {ex}")
            continue
        tot_in += cost
        biz = norm_business(e.get("project"))
        note = f"migrato da costs.json (voce {i})"
        if biz == TEST_BUSINESS:
            to_test[str(e.get("project"))] = to_test.get(str(e.get("project")), 0.0) + cost
            note += f"; project originale '{e.get('project')}' (prova)"
        if biz is None:
            unmapped[str(e.get("project"))] = unmapped.get(str(e.get("project")), 0.0) + cost
            biz, note = TEST_BUSINESS, note + f"; project originale '{e.get('project')}' non riconosciuto: mappato su Test"
        asset = str(e.get("asset", ""))
        rows.append({"run_key": f"{asset}~mig{i:04d}", "asset": asset, "Stato": STATE_CONFIRMED, "Importo USD": round(cost, 6),
                     "Data": when.isoformat(), "Business": biz, "Tipo": _tipo_of(e), "Fornitore": _fornitore_of(e.get("provider")),
                     "Modello": str(e.get("model") or ""), "Origine": "PC", "Nota": note})
    tot_out = round(sum(r["Importo USD"] for r in rows), 6)
    by_biz, by_month = {}, {}
    for r in rows:
        by_biz[r["Business"]] = round(by_biz.get(r["Business"], 0.0) + r["Importo USD"], 4)
        by_month[r["Data"][:7]] = round(by_month.get(r["Data"][:7], 0.0) + r["Importo USD"], 4)
    lab_rule_now = round(sum(float(e.get("cost_usd", 0) or 0) for e in entries if str(e.get("project")) == "Lab"), 4)
    if abs(tot_in - tot_out) > 1e-6:
        problems.append(f"QUADRATURA: costs.json {tot_in:.6f} != righe {tot_out:.6f}")
    return {"rows": rows, "problems": problems,
            "summary": {"voci_in": len(entries), "righe_out": len(rows), "totale_in": round(tot_in, 4), "totale_out": round(tot_out, 4),
                        "per_business": by_biz, "per_mese": by_month, "progetti_mappati_su_Test": {k: round(v, 4) for k, v in to_test.items()},
                        "progetti_non_riconosciuti_mappati_su_Test": {k: round(v, 4) for k, v in unmapped.items()},
                        "lab_totale_con_regola_attuale_project_Lab": lab_rule_now,
                        "lab_totale_dopo_mappatura": by_biz.get(LAB_BUSINESS, 0.0), "test_totale": by_biz.get(TEST_BUSINESS, 0.0)}}


# ----------------------------------------------------------------------------- CLI (sola lettura / dry-run)

def _cli_notion_kwargs():
    """Per i comandi da terminale sul PC: NOTION_TOKEN dall'ambiente, altrimenti la chiave del pipeline tramite notion_client (quello che
    fa gia' spend_gate); se non c'e' nulla: nessuna chiave (proxy). Il modulo in se' non legge file di chiavi."""
    if os.environ.get("NOTION_TOKEN"):
        return {}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import notion_client
        return {"token": notion_client.load_token(), "token_source": ".env del pipeline tramite notion_client"}
    except (ImportError, SystemExit):
        return {}


# ----------------------------------------------------------------------------- posizione della coda (M4, v8): dentro o fuori da OneDrive

SPOOL_DIR_ENV = "COST_LEDGER_SPOOL_DIR"
SPOOL_NAME = "costs-pending.jsonl"
_ONEDRIVE_ATTRS = 0x80000 | 0x100000 | 0x40000 | 0x400000       # PINNED, UNPINNED, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS (attributi che mette OneDrive)


def _under_onedrive(path):
    """Motivo (testo) per cui `path` sta in una cartella sincronizzata da OneDrive, oppure None. Tre prove: una parte del percorso che inizia per
    'OneDrive'; la cartella e' dentro la radice indicata da OneDrive/OneDriveConsumer/OneDriveCommercial; (Windows) una cartella antenata con gli
    attributi dei file di OneDrive (`pinned`/`unpinned`/recall: e' cosi' che `generations\\` risulta sincronizzata anche se il nome non lo dice)."""
    try:
        p = Path(path).resolve()
    except OSError:
        p = Path(os.path.abspath(str(path)))
    for part in p.parts:
        if part.lower().startswith("onedrive"):
            return f"il percorso contiene '{part}'"
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(var)
        if root:
            try:
                p.relative_to(Path(root).resolve())
                return f"e' dentro la cartella di OneDrive ({var})"
            except (ValueError, OSError):
                pass
    if os.name == "nt":
        for q in [p] + list(p.parents):
            try:
                if getattr(os.stat(str(q)), "st_file_attributes", 0) & _ONEDRIVE_ATTRS:
                    return f"la cartella '{q.name or q}' ha gli attributi di OneDrive (sincronizzata)"
            except OSError:
                pass
    return None


def spool_dir_problem(directory):
    """None se `directory` e' una cartella utilizzabile per la coda (assoluta, esistente, scrivibile, NON sotto OneDrive); altrimenti il motivo."""
    d = Path(directory)
    if not d.is_absolute():
        return f"il percorso non e' assoluto ({d})"
    if not d.exists():
        return f"la cartella non esiste ({d}): va creata prima"
    if not d.is_dir():
        return f"non e' una cartella ({d})"
    od = _under_onedrive(d)
    if od:
        return f"sta sotto OneDrive ({od}): la coda sincronizzata genera un file sincronizzato per ogni voce"
    probe = d / f".cost-ledger-write-probe-{os.getpid()}-{time.time_ns()}"
    try:
        fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
        try:
            os.write(fd, b"x")
        finally:
            os.close(fd)
        os.unlink(str(probe))
    except OSError as e:
        return f"la cartella non e' scrivibile ({type(e).__name__})"
    return None


def resolve_spool_path(default_path):
    """Percorso della coda. Senza `COST_LEDGER_SPOOL_DIR` e' `default_path` (comportamento di sempre: generations/costs-pending.jsonl). Con la
    variabile e' `<cartella>/costs-pending.jsonl`, dopo aver controllato che la cartella esista, sia scrivibile e NON sia sotto OneDrive; se il
    controllo fallisce solleva LedgerUnavailable (cancello CHIUSO: una variabile sbagliata non rimette in silenzio la coda dove e' sincronizzata).
    `costs.json` e il suo blocco `costs.json.lock` NON si spostano (sono di spend_gate); il blocco della coda e' `<cartella>/costs-pending.jsonl.lock`."""
    env = (os.environ.get(SPOOL_DIR_ENV) or "").strip()
    if not env:
        return Path(default_path)
    problem = spool_dir_problem(env)
    if problem:
        raise LedgerUnavailable(f"{SPOOL_DIR_ENV} non utilizzabile: {problem}. Cancello CHIUSO finche' non e' corretta (o tolta la variabile, a coda vuota).")
    return Path(env) / SPOOL_NAME


def _legacy_default_spool():
    ws = find_workspace()
    return (ws / "Sistema" / "scripts" / "video-pipeline" / "generations" / SPOOL_NAME) if ws else Path(SPOOL_NAME)


def _default_spool():
    return resolve_spool_path(_legacy_default_spool())


def _selfcheck():
    ok, out = selfcheck_data()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if ok else 1


def queue_inventory(spool):
    """Inventario della coda `spool`, SOLO in lettura e senza blocco (e' un'informazione, non un permesso): usato da selfcheck e spool-status, cosi'
    contano le righe allo stesso modo. Ritorna {'errore': testo|None, 'file': [Path], 'righe': n, 'claims': [(nome, eta_s)], 'tmp': [(nome, eta_s)]}.
    Una CARTELLA al posto della coda e' un errore (non 'coda vuota')."""
    spool = Path(spool)
    inv = {"errore": None, "file": [], "righe": 0, "claims": [], "tmp": []}
    led = CostLedger(LocalBackend(Path("unused.json"), must_exist=False), policy=Policy(), spool_path=spool)
    try:
        if _stat_or_none(spool.parent) is None:                    # cartella ASSENTE = nessuna coda (v8.2: un altro errore, es. accesso negato, NON e' 'assente')
            return inv
        st = _stat_or_none(spool)
        if st is not None and not stat_mod.S_ISREG(st.st_mode):
            inv["errore"] = f"{spool.name} esiste ma non e' un file"
            return inv
        files = led._spool_files()                                 # v8.2: cartella non elencabile = LedgerUnavailable, mai 'zero file'
        inv["claims"] = [(c.name, round(time.time() - t)) for c, t in led._claims()]
        inv["tmp"] = [(c.name, round(a)) for c, a in led._tmp_files()]
    except (OSError, LedgerError) as e:
        inv["errore"] = str(e)[:200]
        return inv
    inv["file"] = files
    unreadable = []
    for f in files:
        try:
            inv["righe"] += len(_split_lines(Path(f).read_bytes().decode("utf-8", errors="replace")))
        except FileNotFoundError:                                  # preso da un altro processo mentre si contava: non e' un errore
            pass
        except OSError as e:                                       # v8.2: un file che non si legge NON e' '0 righe'
            unreadable.append(f"{Path(f).name} ({type(e).__name__})")
    if unreadable:
        inv["errore"] = f"{len(unreadable)} file di coda non leggibili, righe sottostimate: {', '.join(unreadable[:3])}"[:200]
    return inv


def selfcheck_data():
    """Controlli senza rete. Ritorna (ok, dati); ok=False se qualcosa che fa NEGARE il cancello non va (fuso, tetti). Mai valori di variabili."""
    ok, out = True, {"python": sys.version.split()[0], "cost_ledger": __version__, "revisione": __revision__}
    try:
        tz = _tz()
        summer = datetime.datetime(2026, 7, 1, 12, tzinfo=datetime.timezone.utc).astimezone(tz).utcoffset()
        winter = datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.timezone.utc).astimezone(tz).utcoffset()
        out["fuso"] = {"nome": str(tz), "estate_utc+": summer.total_seconds() / 3600, "inverno_utc+": winter.total_seconds() / 3600}
        if (summer.total_seconds(), winter.total_seconds()) != (7200.0, 3600.0) and "COST_LEDGER_TZ" not in os.environ:
            ok, out["fuso"]["ATTENZIONE"] = False, "Roma dovrebbe essere +2 d'estate e +1 d'inverno"
    except LedgerError as e:
        ok, out["fuso"] = False, f"NON DISPONIBILE: {e}"
    try:
        pol = load_policy()
        out["tetti"] = {"contenuto": pol.content_cap_usd, "giornaliero": pol.daily_cap_usd, "prima_generazione": pol.new_gen_cap_usd,
                        "lab_mese": pol.lab_monthly_usd, "lab_prova": pol.lab_per_test_usd, "fonte": pol.source}
    except LedgerError as e:                       # v8.4: budget.yaml/models.yaml esistono ma non si leggono o non sono validi: i cancelli del modo notion NEGHEREBBERO
        ok, out["tetti"] = False, {"errore": str(e)[:300]}
    names = ("COST_LEDGER_BACKEND", "COST_LEDGER_SHADOW", "COST_LEDGER_ORIGIN", "COST_LEDGER_DEADLINE_S", "COST_LEDGER_SHADOW_BUDGET_S", "COST_LEDGER_NOTION_DATA_SOURCE_ID",
             "COST_LEDGER_NOTION_DATABASE_ID", "COST_LEDGER_TZ", SPOOL_DIR_ENV, "NOTION_TOKEN")
    out["variabili"] = {n: ("impostata" if os.environ.get(n) else "assente") for n in names}        # solo presenza, mai il valore
    out["chiave_notion"] = "ambiente (NOTION_TOKEN)" if os.environ.get("NOTION_TOKEN") else "assente nell'ambiente (sul PC puo' arrivare dal .env tramite notion_client; nel cloud: proxy)"
    legacy = _legacy_default_spool()
    try:
        sp = _default_spool()
    except LedgerError as e:                       # COST_LEDGER_SPOOL_DIR sbagliata: i cancelli in modo notion NEGHEREBBERO
        out["coda"] = {"errore": str(e)[:240]}
        return False, out
    inv = queue_inventory(sp)
    try:
        main_exists = _stat_or_none(sp) is not None
    except OSError:
        main_exists = None
    # v8.2 (m3): `presente` vale per TUTTI i file di coda (principale, item, .tmp, in rinvio), non solo per il principale (con 2 righe in coda diceva false);
    # None = non verificabile (cartella non elencabile: l'errore e' sotto). `principale_presente` e' il vecchio significato.
    presente = True if (inv["file"] or main_exists) else (None if inv["errore"] else False)
    out["coda"] = {"file": str(sp), "presente": presente, "principale_presente": main_exists, "righe": inv["righe"], "file_in_coda": len(inv["file"]),     # principale + item + tmp + in rinvio
                   "fuori_da_onedrive": _under_onedrive(sp.parent) is None, "cartella_esterna": bool((os.environ.get(SPOOL_DIR_ENV) or "").strip())}
    if inv["errore"]:
        ok, out["coda"]["errore"] = False, inv["errore"]
    if sp != legacy:                               # coda spostata: nella vecchia posizione non deve essere rimasto niente
        old = queue_inventory(legacy)
        out["coda"]["vecchia_posizione"] = {"file": str(legacy), "righe": old["righe"], "file_in_coda": len(old["file"])}
        if old["errore"]:
            out["coda"]["vecchia_posizione"]["errore"] = old["errore"]
        if old["file"] or old["errore"]:
            ok = False
    # il blocco del sistema operativo (flock/LockFile) funziona su QUESTO file system ed e' esclusivo? (cloud: montaggi condivisi possono non supportarlo)
    try:
        import tempfile
        base = sp if sp.parent.is_dir() else Path(tempfile.mkdtemp(prefix="cost_ledger_probe_")) / "probe"
        with _QueueLock(base, timeout=1.0):
            try:
                with _QueueLock(base, timeout=0.2):
                    excl = False
            except LedgerUnavailable:
                excl = True
        out["blocco_so"] = {"funziona": True, "esclusivo": excl, "dove": str(base.parent)}
        if not excl:
            ok = False
    except LedgerError as e:
        ok = False
        out["blocco_so"] = {"funziona": False, "errore": str(e)[:160]}
    return ok, out


def _spool_cmd(a):
    try:
        spool = Path(a.spool) if a.spool else _default_spool()
    except LedgerError as e:
        print(json.dumps({"errore": str(e)[:300]}, ensure_ascii=False))
        return 1
    if a.cmd == "spool-flush":
        try:
            led = get_ledger(a.backend or "notion", spool_path=spool, **(_cli_notion_kwargs() if (a.backend or "notion") == "notion" else {}))
        except LedgerError as e:                  # v8.4: file dei tetti illeggibile/non valido = errore chiaro (la coda resta dov'e')
            print(json.dumps({"errore": str(e)[:300]}, ensure_ascii=False))
            return 1
        done, left = led.flush_spool()
        print(json.dumps({"rinviate": done, "rimaste": left}, ensure_ascii=False))
        return 0 if left == 0 else 1
    inv = queue_inventory(spool)
    if a.cmd == "spool-status":
        print(json.dumps({"file": str(spool), "righe_in_coda": inv["righe"], "file_in_rinvio": [{"nome": n, "eta_presa_s": e} for n, e in inv["claims"]],
                          "file_tmp": [{"nome": n, "eta_s": e} for n, e in inv["tmp"]], "errore": inv["errore"]}, indent=2, ensure_ascii=False))
        return 0 if (not inv["righe"] and not inv["claims"] and not inv["tmp"] and not inv["errore"] and not inv["file"]) else 1
    # spool-archive: non si archivia un rinvio recente di un altro processo ne' una scrittura in corso (.tmp giovane)
    if inv["errore"]:
        print(json.dumps({"errore": inv["errore"]}, ensure_ascii=False))
        return 1
    fresh = ([n for n, e in inv["claims"] if not _age_expired(e, CostLedger.STALE_CLAIM_S)] +
             [n for n, e in inv["tmp"] if not _age_expired(e, ORPHAN_TMP_AGE_S)])
    if fresh:
        print(json.dumps({"errore": "rinvio o scrittura in corso (file recenti): riprova", "file": fresh}, ensure_ascii=False))
        return 1
    if not inv["file"]:                                           # v8.3 (m-b): niente da spostare = non si crea NULLA (ne' la cartella `--to` ne' il `.lock` della coda)
        print(json.dumps({"archiviati": []}, ensure_ascii=False))
        return 0
    import shutil
    dest = Path(a.to)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    moved = []
    try:
        with _QueueLock(spool, timeout=5.0):                      # lo spostamento non si accavalla con un rinvio che sta prendendo i file
            for f in inv["file"]:
                if Path(f).exists():
                    target = dest / f"{Path(f).name}.archiviato-{stamp}"
                    shutil.move(str(f), str(target))
                    moved.append(str(target))
    except LedgerError as e:
        print(json.dumps({"errore": str(e)[:200], "archiviati": moved}, ensure_ascii=False))
        return 1
    print(json.dumps({"archiviati": moved}, ensure_ascii=False))
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Registro costi condiviso (staging). Solo lettura e dry-run.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("totals", help="totali del giorno/mese dal backend scelto")
    t.add_argument("--backend", default=None)
    t.add_argument("--costs", help="percorso di costs.json (backend local)")
    t.add_argument("--group-by", default=None)
    sub.add_parser("selfcheck", help="controlli locali senza rete: fuso di Roma (tzdata), tetti, variabili d'ambiente, coda")
    for nm, hp in (("spool-status", "stato della coda dei costi in sospeso"), ("spool-flush", "tenta il rinvio della coda al backend scelto"),
                   ("spool-archive", "SPOSTA la coda in una cartella di archivio (non cancella)")):
        sp = sub.add_parser(nm, help=hp)
        sp.add_argument("--spool", help="percorso della coda (default generations/costs-pending.jsonl del pipeline)")
        sp.add_argument("--backend", default=None)
        if nm == "spool-archive":
            sp.add_argument("--to", required=True, help="cartella di archivio")
    m = sub.add_parser("migrate-dry-run", help="mostra le righe che si creerebbero da costs.json (nessuna scrittura)")
    m.add_argument("--costs", required=True)
    m.add_argument("--out", help="file JSON dove salvare le righe (opzionale)")
    a = ap.parse_args(argv)
    if a.cmd == "selfcheck":
        return _selfcheck()
    if a.cmd in ("spool-status", "spool-flush", "spool-archive"):
        return _spool_cmd(a)
    if a.cmd == "migrate-dry-run":
        res = migrate_dry_run(json.loads(Path(a.costs).read_text(encoding="utf-8")))
        print(json.dumps({"summary": res["summary"], "problems": res["problems"]}, indent=2, ensure_ascii=False))
        if a.out:
            Path(a.out).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1 if res["problems"] else 0
    try:
        if (a.backend or os.environ.get("COST_LEDGER_BACKEND") or "local") == "notion":
            led = get_ledger("notion", **_cli_notion_kwargs())
        else:
            led = get_ledger(a.backend, path=a.costs) if (a.backend or "local") == "local" and a.costs else get_ledger(a.backend)
    except LedgerError as e:                      # v8.4: file dei tetti illeggibile/non valido = errore chiaro, non un traceback
        print(json.dumps({"errore": str(e)[:300]}, ensure_ascii=False))
        return 1
    try:
        out = {"oggi": led.totals(day="today"), "mese": led.totals(month="this"), "backend": led.backend.name,
               "policy": led.policy.__dict__}
        if a.group_by:
            out["gruppi"] = led.totals(group_by=a.group_by)
    except LedgerError as e:                      # es. coda dei costi non rinviabile: i totali sarebbero incompleti, si dice chiaro
        print(json.dumps({"errore": str(e)[:300]}, ensure_ascii=False))
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
