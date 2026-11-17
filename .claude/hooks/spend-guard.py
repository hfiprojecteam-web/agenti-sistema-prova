#!/usr/bin/env python
"""
spend-guard.py - hook PreToolUse (Bash). Governance deterministica della spesa e del disco (14/10/2026).

STAGING P2c-bis R2 (31/10/2026, punto 1c/63): la verifica dell'approvazione `spesa-video` usa `approvazioni.md` sul PC (default) e il DB
Notion "Approvazioni" via approve_spend nel cloud (SPEND_GUARD_APPROVAL_BACKEND=notion). R2: nel cloud `approve_spend` si importa PER PERCORSO DI FILE
(mai per nome), solo se e' un file .py regolare con l'hash atteso scritto qui sotto, e solo se nella cartella del pipeline non c'e' nulla che possa
"coprire" i moduli (cartella-pacchetto omonima, estensioni compilate); il ramo della spesa del cloud ha un tempo massimo TOTALE (TOTAL_DEADLINE_S). Un valore diverso da `file`/`notion` NEGA la
spesa (non ricade mai sul file). --allow-over-cap con un backend diverso da `file` e' sempre negato. Nel cloud i messaggi di rifiuto
rimandano al DB, non a approvazioni.md, e portano il segno `[approvazione: notion]`. Il resto INVARIATO. NON installato.

R3 (31/10/2026, difetto G-A del terzo giro): l'hook non deve MAI eseguire un file piantato nel repository con il nome di un modulo standard
(logging.py, argparse.py, random.py, tempfile.py...) o di una libreria (yaml.py...). Il vecchio rimedio ("cartella del pipeline in fondo a sys.path")
non bastava: `vp_common.py` e `cost_ledger._cli_notion_kwargs` fanno `sys.path.insert(0, <pipeline>)`, quindi dopo `import yaml, vp_common` il pipeline
tornava in testa. Ora: (1) la cartella dello script (sys.path[0]) si toglie dal percorso di import; (2) una BARRIERA DI IMPORTAZIONE (finder in
sys.meta_path al posto di PathFinder) risolve dalla cartella del pipeline SOLO i quattro moduli della catena (PIPELINE_MODULES) e ogni altro nome
FUORI dal repository (le voci di sys.path dentro il repo, salvo un ambiente Python nel repo, sono ignorate): non dipende da quali nomi esistono,
da come cambia la catena o dalla versione di Python; (3) i quattro moduli si caricano dai byte letti (mai da __pycache__ o .pyc), e nel cloud (backend
'notion') solo se hanno l'hash atteso (CHAIN_PINS, come approve_spend), cosi' anche un cost_ledger.py modificato rende la spesa NEGATA.
Residuo dichiarato: quello che gira PRIMA della prima riga dell'hook (sitecustomize/usercustomize, file .pth, PYTHONPATH, PYTHONSTARTUP) e i
pacchetti installati fuori dal repo (pip install, anche --user) non sono controllati da qui.

R4 (31/10/2026, quarto giro del verificatore): (D1) il residuo qui sopra NON e' piccolo: un yaml.py, un usercustomize.py o un .pth nella cartella dei pacchetti
dell'utente (nel cloud ~/.local/lib/python3.11/site-packages) facevano dare il consenso all'hook. Il rimedio sta nel COMANDO dell'hook in settings.json
(`python3 -I`: niente user site, niente PYTHON*, niente cartella dello script), non in questo file: `settings.json.proposto` R4. Resta fuori dalla portata di qualunque
difesa dentro l'hook cio' che l'agente puo' scrivere in site-packages di sistema e nella libreria standard (sonda del cloud: `sonda_ambiente_cloud.py`).
(D3) anche i SOTTOMODULI si cercano solo nelle voci fidate (pacchetti namespace). (D4) sono fidate solo le voci presenti nel percorso di import all'installazione
della barriera E fuori dal repository: cio' che il codice aggiunge dopo (approve_spend aggiunge `<sopra il repo>/scripts/video-pipeline`) non e' mai fidato.
(D6) un ambiente Python dentro il repository non e' piu' fidato; il controllo del disco su Linux guarda il disco del repository (prima sollevava e non faceva nulla).

Non decide al posto di Cipriano: forza una conferma esplicita dove serve.
- Comando che lancia uno script a pagamento del video pipeline CON --max-usd  -> "deny", SALVO approvazione (dal 18/10/2026, punto 28):
  passa solo se (a) il comando usa run.py / 01_generate.py / 00_scene_master.py con --brief, (b) in `approvazioni.md` c'e' una riga
  valida `spesa-video` per l'id completo della card del brief, (c) --max-usd non supera l'importo della riga, (d) la spesa di oggi
  (costs.json) + --max-usd resta sotto il tetto giornaliero: 10 USD per i tentativi ripetuti (--regenerate o card con spesa gia'
  sostenuta), 8 USD per una prima generazione (riserva del 20% = 2 USD). Il tetto di 5 USD per contenuto e' nel codice (approval_gate).
  Tutto il resto (nessuna riga, altri script, nessun --brief, errore nei controlli) -> "deny".
  In modalita' automatica "ask" non ferma nulla (verificato col log), quindi si nega.
- Comando con --allow-over-cap (supera il tetto cumulativo per contenuto)       -> "deny", SALVO (dal 20/10/2026, decisione di Cipriano in chat)
  una riga valida `spesa-oltre-tetto` in approvazioni.md per l'id completo della card (importo massimo, scadenza entro oggi + 2 giorni);
  restano comunque i controlli `spesa-video` e il tetto giornaliero. Backup della versione precedente: _backup_2026-10-20/spend-guard.py.bak.
- Runner di prova senza cancello (run_kie_gen, run_kie_i2v, run_kie_pixverse, run_kie_image_compare, _kling_test2, run_wan_from_sidecar, gen_still)
  lanciati con python -> "deny", sempre (18/10/2026). run_higgsfield_gen e run_heygen_gen hanno gia' --max-usd nel codice: con --max-usd ricadono
  nel ramo "spesa" (negati: non passano da --brief); senza --max-usd fanno solo la stima.
- Dal 18/10/2026 (correzioni di Ops, 44 prove): opzioni abbreviate (--max, --allow-over...), --max-usd ripetuto o non numerico (1e1, 1_0) -> "deny";
  i runner senza cancello si negano con qualunque interprete, percorso assoluto, uv run, python -c; aggiunti run_kie_ref_look e voice_*;
  le righe `spesa-video` valgono solo se la scadenza e' entro oggi + 2 giorni.
- Disco C: sotto 3 GB (era 1 GB; allineato a W1 §4) e comando che scrive molti dati (ffmpeg, whisper, generazioni, download) -> "deny".
Legge il JSON dell'evento da stdin. Errore interno fuori dal ramo della spesa: lascia passare (exit 0); nel ramo della spesa: nega.
"""
import os, sys   # os e sys sono GIA' caricati dall'avvio dell'interprete
if __name__ == "__main__":
    # R3: la cartella dello script (sys.path[0]) non serve all'hook e non deve poter coprire i moduli standard (un logging.py accanto all'hook)
    try:
        _HOOK_DIR = os.path.normcase(os.path.realpath(os.path.dirname(os.path.abspath(__file__))))
        sys.path[:] = [_p for _p in sys.path if not (isinstance(_p, str) and os.path.normcase(os.path.realpath(_p or os.getcwd())) == _HOOK_DIR)]
    except OSError:
        pass
import datetime, hashlib, importlib.abc, importlib.machinery, importlib.util, json, pathlib, re, shutil, threading, types, zoneinfo

PAID = re.compile(r"(?:^|[\s/\\\"'])(01_generate|00_scene_master|run|batch|kling3_test|voice_tts|voice_design|voice_sts|voice_sfx|voice_bodyfx|cover_photo|run_higgsfield_gen|run_heygen_gen)\.py\b")
# Runner di prova SENZA cancello di spesa (18/10/2026): chiamano l'API a pagamento senza --max-usd e senza approval_gate. Negati sempre
# quando vengono LANCIATI (interprete + percorso dello script); le semplici menzioni (grep, cat, ls) non scattano. Non e' a prova di
# aggiramento (es. python -c con runpy): la soluzione definitiva e' il runner di lab con cancello (`lab_run.py`, vedi Sistema/lab/LAB.md).
UNGATED_NAME_RE = re.compile(
    r"(?:run_kie_gen|run_kie_i2v|run_kie_pixverse|run_kie_image_compare|_kling_test2|run_wan_from_sidecar|gen_still|run_kie_ref_look|"
    r"voice_tts|voice_design|voice_sts|voice_sfx|voice_bodyfx)\.py\b")
# Primo comando di un segmento che e' solo lettura: la semplice citazione dello script non e' un lancio.
READ_ONLY_VERBS = {"grep", "rg", "cat", "type", "ls", "dir", "sed", "head", "tail", "less", "more", "echo", "wc", "diff", "find", "stat", "file",
                   "git", "get-content", "get-childitem", "select-string", "gc", "sls", "test-path", "get-item", "gi", "gci", "findstr",
                   "where", "which", "write-output", "write-host", "printf"}


def launches_ungated(cmd):
    """True se un segmento del comando (separato da && || ; | o a capo) lancia uno dei runner senza cancello: qualunque interprete,
    percorso assoluto, uv run, python -c con runpy. Un segmento che comincia con un verbo di sola lettura (grep, cat, ls...) non conta."""
    for seg in re.split(r"&&|\|\||;|\||\n", cmd):
        if not UNGATED_NAME_RE.search(seg):
            continue
        words = seg.strip().split(None, 1)
        first = words[0].strip("\"'()&").lower().replace("\\", "/").split("/")[-1] if words else ""
        if first in READ_ONLY_VERBS:
            continue
        return True
    return False


def flag_keys(cmd):
    """Nomi delle opzioni '--xxx' presenti nel comando (senza il valore dopo '=')."""
    return [t.split("=", 1)[0].strip("\"'") for t in re.findall(r"""(?:"[^"]*"|'[^']*'|\S)+""", cmd) if t.lstrip("\"'").startswith("--")]


def has_prefix_of(cmd, flag):
    """Opzione scritta come prefisso abbreviato di `flag` (es. --max, --allow-over): argparse la accetta se allow_abbrev non e' disattivato."""
    return any(k != flag and len(k) >= 3 and flag.startswith(k) for k in flag_keys(cmd))
HEAVY = re.compile(r"\b(ffmpeg|whisper|faster-whisper|yt-dlp|01_generate\.py|run\.py|batch\.py|kling3_test\.py|pip install|npm install|winget install)\b")
MIN_FREE_GB = 3.0       # allineata a W1 §4 (18/10/2026); una sola definizione, qui

ROOT = pathlib.Path(__file__).resolve().parents[2]
APPROVALS = ROOT / "Sistema" / "architettura-agenti" / "approvazioni.md"
COSTS_JSON = ROOT / "Sistema" / "scripts" / "video-pipeline" / "generations" / "costs.json"
PIPELINE_DIR = ROOT / "Sistema" / "scripts" / "video-pipeline"
BRIEF_SCRIPTS = re.compile(r"(?:^|[\s/\\\"'])(01_generate|00_scene_master|run)\.py\b")   # scripts che accettano --brief e passano da approval_gate
DAILY_CAP_USD = 10.0          # tetto giornaliero totale (tentativi ripetuti inclusi)
NEW_GEN_CAP_USD = 8.0         # prima generazione di una card: lascia il 20% (2 USD) di riserva per i tentativi
HEX32 = re.compile(r"[0-9a-f]{32}")

# P2c-bis (31/10/2026, punto 1c/63): fonte dell'approvazione `spesa-video`. 'file' = approvazioni.md (PC, default se la variabile manca o e' vuota);
# 'notion' = DB "Approvazioni" via approve_spend (cloud). Si imposta 'notion' SOLO nel settings.json del cloud. Qualunque altro valore = spesa NEGATA
# (mai ricadere sul file: nel cloud approvazioni.md e' ferma al clone).
BACKENDS = ("file", "notion")
APPROVAL_BACKEND = (os.environ.get("SPEND_GUARD_APPROVAL_BACKEND") or "file").strip().lower()

# P2c-bis R2: hash atteso di approve_spend.py (sha256 INTERO, calcolato sul contenuto con le fini riga normalizzate a LF: git su Windows puo' cambiare
# CRLF/LF tra il PC e il cloud). Cambiare approve_spend.py SENZA cambiare questa riga (e senza ripassare dal verificatore) = spesa NEGATA nel cloud (voluto).
# Come si aggiorna: `python -B hash_attesi.py` (cartella di staging) stampa il valore; si scrive qui, nell'hook, nello stesso commit. Vedi LEGGIMI.
APPROVE_SPEND_SHA256 = "67681fd54926fa9a2cfd2455868d08eeeeb162197b0f7bcd203c41a72f7020d0"
# Moduli che l'hook (o approve_spend) importa dalla cartella del pipeline: nessun'altra cosa con lo stesso nome deve poterli "coprire".
MODULES_NO_SHADOW = ("approve_spend", "cost_ledger", "spend_gate", "vp_common", "notion_client")
# Tempo massimo TOTALE del ramo della spesa quando il backend non e' 'file' (lettura "Approvazioni" <= 30 s + due letture del registro costi + avvio).
# Sta SOTTO il timeout dell'hook (120 s in settings.json.proposto): se scade, l'hook NEGA da solo, senza dipendere da `onFailure`.
TOTAL_DEADLINE_S = 90.0

# ---- R3 (G-A): barriera di importazione e moduli della catena verificati ------------------------------------------------------------------------------
# Gli UNICI moduli che l'hook importa dalla cartella del pipeline (in ordine di dipendenza). Ogni altro nome si risolve fuori dal repository.
PIPELINE_MODULES = ("cost_ledger", "spend_gate", "notion_client", "vp_common")
# Hash attesi (sha256 INTERO, fini riga normalizzate a LF) dei moduli della catena: imposti SOLO quando il backend non e' 'file' (cloud). Le prove stanno
# in uscite/. Come si aggiornano: `python -B hash_attesi.py` (cartella di staging) stampa e controlla i valori; si scrivono qui nello stesso commit del
# modulo cambiato e dopo un nuovo giro del verificatore; senza, nel cloud la spesa e' NEGATA (voluto). vp_common ha due valori noti: quello del cloud
# oggi (7affdbecb107) e quello del PC (16dd9528329c, con la guardia del punto 104: allineamento facoltativo, LEGGIMI Passo 8b).
CHAIN_PINS = {
    "cost_ledger": ("6ceaedc8d5348f8ffe35b81cd0771d6df15aee361c9e84703be8b7023714c105",),
    "spend_gate": ("a179c5f167e4e0883bb1e74396b9bec2bff2dc1d66c578fc6d67bc6c9f0fd562",),
    "notion_client": ("9676640b7cf89279fede79f9e643f5837987e5c39405fe403b27878963d58e84",),
    "vp_common": ("a75082cff286f557e8b6f896f2f570bf45ec7e5eaff53a3d1c1a7d8a6be1fa24",
                  "9672efe4793a01ab9076e32ad6f0516c63acfa4f2940245473cf15bf494260d4"),
}
_BARRIER_STATE = {"error": None}      # se l'installazione della barriera fallisce, il ramo della spesa NEGA (non si lavora senza barriera)


def _norm(p):
    return os.path.normcase(os.path.realpath(p))


def _inside(child, parent):
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:                      # dischi diversi (Windows)
        return False


def _is_untrusted(entry, pipe, root):
    """True se la voce di sys.path (gia' normalizzata) sta dove un agente puo' scrivere: la cartella del pipeline o QUALUNQUE punto del repository.
    R4 (D6): nemmeno un ambiente Python (.venv) dentro il repository e' fidato: i suoi site-packages sono scrivibili dall'agente. Se nel cloud Python girasse da li',
    PyYAML non si troverebbe e la spesa sarebbe NEGATA (errore sicuro, visibile con la sonda): non si accetta in silenzio."""
    return _inside(entry, pipe) or _inside(entry, root)


def _verified_bytes(path, pins):
    """Byte del modulo (fini riga LF). Con `pins` (cloud): file regolare (non collegamento) e sha256 intero tra quelli attesi, altrimenti RuntimeError."""
    p = pathlib.Path(path)
    if pins is not None and (p.is_symlink() or not p.is_file()):
        raise RuntimeError(f"{p.name} manca o non e' un file .py regolare (cartella, collegamento o assente)")
    data = p.read_bytes()
    if pins is not None:
        got = _lf_sha256(data)
        if got not in pins:
            raise RuntimeError(f"{p.name} ha hash {got[:12]}, atteso {' o '.join(x[:12] for x in pins)}: file modificato o hook non aggiornato")
    return data.replace(b"\r\n", b"\n")


class _ChainLoader(importlib.machinery.SourceFileLoader):
    """Carica un modulo del pipeline DAI BYTE LETTI (e, con `pins`, verificati): mai da __pycache__ o da un .pyc, che qualcuno potrebbe aver preparato."""
    def __init__(self, fullname, path, pins=None):
        super().__init__(fullname, path)
        self._pins = pins

    def get_code(self, fullname):
        return compile(_verified_bytes(self.path, self._pins), self.path, "exec", dont_inherit=True)


class _ImportBarrier(importlib.abc.MetaPathFinder):
    """Sostituisce PathFinder in sys.meta_path. Nome di primo livello: se e' uno dei PIPELINE_MODULES si prende dalla cartella del pipeline (file .py regolare,
    caricatore verificato); altrimenti si cerca SOLO nelle voci di sys.path fuori dal repository. Un file o una cartella piantati nel repository con il nome
    di un modulo standard o di una libreria non sono mai nemmeno guardati. I sottomoduli seguono il pacchetto genitore."""
    def __init__(self, pipeline_dir, root):
        self.pipe_dir = pathlib.Path(pipeline_dir)
        self.pipe, self.root = _norm(str(pipeline_dir)), _norm(str(root))
        # R4 (D4): sono fidate SOLO le voci che c'erano nel percorso di import quando la barriera e' stata installata (quelle che Python stesso ha messo all'avvio:
        # libreria standard, site-packages, e con `-I` nient'altro) E che stanno fuori dal repository. Una voce AGGIUNTA dopo da codice (`vp_common` e `cost_ledger`
        # fanno insert(0, pipeline); `approve_spend.load_cost_ledger` aggiunge in fondo `<cartella sopra il repo>/scripts/video-pipeline`, fuori dal repo ma scrivibile
        # dall'agente) non e' mai fidata.
        self.initial = set()
        for e in sys.path:
            try:
                if isinstance(e, str):
                    self.initial.add(_norm(e or os.getcwd()))
            except (OSError, ValueError):
                pass
        self._cache = {}

    def _entry_ok(self, e):
        """True se la voce `e` (una cartella di un pacchetto: sottocartella di una voce iniziale) e' fidata."""
        try:
            ne = _norm(e or os.getcwd())
            return any(_inside(ne, i) for i in self.initial) and not _is_untrusted(ne, self.pipe, self.root)
        except (OSError, ValueError):
            return False

    def trusted_path(self):
        out = []
        for e in sys.path:
            if not isinstance(e, str):
                continue
            r = self._cache.get(e)
            if r is None:
                try:
                    ne = _norm(e or os.getcwd())
                    r = ne not in self.initial or _is_untrusted(ne, self.pipe, self.root)
                except (OSError, ValueError):
                    r = True
                self._cache[e] = r
            if not r:
                out.append(e)
        return out

    def find_spec(self, fullname, path=None, target=None):
        if path is not None:
            # R4 (D3): anche i SOTTOMODULI si cercano solo nelle voci fidate. Il `__path__` di un pacchetto namespace (o con extend_path) si ricalcola su TUTTO
            # sys.path, pipeline compreso: senza questo filtro un `pacchetto/b.py` piantato nel pipeline veniva importato.
            try:
                safe = [e for e in list(path) if isinstance(e, str) and self._entry_ok(e)]
            except Exception:                                          # noqa: BLE001 - un __path__ illeggibile non si cerca affatto
                safe = []
            return importlib.machinery.PathFinder.find_spec(fullname, safe, target)
        if fullname in PIPELINE_MODULES:
            f = self.pipe_dir / (fullname + ".py")
            if not f.is_file():
                raise ModuleNotFoundError(f"No module named {fullname!r} (manca {f.name} nella cartella del pipeline)", name=fullname)
            pins = None if APPROVAL_BACKEND == "file" else CHAIN_PINS[fullname]
            return importlib.util.spec_from_file_location(fullname, str(f), loader=_ChainLoader(fullname, str(f), pins))
        return importlib.machinery.PathFinder.find_spec(fullname, self.trusted_path(), target)

    def find_distributions(self, context=None):
        """`importlib.metadata` cerca le distribuzioni fra i cercatori di sys.meta_path che hanno questo metodo: PathFinder lo aveva e, sostituito, nessun pacchetto
        avrebbe piu' i suoi metadati (PackageNotFoundError). Qui la ricerca e' limitata ai percorsi fidati (un *.dist-info piantato nel repo non si vede)."""
        try:
            import importlib.metadata as md
        except ImportError:
            return iter(())
        name = getattr(context, "name", None) if context is not None else None
        return md.MetadataPathFinder.find_distributions(md.DistributionFinder.Context(name=name, path=self.trusted_path()))

    def invalidate_caches(self):
        importlib.machinery.PathFinder.invalidate_caches()           # importlib.invalidate_caches() deve continuare a svuotare le cache dei percorsi


def install_import_barrier():
    """Installa la barriera al posto di PathFinder (solo quando l'hook gira come programma: vedi in fondo). Solleva se non riesce."""
    barrier = _ImportBarrier(PIPELINE_DIR, ROOT)
    i = next((k for k, f in enumerate(sys.meta_path) if f is importlib.machinery.PathFinder), None)
    if i is None:
        raise RuntimeError("PathFinder non trovato in sys.meta_path")
    sys.meta_path[i] = barrier
    return barrier


def _disk_root(nt=None):
    """Disco da controllare: C: su Windows (come sempre), il disco del repository altrove (R4, D6: nel cloud Linux "C:/" solleva e il controllo non faceva nulla)."""
    return "C:/" if (os.name == "nt" if nt is None else nt) else str(ROOT)


def _tag():
    """Segno nei messaggi di rifiuto quando il backend non e' 'file' (serve anche a PROVARE nel cloud che la variabile arrivi all'hook)."""
    if APPROVAL_BACKEND == "file":
        return ""
    return f"[approvazione: {APPROVAL_BACKEND if APPROVAL_BACKEND in BACKENDS else 'SCONOSCIUTA'}] "


def _over_cap_msg():
    if APPROVAL_BACKEND == "file":
        return ("BLOCCATO - TETTO CUMULATIVO: manca una riga `spesa-oltre-tetto` valida in approvazioni.md per questa card "
                "(id completo, importo massimo, scadenza entro oggi + 2 giorni): la scrive la sessione principale solo dopo l'ok "
                "esplicito di Cipriano in chat; altrimenti lo lancia lui dal terminale.")
    return (f"BLOCCATO - {_tag()}TETTO CUMULATIVO: --allow-over-cap non e' consentito nel cloud (nessuna riga puo' autorizzarlo): "
            "lo sforo del tetto per contenuto lo lancia solo Cipriano dal terminale del PC.")


def _official_path_hint():
    if APPROVAL_BACKEND == "file":
        return "run.py / 01_generate.py con --brief e riga `spesa-video` in approvazioni.md"
    return "run.py / 01_generate.py con --brief e una proposta approvata da Cipriano nel DB 'Approvazioni'"


def _spend_tail():
    if APPROVAL_BACKEND == "file":
        return "Senza una riga `spesa-video` valida in approvazioni.md la generazione la lancia Cipriano dal terminale."
    return "Senza una proposta approvata da Cipriano nel DB 'Approvazioni' la generazione la lancia Cipriano dal terminale."


def log(decision, cmd):
    """Traccia di audit: ogni decisione del guard (solo ask/deny) con comando troncato."""
    try:
        import datetime, pathlib
        p = pathlib.Path(__file__).with_name("spend-guard.log")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {decision} {cmd[:200]!r}\n")
    except Exception:                      # noqa: BLE001
        pass


def out(decision, reason, cmd=""):
    log(decision, cmd)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                              "permissionDecision": decision,
                                              "permissionDecisionReason": reason}}, ensure_ascii=False))
    sys.exit(0)


def _rome_today(now=None):
    """Data di oggi a ROMA (AAAA-MM-GG), anche se l'orologio e' in UTC. Senza tzdata solleva: il ramo della spesa nega."""
    return (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(zoneinfo.ZoneInfo("Europe/Rome")).date().isoformat()


def valid_spend_rows(today=None, kind="spesa-video"):
    """Righe non scadute di approvazioni.md del tipo `kind` (`spesa-video`, o `spesa-oltre-tetto` dal 20/10/2026):
    lista di (id card 32 hex, importo massimo USD)."""
    today = today or _rome_today()
    rows = []
    for line in APPROVALS.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() != kind:
            continue
        d = re.search(r"\d{4}-\d{2}-\d{2}", cells[2])
        cid = HEX32.search(cells[1].lower().replace("-", ""))
        amt = re.search(r"max\s*([0-9]+(?:\.[0-9]+)?)", cells[1], re.I)
        limit = (datetime.date.fromisoformat(today) + datetime.timedelta(days=2)).isoformat()   # orizzonte massimo: oggi + 2 giorni
        if d and today <= d.group(0) <= limit and cid and amt:
            rows.append((cid.group(0), float(amt.group(1))))
    return rows


def _pipeline_on_path():
    """Mette la cartella del pipeline in FONDO a sys.path (mai in testa): un file della cartella con il nome di un modulo standard (hashlib.py,
    yaml.py...) non deve poter prendere il posto di quello vero. I moduli del pipeline (vp_common, spend_gate, cost_ledger) hanno nomi unici."""
    d = str(PIPELINE_DIR)
    if d not in sys.path:
        sys.path.append(d)


def _lf_sha256(data):
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _shadow_problems():
    """Elenco di cose nella cartella del pipeline che possono "coprire" un modulo importato dall'hook: una cartella con quel nome (in Python una
    cartella-pacchetto vince sul .py omonimo) o un'estensione compilata/.pyc senza sorgente (.so/.pyd vincono sul .py). Vuoto = nulla di sospetto."""
    out = []
    suffixes = {s for s in importlib.machinery.all_suffixes() if s != ".py"} | {".pyd", ".pyw", ".pyc", ".so"}
    # R3: anche i suffissi di Linux (.cpython-311-x86_64-linux-gnu.so, .abi3.so) e di Windows (.cp311-win_amd64.pyd), qualunque versione di Python
    compiled_tail = re.compile(r"\.(?:so|pyd|pyc|pyw|dll|dylib)$|\.(?:cpython|cp\d|pypy|abi3)", re.I)
    try:
        names = {e.name: e.is_dir() for e in os.scandir(PIPELINE_DIR)}
    except OSError as e:
        return [f"cartella del pipeline non leggibile ({type(e).__name__})"]
    for m in MODULES_NO_SHADOW:
        if names.get(m) is not None:
            out.append(f"'{m}' esiste come {'cartella' if names[m] else 'file senza estensione'}")
        for nm in sorted(names):
            tail = nm[len(m):]
            if nm.startswith(m + ".") and tail != ".py" and (tail in suffixes or compiled_tail.search(tail)):
                out.append(f"'{nm}' puo' coprire {m}.py")
    return out


def _load_pinned_chain():
    """Cloud (backend 'notion'): carica cost_ledger, spend_gate, notion_client e vp_common dai byte verificati (hash atteso in CHAIN_PINS) e li registra in
    sys.modules PRIMA di ogni import: `import vp_common`, `import cost_ledger as cl` di approve_spend e `import notion_client` trovano questi, mai altro.
    Un file modificato, un collegamento, una cartella o un'estensione compilata con quel nome = RuntimeError (la spesa e' negata)."""
    problems = _shadow_problems()
    if problems:
        raise RuntimeError("nella cartella del pipeline c'e' qualcosa che puo' coprire un modulo della catena: " + "; ".join(problems))
    for name in PIPELINE_MODULES:
        path = str(PIPELINE_DIR / (name + ".py"))
        loader = _ChainLoader(name, path, CHAIN_PINS[name])
        spec = importlib.util.spec_from_file_location(name, path, loader=loader)
        old = sys.modules.pop(name, None)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(name, None)
            if old is not None:
                sys.modules[name] = old
            raise


def _approve_spend():
    """Il modulo approve_spend del pacchetto cloud, caricato PER PERCORSO DI FILE (mai `import approve_spend`: una cartella-pacchetto omonima
    avrebbe la precedenza). Condizioni, tutte necessarie: nulla che copra i moduli (`_shadow_problems`), `approve_spend.py` e' un file regolare
    (non un collegamento, non una cartella), il suo contenuto ha l'hash atteso. Il file si legge UNA volta, si verifica e si esegue dai byte verificati
    (nessuna finestra tra controllo e uso). Qualunque problema = RuntimeError (la spesa e' negata). Import pigro: il PC non lo tocca."""
    problems = _shadow_problems()
    if problems:
        raise RuntimeError("nella cartella del pipeline c'e' qualcosa che puo' coprire un modulo dell'hook: " + "; ".join(problems))
    path = PIPELINE_DIR / "approve_spend.py"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("approve_spend.py manca o non e' un file .py regolare (cartella, collegamento o assente)")
    data = path.read_bytes()
    got = _lf_sha256(data)
    if got != APPROVE_SPEND_SHA256:
        raise RuntimeError(f"approve_spend.py ha hash {got[:12]}, atteso {APPROVE_SPEND_SHA256[:12]}: file modificato o hook non aggiornato")
    mod = types.ModuleType("approve_spend")
    mod.__file__ = str(path)
    _pipeline_on_path()
    exec(compile(data.replace(b"\r\n", b"\n"), str(path), "exec"), mod.__dict__)
    return mod


def _brief_model(brief):
    """(modello, problema) dal brief per il confronto con la proposta. Solo nel backend notion. `sourcing` assente o senza modello,
    o non dizionario = problema (NEGATO): il cloud non indovina il modello."""
    src = brief.get("sourcing") if isinstance(brief, dict) else None
    if not isinstance(src, dict):
        return None, "il brief non ha un 'sourcing' (dizionario) con il campo 'model': il cloud lo confronta con la proposta"
    m = src.get("model")
    if not isinstance(m, str) or not m.strip():
        return None, "il brief non ha sourcing.model: il cloud lo confronta con la proposta"
    return m.strip(), None


def card_approval_reason(cid, max_usd, brief=None, pages=None, today=None):
    """None se la card e' approvata per `max_usd` secondo il backend attivo (file sul PC, notion nel cloud); altrimenti il motivo.
    `pages` e `today` servono solo alla prova (righe Notion finte); in produzione il cloud le scarica da solo. Backend sconosciuto = motivo (NEGATO)."""
    if APPROVAL_BACKEND == "notion":
        model, problem = _brief_model(brief)
        if problem is not None:
            return f"approvazione cloud (DB Approvazioni) negata: {problem}"
        try:
            module = _approve_spend()
        except RuntimeError as e:             # modulo non affidabile: NEGATO con il motivo (mai sul file)
            return f"approvazione cloud (DB Approvazioni) negata: modulo approve_spend non affidabile: {e}"
        d = module.check(cid, max_usd, model=model, pages=pages, today=today)
        return None if d.ok else f"approvazione cloud (DB Approvazioni) negata: {d.reason}"
    if APPROVAL_BACKEND != "file":
        return f"SPEND_GUARD_APPROVAL_BACKEND sconosciuto ({APPROVAL_BACKEND!r}): ammessi 'file' o 'notion'; nel dubbio la spesa e' negata"
    rows = [amt for (rid, amt) in valid_spend_rows() if rid == cid]
    if not rows:
        return f"nessuna approvazione valida 'spesa-video' per la card {cid} in approvazioni.md (la scrive la sessione principale dopo l'ok di Cipriano)"
    if max_usd > max(rows) + 1e-9:
        return f"--max-usd {max_usd:.2f} supera l'importo approvato ({max(rows):.2f} USD) per questa card"
    return None


def spent_today_usd(now=None):
    """Spesa di oggi: DELEGA SEMPRE (anche in modo locale) a spend_gate.spent_today_usd, la stessa funzione del cancello nel codice
    (costs.json + spese pagate e non registrate `.unrecorded-*`, o registro condiviso in modo notion). Nessun try/except: ogni eccezione
    sale a check_spend/main, che NEGA ("errore nei controlli")."""
    _pipeline_on_path()
    import spend_gate
    return spend_gate.spent_today_usd(now)


def check_spend(cmd, cwd=""):
    """None se la spesa e' consentita; altrimenti il motivo del rifiuto. Qualsiasi eccezione = rifiuto (lo gestisce il chiamante)."""
    occ = [k for k in flag_keys(cmd) if k == "--max-usd"]
    if len(occ) != 1:
        return f"--max-usd compare {len(occ)} volte: ne serve esattamente una"
    m = re.search(r"--max-usd(?:=|\s+)[\"']?([^\s\"']+)[\"']?", cmd)
    if not m or not re.fullmatch(r"\d+(?:\.\d+)?", m.group(1)):
        return f"importo --max-usd non valido ({m.group(1) if m else 'mancante'}): solo numeri semplici, per esempio 1.50"
    max_usd = float(m.group(1))
    if len(BRIEF_SCRIPTS.findall(cmd)) != 1 or len(PAID.findall(cmd)) != 1:
        return "sono sbloccati solo run.py, 01_generate.py e 00_scene_master.py, uno per comando (cover, voce, batch e altri restano al terminale di Cipriano)"
    bm = re.findall(r"--brief[ =]+(\"[^\"]+\"|'[^']+'|\S+)", cmd)
    if len(bm) != 1:
        return "serve esattamente un --brief"
    raw = bm[0].strip("\"'")
    cands = [pathlib.Path(raw)] + ([pathlib.Path(cwd) / raw] if cwd else []) + [PIPELINE_DIR / raw]
    path = next((p for p in cands if p.is_file()), None)
    if path is None:
        return f"brief non trovato: {raw}"
    _pipeline_on_path()
    if APPROVAL_BACKEND == "notion":                  # R3: catena di spesa dai byte verificati, PRIMA di qualunque import
        try:
            _load_pinned_chain()
        except Exception as e:                         # noqa: BLE001 - qualunque problema: NEGATO con il motivo
            return f"approvazione cloud (DB Approvazioni) negata: modulo della catena di spesa non affidabile: {type(e).__name__}: {str(e)[:300]}"
    import yaml, vp_common
    brief = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cid = vp_common.norm_id(brief.get("notion_card_id"))
    if len(cid) != 32:
        return "il brief non ha un notion_card_id completo (32 caratteri)"
    reason = card_approval_reason(cid, max_usd, brief=brief)      # P2c-bis: fonte per backend (file sul PC, notion nel cloud)
    if reason is not None:
        return reason
    retry = ("--regenerate" in cmd) or vp_common.card_spent_usd(brief) > 0
    cap = DAILY_CAP_USD if retry else NEW_GEN_CAP_USD
    today = spent_today_usd()
    if today + max_usd > cap + 1e-9:
        return (f"tetto giornaliero: gia' spesi oggi {today:.2f} USD + {max_usd:.2f} richiesti > {cap:.2f} USD "
                f"({'tentativo ripetuto' if retry else 'prima generazione: 2 USD di riserva per i tentativi'})")
    return None


def over_cap_row_ok(cmd, cwd=""):
    """Dal 20/10/2026 (decisione di Cipriano in chat): `--allow-over-cap` passa SOLO se in `approvazioni.md` c'e' una riga valida
    `spesa-oltre-tetto` per l'id completo della card del brief e `--max-usd` (uno solo, numero semplice) non supera l'importo della riga.
    Restano comunque il controllo `spesa-video` e il tetto giornaliero (check_spend). Qualsiasi dubbio o errore = False (nega).
    NEL CLOUD (backend diverso da 'file') e' sempre False: lo sforo del tetto per contenuto resta azione dal terminale di Cipriano."""
    if APPROVAL_BACKEND != "file":
        return False
    try:
        m = re.search(r"--max-usd(?:=|\s+)[\"']?(\d+(?:\.\d+)?)[\"']?", cmd)
        if not m or len([k for k in flag_keys(cmd) if k == "--max-usd"]) != 1 or len([k for k in flag_keys(cmd) if k == "--allow-over-cap"]) != 1:
            return False
        if len(BRIEF_SCRIPTS.findall(cmd)) != 1 or len(PAID.findall(cmd)) != 1:
            return False
        bm = re.findall(r"--brief[ =]+(\"[^\"]+\"|'[^']+'|\S+)", cmd)
        if len(bm) != 1:
            return False
        raw = bm[0].strip("\"'")
        cands = [pathlib.Path(raw)] + ([pathlib.Path(cwd) / raw] if cwd else []) + [PIPELINE_DIR / raw]
        path = next((p for p in cands if p.is_file()), None)
        if path is None:
            return False
        _pipeline_on_path()
        import yaml, vp_common
        brief = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cid = vp_common.norm_id(brief.get("notion_card_id"))
        if len(cid) != 32:
            return False
        amts = [amt for (rid, amt) in valid_spend_rows(kind="spesa-oltre-tetto") if rid == cid]
        return bool(amts) and float(m.group(1)) <= max(amts) + 1e-9
    except Exception:                      # noqa: BLE001 - nel ramo della spesa un errore NEGA
        return False


def run_with_deadline(fn, seconds):
    """Esegue fn() in un thread e aspetta al massimo `seconds`: TimeoutError se non finisce. Il thread e' 'daemon': il processo dell'hook puo' uscire
    anche se resta bloccato in una richiesta di rete. Le eccezioni di fn() risalgono invariate."""
    box = {}

    def target():
        try:
            box["r"] = fn()
        except BaseException as e:               # noqa: BLE001
            box["e"] = e
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise TimeoutError(f"tempo massimo del controllo ({seconds:.0f} s) esaurito")
    if "e" in box:
        raise box["e"]
    return box.get("r")


def main():
    try:
        ev = json.load(sys.stdin)
        if ev.get("tool_name") not in ("Bash", "PowerShell"):   # esteso a PowerShell il 16/10/2026 (falla trovata dalla coda in ombra)
            return
        cmd = (ev.get("tool_input") or {}).get("command", "") or ""
        if PAID.search(cmd) and _BARRIER_STATE["error"] and ("--max-usd" in cmd or "--allow-over-cap" in cmd):
            out("deny", f"BLOCCATO - {_tag()}SPESA A PAGAMENTO: la barriera di importazione dell'hook non e' installata ({_BARRIER_STATE['error']}): "
                       "nel dubbio NEGATO. " + _spend_tail(), cmd)
        if PAID.search(cmd) and (has_prefix_of(cmd, "--allow-over-cap") or has_prefix_of(cmd, "--max-usd")):
            out("deny", "BLOCCATO - OPZIONE ABBREVIATA: --max, --allow-over e simili sono abbreviazioni di --max-usd e --allow-over-cap. "
                       "Scrivi le opzioni per intero (e --allow-over-cap la lancia solo Cipriano).", cmd)
        if "--allow-over-cap" in cmd and PAID.search(cmd):
            if not over_cap_row_ok(cmd, ev.get("cwd") or ""):
                out("deny", _over_cap_msg(), cmd)
            log("allow-over-cap (riga spesa-oltre-tetto valida)", cmd)
        if launches_ungated(cmd):
            out("deny", f"BLOCCATO - {_tag()}RUNNER SENZA CANCELLO DI SPESA (runner di prova, run_kie_ref_look, voice_*): chiama l'API a pagamento senza --max-usd e senza "
                       f"approval_gate. Usa il pipeline ufficiale ({_official_path_hint()}) "
                       "oppure lo lancia Cipriano dal terminale. Il percorso del lab con cancello (lab_run.py) e' da costruire (Sistema/lab/LAB.md).", cmd)
        if "--max-usd" in cmd and PAID.search(cmd):
            m = re.search(r"--max-usd(?:=|\s+)[\"']?([^\s\"']+)", cmd)
            try:
                if APPROVAL_BACKEND == "file":                 # PC: nessun cambiamento (letture locali, nessuna rete)
                    reason = check_spend(cmd, ev.get("cwd") or "")
                else:                                          # cloud: tempo massimo TOTALE, poi NEGATO da solo (non si aspetta il timeout dell'hook)
                    reason = run_with_deadline(lambda: check_spend(cmd, ev.get("cwd") or ""), TOTAL_DEADLINE_S)
            except TimeoutError as exc:
                reason = f"{exc}: nel dubbio NEGATO"
            except Exception as exc:       # noqa: BLE001 - nel ramo della spesa un errore NEGA
                reason = f"errore nei controlli ({type(exc).__name__}: {str(exc)[:80]})"
            if reason is None:
                log("allow-spend", cmd)
            else:
                out("deny", f"BLOCCATO - {_tag()}SPESA A PAGAMENTO (--max-usd {m.group(1) if m else '?'}): {reason}. " + _spend_tail(), cmd)
        if HEAVY.search(cmd):
            free = shutil.disk_usage(_disk_root()).free / 1e9      # R4 (D6): su Linux "C:/" non esiste e il controllo non faceva nulla
            if free < MIN_FREE_GB:
                out("deny", f"DISCO {'C:' if os.name == 'nt' else '(disco del repository)'}: quasi pieno ({free:.2f} GB liberi, minimo {MIN_FREE_GB} GB). "
                            "Libera spazio prima di eseguire lavori che scrivono media o scaricano modelli.", cmd)
    except Exception:                      # noqa: BLE001 - mai bloccare per un errore del hook
        return


if __name__ == "__main__":
    try:
        install_import_barrier()
    except Exception as _exc:              # noqa: BLE001 - senza barriera il ramo della spesa NEGA (vedi main)
        _BARRIER_STATE["error"] = f"{type(_exc).__name__}: {str(_exc)[:80]}"
    main()
