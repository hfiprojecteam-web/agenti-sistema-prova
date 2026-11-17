#!/usr/bin/env python
"""
protected-zone-guard.py - hook PreToolUse (Write|Edit|MultiEdit|Read|Bash|PowerShell). Zona protetta degli agenti (17/10/2026).
VERSIONE R2 31/10/2026 (staging, NON installata): corregge G1 (falsi positivi sul comando legittimo propose_spend.py) e G2 (moduli
sostituibili con una cartella-pacchetto, percorsi con `..`, euristica shell piu' ampia).

Vale SOLO per le chiamate dei sub-agenti (l'evento contiene agent_id): la sessione principale, cioe' Cipriano
o chi lavora per sua richiesta, non viene mai bloccata. VARIANTE B: tranne nel cloud (SPEND_GUARD_APPROVAL_BACKEND=notion), dove vale anche per la sessione principale. Verificato il 17/10/2026 con una sonda: le chiamate dei
sub-agenti (anche in background) portano agent_id e agent_type, quelle della sessione principale no.

Un sub-agente non puo' scrivere (o, per .env, leggere):
- .claude/hooks/*  e  .claude/settings*.json (hook di sicurezza, permessi)
- .claude/agents/direttore.md  e  .claude/agents/verificatore.md (le approva solo Cipriano)
- file .env (non .env.example)
- (31/10/2026, punto 1c) i moduli che l'hook di spesa importa e che decidono/scrivono le approvazioni nel video pipeline:
  approve_spend, propose_spend, cost_ledger, spend_gate, vp_common, notion_client, sia come file (.py, .pyc, .so, .pyd) SIA COME CARTELLA-PACCHETTO
  con quel nome (in Python una cartella-pacchetto ha la precedenza sul modulo .py omonimo), in QUALUNQUE percorso: il percorso si normalizza
  prima (maiuscole/minuscole, backslash, `.` e `..`, percorsi relativi con la cwd dell'evento). Eccezione: i percorsi sotto `/backup/` e
  `/archivio/` (copie di sicurezza e archivio non sono su nessun percorso di import); valgono solo per i moduli, non per hook e settings.
- il tetto di spesa nel video pipeline: modifiche che toccano budget_cap / approval_gate / content_cap_usd / allow_over_cap / max-usd

Per Bash/PowerShell il controllo e' un'euristica STRUTTURALE (non piu' "parola protetta + parola di scrittura ovunque nel testo"):
il comando si divide in comandi semplici (rispettando virgolette, here-doc, &&, ||, ;, |, a capo) e si nega SOLO se il BERSAGLIO di una
scrittura e' un percorso protetto: il bersaglio di una redirezione (> >> 2>), l'argomento di rm/tee/touch/mkdir/Set-Content/Out-File/ln/patch/sed -i...,
la destinazione di cp/mv/Copy-Item/install, un file toccato da `git checkout --`/`restore`, o il codice di `python -c` / here-doc che contiene
un nome protetto e una scrittura. Il testo libero tra virgolette (--cosa "clip del gancio", "formato rm", "stima > 1 USD", "| tee") non e' un comando.
LANCIARE un modulo (python propose_spend.py ...), leggerlo (cat, grep, ls, diff) o copiarlo DA protetto VERSO un backup NON e' una scrittura sul modulo.
NON e' a prova di aggiramento (variabili, cicli, nomi costruiti a pezzi, uno script scritto altrove e poi lanciato): e' una rete di sicurezza
contro gli errori e contro le mosse ovvie. Il blocco vero e' sulle scritture dirette (Write/Edit). Se l'analisi strutturale va in errore,
si ripiega sul vecchio controllo (piu' severo). Qualsiasi altro errore interno: lascia passare (exit 0).

R3 (31/10/2026, terzo giro del verificatore): (a) un file o una cartella con il NOME DI UN MODULO STANDARD o di una libreria (logging.py, argparse/,
yaml.py...) nel livello alto della cartella del pipeline non si scrive (puo' coprire il modulo vero: la difesa vera e' la barriera di importazione
dell'hook di spesa, questa e' difesa in profondita'); (b) suffissi Linux/pypy/abi3 dei moduli (.cpython-311-x86_64-linux-gnu.so, .abi3.so); (c) euristica
shell piu' larga senza nuovi falsi positivi: sed 'w file', awk con redirezione, editor (vim/nano...), estrazione di archivi (tar -x, unzip, Expand-Archive)
dentro una cartella con moduli/hook o che li contiene, copia del CONTENUTO di una cartella o di un glob dentro il pipeline (cp -r dir/., rsync dir/),
git checkout <rev> -- <cartella>, git apply / patch con una patch che tocca un percorso protetto, pip install --user/--target/--prefix/--root,
python -c con nome spezzato ('approve_'+'spend.py'), codice in linea che decodifica (base64, hex) ed esegue, `base64 -d | sh`, e le scritture .NET
([IO.File]::WriteAllText...) dallo strumento PowerShell diretto. RESTA aggirabile (variabili che compongono il nome, script scritto altrove e poi lanciato, cicli,
git merge/pull/reset --hard che portano revisioni, altri interpreti): la difesa in profondita' vera e' l'hash controllato dall'hook, non questa euristica.

R4 (31/10/2026, quarto giro del verificatore + scelta di Cipriano sul canale di scrittura): (D1) cartelle di Python = zona protetta: user site, site-packages,
dist-packages, libreria standard (`lib/pythonX.Y`), `usercustomize`/`sitecustomize`, qualunque `*.pth`; la scrittura nel user site (e `PIP_USER=1`, `PIP_TARGET`, `PIP_PREFIX`,
`PIP_ROOT`, `PYTHONUSERBASE`, `pip config set`, `--system`, `--break-system-packages`) e' negata; un comando che chiede a Python dove stanno quelle cartelle (`--user-site`,
`getusersitepackages`...) E contiene una scrittura e' negato. La difesa vera e' `python3 -I` nel comando dell'hook (settings.json.proposto R4), non questa euristica.
(D5) patch scritta e applicata nello stesso comando (o ricevuta da una pipe) negata; `New-Item -ItemType Junction/SymbolicLink/HardLink` verso il pipeline o gli hook negato; percorsi
8.3 di Windows (`VIDEO-~1`, `APPROV~1.PY`) riportati al nome vero prima del controllo. (Canale di scrittura sulle schede) nel cloud (SPEND_GUARD_APPROVAL_BACKEND=notion) le
scritture DIRETTE su Notion fuori da `write_card.py` e `propose_spend.py` sono negate: `curl`/`Invoke-RestMethod` con metodo di scrittura verso api.notion.com, codice in linea con
richieste di scrittura a Notion, `notion_client.py set-fase`, e gli strumenti di scrittura del connettore Notion (create/update/delete/move/duplicate/comment/file upload).
Lanciare `write_card.py`, `propose_spend.py` o `read_card.py` NON e' una scrittura diretta e passa. Sul PC (variabile assente) quest'ultima regola non c'e'.

R5 (01/11/2026, quinto giro del verificatore; decisione di Cipriano del 01/11 su D-1: accettato SOLO sul database di prova):
(D-1) l'analisi delle scritture dirette su Notion e' un'EURISTICA SUL TESTO del comando (e dei file di script che il comando lancia), NON un confine di sicurezza: chiude le forme
che il verificatore ha trovato (curl con `--request=PATCH`, `-d'...'` attaccato, `--json=`, `-G`, maiuscole/minuscole delle opzioni corte, gruppi `-sSfd`, host in una variabile, `-K file`,
wget `--method=`/`--body-data=`, httpie con campi dati, PowerShell `-Method:Patch`, splatting, WebClient/HttpClient, codice in linea con verbo tra virgolette o `data=`/`json=`/secondo argomento
di urlopen/Request, `deno eval`, Net::HTTP::Patch, script scritto e lanciato nello stesso comando, script GIA' scritto in un file che il comando lancia, `echo ... | sh`) ma un agente manipolato
puo' sempre costruire nomi a pezzi, cifrare il testo o usare un altro programma. Il confine vero e' che la chiave del bot non sia raggiungibile dall'agente (canale fuori dal contenitore,
punto 66). (D-4) nel cloud gli strumenti del connettore Notion passano SOLO se sono letture (lista BIANCA: fetch, search, get-*, query-*, list-*, retrieve-*, read-*, post-search, post-database-query; tutto il resto,
nomi nuovi compresi, e' negato); un errore interno del controllo nel cloud NEGA (prima lasciava passare). (G1) la scrittura di un file `python`/`python3...` in una cartella `bin` e' zona protetta.
(M-2) `curl -f -s` (fail, in minuscolo) non e' piu' scambiato per `-F` (form).

R7 (01/11/2026, settimo giro del verificatore, V7-G1 GRAVE): la guardia nuova lasciava passare 49 mosse ordinarie che l'installata nega (`cd .claude/hooks && cp ...`, `H=...; cp /tmp/x $H`,
`sh -c 'cp /tmp/x "$1"' _ ...`, `cp .env /tmp/e`...). CORREZIONE PER COSTRUZIONE: la regola della guardia installata, riprodotta alla lettera (`floor_reason`), gira PRIMA di tutta l'analisi e
sullo stesso testo; l'analisi strutturale aggiunge negazioni, non ne toglie. Quindi: "se l'installata nega, anche questa nega" vale per ogni evento, anche mai visto. Conseguenza dichiarata: i
falsi positivi che l'installata ha oggi (es. `grep -rn cp .claude/hooks/`, `find .claude/hooks -name '*.py' -exec cp {} /tmp/bk/ \\;`) tornano, ne' piu' ne' meno. (V7-M1) `_peel` legge anche
il VALORE delle opzioni dei wrapper (`sudo -u root`, `timeout -s KILL 5`, `env -u FOO`, `nice -n 5`, `ionice -c 3`...).
"""
import fnmatch, json, os, posixpath, re, sys, datetime, pathlib

MODULES = ("approve_spend", "propose_spend", "write_card", "read_card", "cost_ledger", "spend_gate", "vp_common", "notion_client")      # R4: write_card e read_card decidono cosa si scrive/legge sulle schede
_MOD = "|".join(MODULES)
# Un modulo: file (.py/.pyc/.pyd/.pyw/.so/.cpython-*.so/.cp311-*.pyd) OPPURE cartella-pacchetto con quel nome, in qualunque percorso normalizzato.
MODULE_PATH = re.compile(r"(?:^|/)(?:%s)(?:\.py[cdwo]?|\.so|\.pyd|\.(?:cpython|cp\d|abi3|pypy)[\w.\-]*)?(?:/|$)" % _MOD)
# Copie di sicurezza e archivio: nessun import puo' arrivare da li' (il percorso di import dell'hook e' fisso).
SAFE_TREE = re.compile(r"/(?:backup|archivio)/")
PROTECTED_PATH = re.compile(
    r"(/\.claude/hooks(?:/|$)|/\.claude/settings[^/]*$|/\.claude/agents/(?:direttore|verificatore)\.md$|"
    r"/sistema/architettura-agenti/(?:approvazioni|lista-bianca-comunicazioni)\.md$)")
SHELL_EXTRA = re.compile(r"(?:^|/)(?:approvazioni|lista-bianca-comunicazioni)\.md$")
# R3 (G-A): nomi di moduli standard e di librerie. Un file o una cartella con quel nome nel LIVELLO ALTO della cartella del pipeline puo' coprire il modulo
# vero per chi lo importa. Difesa in profondita': la difesa vera e' la barriera di importazione dell'hook di spesa (spend-guard.py R3).
_STATIC_STD = ("abc argparse array ast asyncio base64 bisect builtins bz2 calendar cgi cmd code codecs collections colorsys concurrent configparser contextlib "
               "copy csv ctypes dataclasses datetime decimal difflib dis email encodings enum errno faulthandler filecmp fnmatch fractions ftplib functools gc "
               "getopt getpass gettext glob gzip hashlib heapq hmac html http imaplib importlib inspect io ipaddress itertools json keyword linecache locale "
               "logging lzma mimetypes multiprocessing netrc numbers operator optparse os pathlib pdb pickle pkgutil platform plistlib poplib posixpath pprint "
               "profile pstats pty pwd queue quopri random re reprlib resource sched secrets select selectors shelve shlex shutil signal site smtplib socket "
               "socketserver sqlite3 ssl stat statistics string stringprep struct subprocess sys sysconfig tarfile telnetlib tempfile textwrap threading time "
               "timeit token tokenize tomllib trace traceback tracemalloc types typing unicodedata unittest urllib uuid venv warnings wave weakref webbrowser "
               "xml xmlrpc zipapp zipfile zipimport zlib zoneinfo test this antigravity sitecustomize usercustomize")
_THIRD_PARTY = "yaml requests tzdata urllib3 certifi idna charset_normalizer six dateutil setuptools pkg_resources pip numpy pil"
STD_NAMES = frozenset(n.lower() for n in getattr(sys, "stdlib_module_names", ())) | frozenset(_STATIC_STD.split()) | frozenset(_THIRD_PARTY.split())
STD_EXT = re.compile(r"\.py[cdwo]?|\.so|\.pyd|\.(?:cpython|cp\d|pypy|abi3)[\w.\-]*")
PIPE_TOP = re.compile(r"(?:^|/)sistema/scripts/video-pipeline/([^/]+)(?:/|$)")
ENV_FILE = re.compile(r"(^|/)\.env(\.(?!example)[^/]*)?$")
CAP_TOKENS = re.compile(r"(budget_cap|approval_gate|content_cap_usd|allow_over_cap|allow-over-cap|--max-usd|max_usd)", re.I)
PIPELINE = "/sistema/scripts/video-pipeline/"
PIPE_DIR_END = re.compile(r"(?:^|/)video-pipeline$")
# Quali sub-agenti puo' lanciare ciascun agente (le liste tra parentesi nel frontmatter di un sub-agente sono ignorate da Claude Code)
AGENT_SPAWN_ALLOWED = {"consigliere": {"analista", "assistente", "direttore", "ricercatore"}}
# Vecchio controllo (R1): resta come rete di salvataggio se l'analisi strutturale solleva un'eccezione.
CMD_PROTECTED = re.compile(r"(\.claude[/\\]hooks|(approve_spend|propose_spend|write_card|read_card|cost_ledger|spend_gate|vp_common|notion_client)\.py|\.claude[/\\]settings|[/\\]agents[/\\](direttore|verificatore)\.md|approvazioni\.md|lista-bianca-comunicazioni\.md|(^|[\s/\\\"'])\.env(?!\.example)\b)", re.I)
# Lettura dei .env via shell (cat, type, Get-Content, grep, python...): i sub-agenti conoscono solo i NOMI delle variabili (.env.example).
ENV_TOKEN = re.compile(r"(^|[\s/\\\"'])\.env(?!\.example)\b", re.I)
ENV_READ = re.compile(r"(\bcat\b|\btype\b|\bmore\b|\bless\b|\bhead\b|\btail\b|\bGet-Content\b|\bgc\b|\bSelect-String\b|\bsls\b|\bgrep\b|\brg\b|"
                      r"\bfindstr\b|\bread_text\b|\bopen\(|\bsed\b|\bawk\b|\bpython[\d.]*\s+-c\b)", re.I)
CMD_WRITE = re.compile(
    r"((?<![0-9&])>>?(?!&)|\btee\b|\bcp\b|\bmv\b|\bsed\s+-i|\bperl\s+-p?i|\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|"
    r"\bRemove-Item\b|\bMove-Item\b|\bCopy-Item\b|\bNew-Item\b|\brm\b|\bdel\b|\bwrite_text\b|\bopen\([^)]*,\s*['\"][wa]|"
    r"\bshutil\.(copy|move|rmtree)|\bos\.(remove|rename|replace)|\btruncate\b|\bdd\b)", re.I)


# ---- R7 (01/11/2026, settimo giro, V7-G1): PAVIMENTO = la regola della guardia INSTALLATA, riprodotta alla lettera.
# Proprieta' per COSTRUZIONE: se la guardia installata nega un evento, anche questa lo nega, perche' la sua regola gira PRIMA di tutta l'analisi strutturale e sullo stesso testo
# (comando grezzo, senza normalizzazione; percorso con la sola normalizzazione dell'installata). L'analisi strutturale AGGIUNGE negazioni, non ne toglie.
# Le espressioni qui sotto sono COPIE LETTERALI di quelle del file installato (`.claude/hooks/protected-zone-guard.py`, hash grezzo ccb08f1e5cc0): una prova (test_pavimento_r7.py)
# le confronta con il file installato quando e' presente. NB: INST_CMD_PROTECTED e' PIU' STRETTA di CMD_PROTECTED (non nomina i moduli del pipeline: i falsi positivi che
# l'installata ha oggi su `write_card.py ... > file` non tornano); CMD_WRITE, ENV_TOKEN, ENV_READ, ENV_FILE, CAP_TOKENS, PIPELINE sono gia' identiche alle installate.
INST_CMD_PROTECTED = re.compile(r"(\.claude[/\\]hooks|\.claude[/\\]settings|[/\\]agents[/\\](direttore|verificatore)\.md|approvazioni\.md|lista-bianca-comunicazioni\.md|(^|[\s/\\\"'])\.env(?!\.example)\b)", re.I)
INST_PROTECTED_PATH = re.compile(
    r"(/\.claude/hooks/|/\.claude/settings[^/]*$|/\.claude/agents/(direttore|verificatore)\.md$|/sistema/architettura-agenti/(approvazioni|lista-bianca-comunicazioni)\.md$)")


def inst_norm(p):
    return (p or "").replace("\\", "/").lower()


def floor_reason(tool, ti):
    """(motivo, cosa) se la guardia INSTALLATA negherebbe questo evento (stesse condizioni, stesso testo); altrimenti None. Non analizza nulla: e' il pavimento."""
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        fp = inst_norm(ti.get("file_path") or ti.get("notebook_path"))
        if INST_PROTECTED_PATH.search(fp) or ENV_FILE.search(fp):
            return "pavimento R7 = regola della guardia installata: percorso protetto", fp
        if PIPELINE in fp:
            text = " ".join(str(ti.get(k, "")) for k in ("content", "old_string", "new_string"))
            for e in ti.get("edits") or []:
                text += " " + str(e.get("old_string", "")) + " " + str(e.get("new_string", ""))
            if CAP_TOKENS.search(text) or fp.endswith("models.yaml") and "budget" in text.lower():
                return "pavimento R7 = regola della guardia installata: tetto di spesa", fp
    elif tool == "Read":
        fp = inst_norm(ti.get("file_path"))
        if ENV_FILE.search(fp):
            return "pavimento R7 = regola della guardia installata: file .env", fp
    elif tool in ("Bash", "PowerShell"):
        cmd = ti.get("command") or ""
        if INST_CMD_PROTECTED.search(cmd) and CMD_WRITE.search(cmd):
            return "pavimento R7 = regola della guardia installata: nome protetto e parola di scrittura nel comando", cmd
        if ENV_TOKEN.search(cmd) and ENV_READ.search(cmd):
            return "pavimento R7 = regola della guardia installata: lettura di un file .env", cmd
    return None


# ---- R4
CLOUD = (os.environ.get("SPEND_GUARD_APPROVAL_BACKEND") or "").strip().lower() == "notion"
# B (31/10/2026): nel cloud (SPEND_GUARD_APPROVAL_BACKEND=notion) la zona protetta vale ANCHE per la sessione principale: in una routine la
# "sessione principale" e' il Direttore stesso, che altrimenti potrebbe riscrivere approve_spend.py o l'hook. Sul PC (variabile assente) nulla cambia.
CLOUD_STRICT = CLOUD
# D1: cartelle di Python (user site, site-packages, libreria standard) e file eseguiti all'avvio di Python
PYDIR_PATH = re.compile(r"(?:^|/)(?:site-packages|dist-packages)(?:/|$)|/\.local/lib/python[\d.]*(?:/|$)|(?:^|/)(?:usercustomize|sitecustomize)(?:\.py[cdwo]?)?$|\.pth$|"
                        r"/lib(?:64)?/python[\d.]+(?:/|$)|/python3\d+/lib(?:/|$)|/appdata/roaming/python(?:/|$)|(?:^|/)bin/(?:python|pythonw|pypy)[\d.]*(?:\.exe)?$|/python3\d+\.zip$|(?:^|/)pyvenv\.cfg$")
PIP_ENV = re.compile(r"(?<![A-Za-z0-9_])(?:PIP_(?:USER|TARGET|PREFIX|ROOT|CONFIG_FILE)|PYTHONUSERBASE|UV_SYSTEM_PYTHON)\s*=", re.I)
PYDIR_QUERY = re.compile(r"--user-site|--user-base|getusersitepackages|getsitepackages|get_python_lib|sysconfig\.get_path|site\.(?:USER_|PREFIXES)|\busersite\b", re.I)
PIP_BAD_FLAGS = ("--user", "--target", "-t", "--prefix", "--root", "--system", "--break-system-packages")
LINK_TYPES = {"junction", "symboliclink", "hardlink", "symlink"}
# D5: nomi 8.3 di Windows -> nome vero (solo per i nomi che ci riguardano); (base, estensione di default)
CANON83 = (("video-pipeline", ""), ("architettura-agenti", ""), ("approvazioni", ".md"), ("lista-bianca-comunicazioni", ".md"), ("approve_spend", ".py"),
           ("propose_spend", ".py"), ("write_card", ".py"), ("read_card", ".py"), ("cost_ledger", ".py"), ("spend_gate", ".py"), ("vp_common", ".py"), ("notion_client", ".py"),
           ("spend-guard", ".py"), ("protected-zone-guard", ".py"), ("agent-permissions-guard", ".py"), ("settings", ".json"), ("direttore", ".md"), ("verificatore", ".md"))
ALIAS83 = re.compile(r"^([^~/]{1,6})~\d+(\.[^/]*)?$")
# Scritture DIRETTE su Notion (solo nel cloud)
NOTION_HOST = re.compile(r"api\.notion\.com|notion\.so/api", re.I)
NOTION_INLINE = re.compile(r"api\.notion\.com|notion_client|NOTION_API|/v1/(?:pages|blocks|databases|data_sources|file_uploads|comments)|NotionBackend|_cli_notion_kwargs", re.I)
HTTP_WRITE_WORD = re.compile(r"""\.(?:post|patch|put|delete)\s*\(|method\s*[=:]\s*['"](?:POST|PATCH|PUT|DELETE)|_request\s*\(\s*['"](?:POST|PATCH|PUT|DELETE)|"""
                             r"""['"](?:POST|PATCH|PUT|DELETE)['"]\s*,\s*['"]?/|\bupdate_page\s*\(|\bset_page_fase\s*\(|\bset_page_link\s*\(|\bcreate_page\s*\(|"""
                             r"""\bhttp\.client\b.*\.request\s*\(\s*['"](?:POST|PATCH|PUT|DELETE)|\burlopen\s*\([^)]*\bdata\s*=""", re.I | re.S)
NOTION_CLI_WRITE = re.compile(r"notion_client\.py\s+(?:set[-_]fase|set[-_]link|update|create|archive|delete)\b", re.I)
HTTP_VERBS = {"post", "patch", "put", "delete"}
# R5 (D-1, D-4, M-2). L'analisi dei comandi che scrivono su Notion e' un'EURISTICA sul testo del comando (e sui file di script che il comando lancia): NON e' un confine di
# sicurezza. Il confine vero e' che la chiave del bot non sia raggiungibile dall'agente (canale fuori dal contenitore, punto 66).
CURL_SHORT_ARG = frozenset("AbcCdDeEFHKmoPQrtTuUwxXYyzZ")          # opzioni corte di curl che prendono un argomento (MAIUSCOLE/minuscole contano: -f e' "fail", -F e' "form")
CURL_WRITE_LONG = ("data", "data-raw", "data-binary", "data-urlencode", "data-ascii", "json", "form", "form-string", "upload-file")
WGET_WRITE_LONG = ("post-data", "post-file", "body-data", "body-file")
HTTPIE = {"http", "https", "xh"}
PS_DL = {"iwr", "irm", "invoke-webrequest", "invoke-restmethod"}
# lista BIANCA degli strumenti del connettore Notion ammessi nel cloud: solo letture (fetch, search, get-*, query-*, list-*, retrieve-*, read-*, post-search, post-database-query)
# R6 (M4): lista bianca a NOMI ESATTI (senza distinzione di maiuscole, `_` = `-`, prefissi api- e notion- facoltativi). Il prefisso "get-/list-/..." NON basta: `notion-get-or-create-page` non e' una lettura.
NOTION_MCP_READS = frozenset({"fetch", "search", "read-page", "read-pages", "get-self", "get-user", "get-users", "get-teams", "get-comments", "get-block-children",
                              "list-databases", "list-users", "query-data-source", "query-data-sources", "query-database-view", "query-meeting-notes",
                              "retrieve-a-page", "retrieve-a-database", "retrieve-a-block", "retrieve-a-comment", "retrieve-a-page-property", "retrieve-a-data-source", "retrieve-a-user",
                              "post-search", "post-database-query"})
QUOTED_VERB = re.compile(r"""['"](?:POST|PATCH|PUT|DELETE|post|patch|put|delete)['"]""")
KW_BODY = re.compile(r"""[(,]\s*(?:data|json|files|body)\s*=(?!=)|[{,]\s*(?:body|data)\s*:""")
POS_DATA = re.compile(r"\b(?:urlopen|Request)\s*\(\s*[^,()]+,\s*(?!\w+\s*=)[^\s)]")
NET_WRITE_API = re.compile(r"\.Upload(?:String|Data|File|Values)(?:Async|TaskAsync)?\s*\(|\.(?:Post|Put|Patch|Delete)Async\s*\(|Net::HTTP::(?:Post|Patch|Put|Delete)|"
                           r"\bget_method\b|XMLHttpRequest|\.Method\s*=\s*['\"]?(?:POST|PATCH|PUT|DELETE)|\bSendAsync\s*\(", re.I)
TRUSTED_SCRIPTS = frozenset(m + ".py" for m in MODULES)             # i moduli del canale: protetti da modifiche, non si leggono per cercare scritture
try:
    PIPE_REAL = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Sistema", "scripts", "video-pipeline"))      # il pipeline VERO del repo (l'hook sta in <repo>/.claude/hooks/)
except Exception:                                                  # noqa: BLE001
    PIPE_REAL = None


# ------------------------------------------------------------------------------------------------ percorsi

def norm(p, cwd=""):
    """Percorso normalizzato: barre dritte, minuscolo, `.` e `..` risolti, relativo risolto con la cwd (se c'e'; altrimenti con una barra iniziale)."""
    s = (p or "").strip().strip("\"'").replace("\\", "/").lower()
    if not s:
        return s
    if not (s.startswith("/") or re.match(r"[a-z]:/", s) or s.startswith("~")):
        base = (cwd or "").replace("\\", "/").lower().rstrip("/")
        s = (base + "/" + s) if base else "/" + s
    return posixpath.normpath(s)


def _variants(p, cwd=""):
    """Il percorso normalizzato con la cwd e senza (unione: la cwd dell'evento potrebbe non essere quella del comando)."""
    out = {norm(p, cwd), norm(p, "")}
    res = {"/" + v.lstrip("/") if not re.match(r"[a-z]:/|~", v) else v for v in out}
    for v in list(res):                                                  # R4 (D5): nomi 8.3 -> nome vero
        res |= alias83_variants(v)
    return res


def alias83_variants(n):
    """Varianti di un percorso normalizzato in cui i segmenti nome-8.3 (`video-~1`, `approv~1.py`) sono sostituiti dai nomi veri che potrebbero essere."""
    segs = n.split("/")
    if not any(ALIAS83.match(x) for x in segs):
        return set()
    out = {n}
    for i, sg in enumerate(segs):
        m = ALIAS83.match(sg)
        if not m:
            continue
        stem, ext = re.sub(r"[^a-z0-9]", "", m.group(1)), m.group(2) or ""
        if not stem:
            continue
        new = set()
        for base, own in CANON83:
            if re.sub(r"[^a-z0-9]", "", base).startswith(stem):
                for o in out:
                    parts = o.split("/")
                    parts[i] = base + (ext or own)
                    new.add("/".join(parts))
        out |= new
    out.discard(n)
    return out


def module_protected(n):
    return bool(MODULE_PATH.search(n)) and not SAFE_TREE.search(n)


def std_shadow(n):
    """R3: True se `n` (percorso normalizzato) e' un file o una cartella nel livello alto della cartella del pipeline che ha il nome di un modulo standard o di
    una libreria (logging.py, argparse/, yaml.pyc, tempfile.abi3.so...). Backup e archivio esclusi."""
    if SAFE_TREE.search(n):
        return False
    m = PIPE_TOP.search(n)
    if not m:
        return False
    first = m.group(1)
    stem = first.split(".", 1)[0]
    rest = first[len(stem):]
    return stem in STD_NAMES and (rest == "" or bool(STD_EXT.fullmatch(rest)))


def path_protected(p, cwd="", shell=False):
    """True se `p` (percorso o argomento di un comando) e' nella zona protetta."""
    for n in _variants(p, cwd):
        if module_protected(n) or PROTECTED_PATH.search(n) or ENV_FILE.search(n) or std_shadow(n) or PYDIR_PATH.search(n):
            return True
        if shell and SHELL_EXTRA.search(n):
            return True
    return False


_GLOB = re.compile(r"[*?\[]")
_GLOB_CANDIDATES = (
    ["/sistema/scripts/video-pipeline/%s.py" % m for m in MODULES] + ["/sistema/scripts/video-pipeline/%s/__init__.py" % m for m in MODULES] +
    ["/.claude/hooks/spend-guard.py", "/.claude/hooks/protected-zone-guard.py", "/.claude/hooks/agent-permissions-guard.py",
     "/.claude/settings.json", "/.claude/settings.local.json", "/.claude/agents/direttore.md", "/.claude/agents/verificatore.md",
     "/sistema/architettura-agenti/approvazioni.md", "/sistema/architettura-agenti/lista-bianca-comunicazioni.md"])


def _brace_expand(tok, depth=0):
    """Espansione semplice di {a,b} (un livello alla volta, max 32 risultati)."""
    m = re.search(r"\{([^{}]*,[^{}]*)\}", tok)
    if not m or depth > 3:
        return [tok]
    out = []
    for alt in m.group(1).split(","):
        out.extend(_brace_expand(tok[:m.start()] + alt + tok[m.end():], depth + 1))
        if len(out) > 32:
            break
    return out[:32]


def token_protected(tok, cwd=""):
    """Un argomento di comando (anche con glob, graffe, key=valore) e' un bersaglio protetto?"""
    for t in _brace_expand(tok):
        cands = [t]
        if "=" in t and not t.startswith("="):                    # of=file, --output=file, -OutFile=...
            cands.append(t.split("=", 1)[1])
        for c in cands:
            if not c:
                continue
            if path_protected(c, cwd, shell=True):
                return True
            if _GLOB.search(c):
                for v in _variants(c, cwd):
                    pat = "*" + v if v.startswith("/") else v
                    if any(fnmatch.fnmatchcase(cd, pat) or fnmatch.fnmatchcase(cd, v) for cd in _GLOB_CANDIDATES):
                        return True
    return False


# ------------------------------------------------------------------------------------------------ analisi del comando di shell

class Simple:
    """Un comando semplice: parole (senza virgolette), redirezioni (op, bersaglio), testo grezzo e corpo dei here-doc."""
    def __init__(self):
        self.words, self.redirs, self.raw, self.heredoc = [], [], [], []
        self.piped = False           # R3: il comando e' seguito da una vera pipe (|, |&) verso il successivo
        self.dq = []                 # parole che contenevano virgolette doppie (possibile sostituzione di comandi)
        self.written = frozenset()   # R4: percorsi (normalizzati) scritti da altri comandi della STESSA riga (redirezioni, tee)
        self.piped_in = False        # R4: il comando riceve il suo ingresso da una pipe


def parse_shell(cmd):
    """(lista di Simple, bilanciato). Divide in comandi semplici; le virgolette proteggono il testo libero; gestisce here-doc, a capo con \\, commenti."""
    cmds, cur = [], Simple()
    word, inword, hasq = [], False, False
    pend_redir = None                     # operatore in attesa del suo bersaglio
    pend_heredoc = []                     # [(delimitatore, cmd)]
    i, n = 0, len(cmd)
    balanced = True
    brace = 0

    def end_word():
        nonlocal word, inword, hasq, pend_redir
        if inword:
            w = "".join(word)
            if pend_redir is not None:
                cur.redirs.append((pend_redir, w))
                pend_redir = None
            elif pend_heredoc and pend_heredoc[-1][0] is None:
                pend_heredoc[-1] = (w, pend_heredoc[-1][1])
            else:
                cur.words.append(w)
        word, inword, hasq = [], False, False

    def end_cmd():
        nonlocal cur, pend_redir
        end_word()
        pend_redir = None
        if cur.words or cur.redirs or cur.heredoc:
            cmds.append(cur)
        cur = Simple()

    while i < n:
        c = cmd[i]
        if c in " \t\r":
            end_word(); i += 1; continue
        if c == "\n":
            end_cmd(); i += 1
            while pend_heredoc:                              # consuma i corpi dei here-doc fino al delimitatore
                delim, owner = pend_heredoc.pop(0)
                body = []
                while i < n:
                    j = cmd.find("\n", i)
                    line = cmd[i:] if j < 0 else cmd[i:j]
                    i = n if j < 0 else j + 1
                    if line.strip("\r\t ") == delim:
                        break
                    body.append(line)
                owner.heredoc.append("\n".join(body))
            continue
        if c == "\\" and i + 1 < n:
            nx = cmd[i + 1]
            if nx == "\n":
                i += 2; continue
            if nx == "\r" and cmd[i + 2:i + 3] == "\n":
                i += 3; continue
            if nx in "'\"\\ $;&|<>(){}`":
                word.append(nx); inword = True; cur.raw.append(nx); i += 2; continue
            word.append(c); inword = True; cur.raw.append(c); i += 1; continue
        if c == "`" and i + 1 < n and cmd[i + 1] == "\n":     # a capo di PowerShell
            i += 2; continue
        if c == "#" and not inword:
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        if c in "'\"":
            q, j, buf = c, i + 1, []
            closed = False
            while j < n:
                d = cmd[j]
                if q == '"' and d == "\\" and j + 1 < n and cmd[j + 1] in '"\\$`':
                    buf.append(cmd[j + 1]); j += 2; continue
                if d == q:
                    closed = True; break
                buf.append(d); j += 1
            if not closed:
                balanced = False
            text = "".join(buf)
            word.append(text); inword = True; hasq = True
            cur.raw.append(text)
            if q == '"':
                cur.dq.append(text)
            i = j + 1
            continue
        if c == "$" and cmd[i + 1:i + 2] == "(":
            end_cmd(); i += 2; continue
        if c in "();`":
            end_cmd(); i += 1; continue
        if c == "{" and not inword and cmd[i:i + 2] == "{}":      # R6: `{}` e' una parola (segnaposto di find -exec e xargs -I), non un gruppo
            word.append("{}"); inword = True; cur.raw.append("{}"); i += 2; continue
        if c == "{" and not inword:
            mb = re.match(r"\{[^{}\s;|&<>()]*,[^{}\s;|&<>()]*\}", cmd[i:])
            if mb:                                            # espansione {a,b} a inizio parola: e' parte della parola
                word.append(mb.group(0)); inword = True; cur.raw.append(mb.group(0)); i += len(mb.group(0)); continue
            end_cmd(); i += 1; continue
        if c == "{":
            brace += 1; word.append(c); i += 1; continue
        if c == "}":
            if brace > 0 and inword:
                brace -= 1; word.append(c); i += 1; continue
            end_cmd(); i += 1; continue
        if c == "|":
            j = i + 1
            real_pipe = cmd[j:j + 1] != "|"
            if cmd[j:j + 1] in ("|", "&"):
                j += 1
            end_word()
            if real_pipe:
                cur.piped = True
            end_cmd(); i = j; continue
        if c == "&":
            if cmd[i + 1:i + 2] == ">":                       # &> file
                end_word(); i += 2
                if cmd[i:i + 1] == ">":
                    i += 1
                pend_redir = "&>"; continue
            end_cmd(); i += 1 + (1 if cmd[i + 1:i + 2] == "&" else 0); continue
        if c == ">":
            if inword and re.fullmatch(r"\d+", "".join(word)) and not hasq:
                word, inword = [], False                       # il numero davanti e' il descrittore (2>)
            else:
                end_word()
            i += 1
            op = ">"
            if cmd[i:i + 1] == ">":
                op = ">>"; i += 1
            elif cmd[i:i + 1] == "|":
                i += 1
            if cmd[i:i + 1] == "&" and re.match(r"[0-9\-]", cmd[i + 1:i + 2] or ""):    # 2>&1: duplicazione, nessun file
                i += 1
                while i < n and (cmd[i].isdigit() or cmd[i] == "-"):
                    i += 1
                continue
            if cmd[i:i + 1] == "&":
                i += 1
            pend_redir = op
            continue
        if c == "<":
            end_word()
            if cmd[i:i + 3] == "<<<":
                i += 3; pend_redir = "<<<"; continue
            if cmd[i:i + 2] == "<<":
                i += 2
                if cmd[i:i + 1] == "-":
                    i += 1
                pend_heredoc.append((None, cur)); continue
            i += 1; pend_redir = "<"; continue
        word.append(c); inword = True; cur.raw.append(c); i += 1
    end_cmd()
    return cmds, balanced


LOOSE = re.compile(r"(?:%s)|\.claude[/\\]+(?:hooks|settings|agents)|approvazioni\.md|lista-bianca-comunicazioni|(?:direttore|verificatore)\.md|(?:^|[\s/\\\"'])\.env\b|"
                   r"site-packages|dist-packages|usercustomize|sitecustomize|\.pth\b|\.local[/\\]lib[/\\]python" % _MOD, re.I)
WRITE_PRIM = re.compile(
    r"(?:open\s*\(.*?['\"](?:[wax]|r\+)[bt+]*['\"]|\.(?:write_text|write_bytes|write|touch|unlink|symlink_to|hardlink_to|rename|rmdir|truncate)\s*\(|"
    r"\bshutil\.(?:copy|copy2|copyfile|copytree|move|rmtree)|\bos\.(?:replace|rename|remove|unlink|symlink|link|truncate|rmdir|makedirs|mkdir|system|popen)|"
    r"\bsubprocess\b|writeFile|appendFile|createWriteStream|copyFile|renameSync|unlinkSync|File\.write|File\.open|"
    r"\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|\bNew-Item\b|\bRemove-Item\b|\bCopy-Item\b|\bMove-Item\b|\bClear-Content\b|"
    r"WriteAllText|WriteAllBytes|\bFile\]::|\btee\b|\bcp\s|\bmv\s|\brm\s|\bsed\s+-i|\bextractall|\bunpack_archive|>>?\s*['\"]?[^\s'\"|&;]+)", re.I)

READ_GIT = {"status", "diff", "log", "show", "ls-files", "blame", "rev-parse", "branch", "remote", "config", "describe", "grep", "cat-file",
            "fetch", "ls-remote", "shortlog", "reflog", "help", "version", "add", "commit", "push", "tag"}
WRITE_GIT = {"checkout", "restore", "reset", "apply", "am", "stash", "merge", "rebase", "cherry-pick", "revert", "pull", "clean", "rm", "mv", "switch",
             "checkout-index", "read-tree", "update-index"}
DEST_LAST = {"cp", "copy", "copy-item", "cpi", "install", "mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni", "rsync", "scp"}
MOVERS = {"mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni"}
ANY_ARG_WRITERS = {"rm", "del", "erase", "rd", "rmdir", "unlink", "shred", "truncate", "touch", "mkdir", "md", "tee", "remove-item", "ri",
                   "set-content", "sc", "add-content", "ac", "out-file", "new-item", "ni", "clear-content", "clc", "set-itemproperty", "sp", "patch", "ex", "ed",
                   "ln", "mklink", "robocopy", "xcopy", "tee-object", "export-clixml", "export-csv", "tar", "unzip", "7z", "7za", "expand-archive",
                   # R3: editor che aprono e salvano il file dato (vim -c wq ...)
                   "vim", "vi", "nvim", "rvim", "nano", "micro", "emacs", "pico", "joe", "gedit", "kate", "notepad", "notepad++"}
# R3
EXTRACTORS = {"tar", "bsdtar", "unzip", "7z", "7za", "7zr", "expand-archive"}
AWKS = {"awk", "gawk", "mawk", "nawk"}
PIP_PROGS = {"pip", "pip3", "pipx", "uv"}
DECODE_THEN_RUN = {"sh", "bash", "zsh", "dash", "ksh", "ash", "python", "python3", "py", "node", "nodejs", "perl", "ruby", "pwsh", "powershell", "iex",
                   "invoke-expression", "php", "lua", "source", ".", "eval"}
DOTNET_WRITE = re.compile(r"(?:\[\s*(?:System\.)?IO\.(?:File|Directory|FileInfo|DirectoryInfo|StreamWriter|FileStream)\s*\]\s*::\s*"
                          r"(?:WriteAll\w+|AppendAll\w+|Replace|Create\w*|Open\w*|Delete|Write\w*)|"
                          r"New-Object\s+(?:System\.)?IO\.(?:StreamWriter|FileStream)|\.(?:CopyTo|MoveTo|CreateText|AppendText|OpenWrite)\s*\()", re.I)
DOTNET_COPY = re.compile(r"\[\s*(?:System\.)?IO\.File\s*\]\s*::\s*(Copy|Move)\s*\(\s*([^,()]+),\s*([^,()]+)", re.I)     # Copy/Move: conta la destinazione (e, per Move, la sorgente)
OPAQUE_DEC = re.compile(r"b64decode|b32decode|b85decode|a85decode|fromhex|unhexlify|codecs\.decode|\batob\s*\(|FromBase64String|Buffer\.from\([^)]*(?:base64|hex)", re.I)
OPAQUE_EXE = re.compile(r"\b(?:exec|eval|compile)\s*\(|__import__|getattr\s*\(|\bFunction\s*\(|child_process|os\.system|os\.popen|subprocess|"
                        r"Invoke-Expression|\biex\b|\[scriptblock\]", re.I)
DYN_NAME = re.compile(r"\bchr\s*\(|\.join\s*\(|\.format\s*\(|bytes\.fromhex|\[::-1\]|\bf['\"]|%s|%\(", re.I)
PIPE_WORDS = re.compile(r"video-pipeline|\.claude|[/\\]hooks\b", re.I)
_DANGEROUS_END = re.compile(r"(?:^|/)(?:video-pipeline|\.claude|\.claude/hooks|sistema|sistema/scripts|sistema/architettura-agenti)$")


def _deconcat(t):
    """Toglie le giunzioni di stringhe letterali ('approve_' + 'spend.py', "a" "b") cosi' un nome spezzato in due pezzi si legge intero."""
    return re.sub(r"""['"]\s*(?:\+\s*)?['"]""", "", t)


def _std_text(t):
    """True se nel testo di un comando c'e' un percorso completo `Sistema/scripts/video-pipeline/<nome>` con un nome da modulo standard (logging.py, argparse/...)."""
    for m in re.finditer(r"sistema[/\\]+scripts[/\\]+video-pipeline[/\\]+([^\s/\\'\"),;]+)", _deconcat(t), re.I):
        if path_protected("Sistema/scripts/video-pipeline/" + m.group(1)):
            return True
    return False


def _dest_dangerous(d, cwd=""):
    """R3: la cartella `d` e' (o contiene) una cartella con moduli/hook protetti: il pipeline, .claude, .claude/hooks, Sistema, Sistema/scripts,
    la cartella delle regole, oppure (sul disco) una cartella che ha dentro .claude o il pipeline (per esempio la radice del repository)."""
    for v in _variants(d, cwd):
        if _DANGEROUS_END.search(v):
            return True
    try:
        p = os.path.normpath(os.path.join(cwd or "", d))
        return any(os.path.isdir(os.path.join(p, rel)) for rel in
                   (".claude", os.path.join("Sistema", "scripts", "video-pipeline"), os.path.join("scripts", "video-pipeline"), "video-pipeline"))
    except (OSError, ValueError):
        return False


def _contents_src(s):
    """Sorgente che indica 'il CONTENUTO' (dir/., dir/*, dir/, glob, .): non si sa quali nomi finiscono nella destinazione."""
    b = s.replace("\\", "/")
    return b.endswith(("/.", "/*", "/")) or b in (".", "..") or bool(_GLOB.search(b.rsplit("/", 1)[-1]))


def _patch_reason(files, bodies, cwd):
    """Percorso protetto toccato da una patch (file indicati o testo dato in ingresso); None se nessuno o se non si riesce a leggere."""
    texts = list(bodies)
    for f in files:
        try:
            p = f if os.path.isabs(f) else os.path.join(cwd or "", f)
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                texts.append(fh.read(2000000))
        except (OSError, ValueError):
            continue
    for t in texts:
        for m in re.finditer(r"^(?:\+\+\+ |--- |diff --git |rename (?:to|from) |copy (?:to|from) )(\S+)(?: (\S+))?", t, re.M):
            for g in m.groups():
                if g and token_protected(g.strip("\"'"), "" if not cwd else cwd):
                    return g
    return None


def _pip_reason(args):
    low = [a.lower() for a in args]
    if low[:1] == ["config"] and any(a in ("set", "edit") for a in low[1:]):
        return "pip config set/edit: cambia dove pip installa (user, target, prefix...)"
    if "install" not in low:
        return None
    for a in low:
        if a in PIP_BAD_FLAGS or a.startswith(("--target=", "--prefix=", "--root=")):
            return f"pip install {a}: installa pacchetti Python in una posizione scelta dall'agente o nell'ambiente di sistema (puo' coprire yaml, requests...)"
    return None


def _patch_inline_reason(s, files, cwd, stdin_flag=False):
    """R4 (D5): la patch e' scritta e applicata nello stesso comando, o arriva da una pipe: la guardia non puo' leggerla prima, quindi nel dubbio si nega."""
    for f in files:
        if _variants(f, cwd) & set(s.written):
            return "la patch viene scritta e applicata nello stesso comando (il contenuto non si puo' leggere prima)"
    if s.piped_in and (stdin_flag or not files):
        return "patch ricevuta da una pipe: il contenuto non si puo' leggere prima"
    return None


def _peel(words):
    """R6 (V6-G1): toglie in testa le assegnazioni di variabili, le PAROLE RISERVATE della shell (`!`, `if`, `then`, `else`, `elif`, `do`, `while`, `until`, `{`, `}`, `coproc`...)
    e i wrapper (sudo, env, time, nohup...) con le loro opzioni: il comando vero e' quello che viene dopo."""
    words = list(words)
    while words:
        w0 = words[0]
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*=", w0):
            words.pop(0); continue
        if w0.lower() in RESERVED:
            words.pop(0); continue
        if _prog(w0) in WRAPPERS:
            vopts = WRAPPER_VALUE_OPTS.get(_prog(w0), ())
            words.pop(0)
            while words and (words[0].startswith("-") or re.match(r"[A-Za-z_][A-Za-z0-9_]*=", words[0]) or re.fullmatch(r"\d+(\.\d+)?[smhd]?", words[0])):
                opt = words.pop(0)
                if opt in vopts and words:                    # R7 (V7-M1): `sudo -u root X`, `timeout -s KILL 5 X`, `env -u FOO X`: il VALORE dell'opzione non e' il programma
                    words.pop(0)
            continue
        break
    return words


def _prog_args(s):
    """(programma, argomenti) di un comando semplice, saltando assegnazioni di variabili, parole riservate e wrapper (sudo, env, time...)."""
    words = _peel(s.words)
    return (_prog(words[0]), words[1:]) if words else None


def _is_decoder(prog, args):
    low = [a.lower() for a in args]
    return ((prog == "base64" and any(a in ("-d", "--decode", "-D") for a in args)) or (prog == "xxd" and "-r" in low) or
            (prog == "openssl" and "enc" in low and "-d" in low) or prog in ("basenc", "uudecode"))
DESTRUCTIVE = {"rm", "del", "erase", "rd", "rmdir", "remove-item", "ri", "unlink", "shred", "ln", "mklink"}      # colpiscono anche l'INTERA cartella del pipeline
DEST_OPTS = {"-t", "--target-directory", "-destination", "-dest"}
SHELLS_C = {"bash", "sh", "zsh", "dash", "ksh", "ash"}
PS_NAMES = {"powershell", "pwsh"}
SCRIPT_INTERP = {"python", "python3", "py", "pythonw", "node", "nodejs", "deno", "bun", "ruby", "perl", "php", "lua", "rscript"}
DATA_PROGS = frozenset({"echo", "printf", "cat", "grep", "egrep", "fgrep", "rg", "ls", "head", "tail", "wc", "diff", "cmp", "sort", "uniq", "tr", "cut", "jq", "basename", "dirname", "true", "false",
                        "sleep", "date", "which", "type", "man", "stat", "file", "readlink", "realpath", "write-output", "write-host", "select-string", "get-content", "findstr"})
LATE_WRITERS = frozenset({"cp", "copy", "copy-item", "cpi", "install", "mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni", "rsync", "scp", "rm", "del", "erase", "rd", "rmdir", "unlink",
                          "shred", "truncate", "touch", "mkdir", "md", "tee", "remove-item", "ri", "ln", "mklink", "sed", "dd", "patch", "tar", "bsdtar", "unzip", "7z", "7za", "expand-archive", "set-content", "add-content", "out-file"})
RESERVED = frozenset({"!", "if", "then", "else", "elif", "do", "while", "until", "{", "}", "coproc"})     # R6 (V6-G1): parole riservate della shell, mai il programma
WRAPPERS = {"sudo", "env", "time", "nohup", "nice", "command", "builtin", "exec", "stdbuf", "timeout", "start", "call", "doas", "ionice", "setsid", "caffeinate"}
# R7 (V7-M1): opzioni dei wrapper il cui valore e' la parola SUCCESSIVA (la forma attaccata `-uroot` e `--user=root` e' gia' una sola parola). Opzione non elencata = senza valore (come prima).
WRAPPER_VALUE_OPTS = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-T", "-U", "--user", "--group", "--host", "--prompt", "--close-from", "--chdir", "--role", "--type", "--command-timeout", "--other-user"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-P", "-a", "--argv0"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "exec": frozenset({"-a"}),
    "caffeinate": frozenset({"-t", "-w"}),
}
INLINE_FLAGS = {"-c", "-e", "-E", "-p", "-r", "--eval"}
DOWNLOADERS = {"curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod"}
OUT_OPTS = ("-o", "-O", "--output", "--output-document", "-outfile", "-of", "-OutFile")


def _prog(w):
    b = w.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suf in (".exe", ".cmd", ".bat"):
        if b.endswith(suf):
            b = b[:-len(suf)]
    return re.sub(r"\d+(\.\d+)*$", "", b) if re.match(r"(python|pythonw|node|ruby|perl|php|lua)[\d.]*$", b) else b


XARGS_SEP = frozenset({"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a", "-J", "-R", "-S", "--arg-file", "--delimiter", "--max-args", "--max-procs", "--max-chars", "--max-lines",
                       "--eof", "--replace", "--process-slot-var"})      # opzioni di xargs il cui valore e' la parola successiva (-i, -l, -e: il valore, se c'e', e' attaccato)


def _xargs_rest(args):
    """R6: il comando che xargs lancia (le parole dopo le sue opzioni)."""
    rest = list(args)
    while rest and rest[0].startswith("-") and rest[0] != "-":
        opt = rest.pop(0)
        if opt == "--":
            break
        if opt in XARGS_SEP and rest:
            rest.pop(0)
    return rest


def _late(w):
    """Nome del programma di una parola che sta DOPO il primo (per la ricerca dei programmi lanciati da altri): il nome si legge cosi' com'e' (`PATCH` non e' `patch`); solo i cmdlet di PowerShell (con un trattino) senza distinzione di maiuscole."""
    b = w.replace("\\", "/").rsplit("/", 1)[-1]
    return b.lower() if "-" in b else b


def _positionals(words):
    return [w for w in words if not (w.startswith("-") and len(w) > 1)]


def _analyze_simple(s, cwd, depth, full=""):
    """Motivo (str) se il comando semplice scrive su un percorso protetto; altrimenti None."""
    for op, tgt in s.redirs:
        if op in (">", ">>", "&>") and token_protected(tgt, cwd):
            return f"redirezione ({op}) verso {tgt}"
    words = list(s.words)
    # sostituzioni di comandi dentro le virgolette doppie: $(...) e `...`
    for t in s.dq:
        for inner in re.findall(r"\$\(([^()]*)\)", t) + re.findall(r"`([^`]*)`", t):
            r = shell_reason(inner, cwd, depth + 1)
            if r:
                return r
    # prefissi: assegnazioni di variabili, parole riservate (R6) e wrapper
    words = _peel(words)
    if not words:
        return None
    prog, args = _prog(words[0]), words[1:]
    low = [a.lower() for a in args]
    code = None                                                 # codice in linea da analizzare
    body = "\n".join(s.heredoc + [t for op, t in s.redirs if op == "<<<"])           # R6: anche la here-string e' un testo che la shell esegue

    if prog == "eval":
        return shell_reason(" ".join(args), cwd, depth + 1)
    if prog in SHELLS_C or prog in PS_NAMES or prog in ("cmd", "wsl"):
        flags = {"cmd": ("/c", "/k"), "wsl": ("-e", "--", "bash")}.get(prog)
        if flags is None:
            flags = ("-c", "-command", "-com", "-encodedcommand", "-ec", "-enc", "-e") if prog in PS_NAMES else ("-c",)
        for k, a in enumerate(args):
            if a.lower() in flags and k + 1 < len(args):
                payload = args[k + 1] if prog in SHELLS_C else " ".join(args[k + 1:])
                if a.lower() in ("-encodedcommand", "-ec", "-enc") or (a.lower() == "-e" and prog in PS_NAMES):
                    try:
                        import base64
                        payload = base64.b64decode(args[k + 1]).decode("utf-16-le")
                    except Exception:                          # noqa: BLE001
                        return "comando codificato (base64) non leggibile: nel dubbio negato" if LOOSE.search(" ".join(args)) else None
                r = shell_reason(payload, cwd, depth + 1)
                if r:
                    return r
                if prog in PS_NAMES and LOOSE.search(payload) and WRITE_PRIM.search(payload):
                    return "codice PowerShell che nomina un percorso protetto e contiene una scrittura"
                if prog in PS_NAMES and OPAQUE_DEC.search(payload) and OPAQUE_EXE.search(payload):
                    return "codice PowerShell che decodifica (base64) ed esegue: opaco, nel dubbio negato"
        if body:
            r = shell_reason(body, cwd, depth + 1)
            if r:
                return r
        return None
    if prog == "sed" or prog == "perl":
        inplace = any(a == "--in-place" or a.startswith("--in-place=") or re.fullmatch(r"-[a-z]*i[a-z.]*", a) for a in low)
        if inplace and any(token_protected(a, cwd) for a in args):
            return f"{prog} -i su un percorso protetto"
        if prog == "sed":
            for a in args:                                              # R3: il comando `w file` e il flag `s///w file` scrivono senza -i
                for mw in re.finditer(r"""(?:^|[\s;{}!/\d$,='"])[wW]\s*([^\s;}'"]+)""", a):
                    if token_protected(mw.group(1), cwd):
                        return f"sed: il comando 'w' scrive su {mw.group(1)}"
            return None
    if prog in SCRIPT_INTERP and len(args) >= 2 and args[0] == "-m" and args[1].lower() in ("pip", "pip3"):
        return _pip_reason(args[2:])
    if prog in SCRIPT_INTERP:
        # codice in linea: -c / -e / - (stdin) / here-doc
        inline = []
        for k, a in enumerate(args):
            if a in INLINE_FLAGS or (a.startswith("-") and not a.startswith("--") and 2 < len(a) <= 6 and a[1:].isalpha() and a[-1] in "ceE"):
                inline.extend(args[k + 1:k + 2])
        if "-" in args:
            inline.append("")
        if body:
            inline.append(body)
        text = "\n".join(inline)
        if inline and (LOOSE.search(text) or LOOSE.search(_deconcat(text)) or _std_text(text)) and WRITE_PRIM.search(text):
            return "codice in linea che nomina un percorso protetto (anche spezzato in pezzi) e contiene una scrittura"
        if inline and OPAQUE_DEC.search(text) and OPAQUE_EXE.search(text):
            return "codice in linea che decodifica (base64, esadecimale) ed esegue: opaco, nel dubbio negato"
        if inline and WRITE_PRIM.search(text) and DYN_NAME.search(text) and PIPE_WORDS.search(text):
            return "codice in linea che scrive con un percorso costruito a pezzi nella cartella del pipeline o degli hook"
        return None
    if prog == "xargs":
        rest = _xargs_rest(args)
        if rest:
            sub = Simple(); sub.words = rest; sub.heredoc = s.heredoc
            r = _analyze_simple(sub, cwd, depth + 1, full)
            if r:
                return "xargs: " + r
            sp = (_prog_args(sub) or ("",))[0]
            if (sp in ANY_ARG_WRITERS or sp in DEST_LAST or sp == "sed") and LOOSE.search(full or " ".join(s.raw)):
                return f"xargs {sp} con un nome protetto nell'ingresso"
        return None
    if prog == "find":
        paths = []
        for a in args:                                                  # R6: i percorsi di partenza (fino alla prima espressione)
            if a in ("-H", "-L", "-P") or (a.startswith("-D") or a.startswith("-O")) and len(a) <= 4:
                continue
            if a.startswith("-") or a in ("!", "(", ")"):
                break
            paths.append(a)
        k = 0
        while k < len(args):
            a = low[k]
            if a in ("-fprint", "-fprint0", "-fprintf", "-fls") and k + 1 < len(args) and token_protected(args[k + 1], cwd):
                return f"find {a} scrive su {args[k + 1]}"
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                j, ex = k + 1, []
                while j < len(args) and args[j] not in (";", "+"):
                    ex.append(args[j]); j += 1
                if ex:
                    for repl in list(paths or ["."]) + [q.rstrip("/\\") + "/x" for q in (paths or ["."])] + [None]:         # R6: `{}` vale ogni percorso di partenza, o un file dentro di esso (se e' una cartella protetta); senza sostituzione resta com'e'
                        sub = Simple(); sub.words = [(w.replace("{}", repl) if repl is not None else w) for w in ex]; sub.heredoc = s.heredoc
                        r = _analyze_simple(sub, cwd, depth + 1, full)
                        if r:
                            return "find -exec: " + r
                    pe = _peel(ex)
                    if pe and (_prog(pe[0]) in ANY_ARG_WRITERS or _prog(pe[0]) in DEST_LAST or _prog(pe[0]) == "sed") and any(token_protected(x, cwd) for x in args):
                        return "find -exec con scrittura e un percorso protetto"
                k = j
            k += 1
        if "-delete" in low and any(token_protected(x, cwd) for x in args):
            return "find -delete su un percorso protetto"
        return None
    if prog == "git":
        k_sub, skip = None, False
        for k, a in enumerate(low):                                      # salta -C <dir>, -c k=v, --git-dir <d>, --work-tree <d>
            if skip:
                skip = False; continue
            if a in ("-c", "--git-dir", "--work-tree", "--namespace") or (a == "-C"):
                skip = True; continue
            if a.startswith("-"):
                continue
            k_sub = k; break
        sub = low[k_sub] if k_sub is not None else ""
        rest = args[k_sub + 1:] if k_sub is not None else []
        if sub in WRITE_GIT or (sub == "reset" and "--hard" in low):
            prot = [a for a in args if token_protected(a, cwd)]
            if prot:
                return f"git {sub} su {prot[0]}"
        # R3: checkout <rev> -- <cartella> / restore --source ... <cartella> riportano INTERE cartelle da un'altra revisione
        if sub in ("checkout", "restore", "checkout-index", "read-tree"):
            if "--" in rest:
                before, targets = rest[:rest.index("--")], rest[rest.index("--") + 1:]
                rev0 = any(not a.startswith("-") for a in before)
            else:
                targets = [a for a in rest if not a.startswith("-")]
                rev0 = len(targets) >= 2
            has_rev = (sub == "checkout" and rev0) or any(a.lower().startswith(("--source", "-s")) for a in rest if a.startswith("-"))
            if has_rev and any(_dest_dangerous(t, cwd) for t in targets):
                return f"git {sub} da un'altra revisione su una cartella con moduli o hook protetti"
        # R3: git apply / am con una patch che tocca un percorso protetto
        if sub in ("apply", "am") and not ({"--check", "--stat", "--numstat", "--summary"} & set(low)):
            files = [a for a in rest if not a.startswith("-")] + [t for op, t in s.redirs if op == "<"]
            ri = _patch_inline_reason(s, files, cwd, "-" in rest)
            if ri:
                return f"git {sub}: {ri}"
            g = _patch_reason(files, s.heredoc, cwd)
            if g:
                return f"git {sub}: la patch tocca un percorso protetto ({g})"
        return None
    if prog in DEST_LAST:
        pos = _positionals(args)
        dest_vals, srcs = [], list(pos)
        for k, a in enumerate(low):
            if a in DEST_OPTS and k + 1 < len(args):
                dest_vals.append(args[k + 1])
            elif any(a.startswith(o + "=") for o in DEST_OPTS if o.startswith("--")):
                dest_vals.append(args[k].split("=", 1)[1])
        if dest_vals:
            srcs = [p for p in pos if p not in dest_vals]
        elif pos:
            dest_vals, srcs = [pos[-1]], pos[:-1]
        for d in dest_vals:
            if token_protected(d, cwd):
                return f"{prog}: destinazione protetta ({d})"
            if _dest_dangerous(d, cwd) and (prog in ("rsync", "scp") or any(_contents_src(sname) for sname in srcs)):      # R3
                return f"{prog}: copia il CONTENUTO di una cartella (o un glob) dentro {d}, che contiene moduli o hook protetti"
            if any(PIPE_DIR_END.search(v) for v in _variants(d, cwd)):          # copia dentro la cartella del pipeline: conta il nome del file
                for sname in srcs:
                    base = sname.replace("\\", "/").rsplit("/", 1)[-1]
                    if token_protected(d.rstrip("/\\") + "/" + base, cwd):
                        return f"{prog}: copia di {base} dentro la cartella del pipeline (sostituirebbe un modulo protetto)"
        if prog in MOVERS:
            for sname in srcs:
                if token_protected(sname, cwd) or any(PIPE_DIR_END.search(v) for v in _variants(sname, cwd)):
                    return f"{prog}: sposta via un percorso protetto ({sname})"
        return None
    if prog == "dd":
        for a in args:
            if a.lower().startswith("of=") and token_protected(a, cwd):
                return f"dd su {a}"
        return None
    if prog in DOWNLOADERS:                                    # solo il file di OUTPUT conta (l'URL puo' contenere il nome di un modulo)
        for k, a in enumerate(args):
            val = None
            if a in OUT_OPTS or a.lower() in OUT_OPTS:
                val = args[k + 1] if k + 1 < len(args) else None
            elif a.startswith("--output=") or a.startswith("--output-document="):
                val = a.split("=", 1)[1]
            elif re.fullmatch(r"-[oO].+", a) and not a.startswith("--"):
                val = a[2:]
            if val and token_protected(val, cwd):
                return f"{prog} con output su {val}"
        return None
    if prog in AWKS:                                           # R3: print > file, printf >> file (anche con il nome in una variabile di -v)
        scripts = [a for a in args if re.search(r">>?", a)]
        for a in scripts:
            for ma in re.finditer(r"""\b(?:print|printf)\b[^;}]*?>>?\s*"?([^\s";}]+)""", a):
                if token_protected(ma.group(1), cwd):
                    return f"{prog}: redirezione verso {ma.group(1)}"
        if any(re.search(r"\b(?:print|printf)\b[^;}]*?>>?\s*[A-Za-z_]\w*\s*[;}]", a) for a in args) and any(token_protected(a, cwd) for a in args):
            return f"{prog}: redirezione verso una variabile che nomina un percorso protetto"
        return None
    if prog in PIP_PROGS:
        return _pip_reason(args[1:] if prog == "uv" and args[:1] == ["pip"] else args)
    if prog == "patch":                                       # R3: patch -i file / patch < file: il contenuto della patch decide i bersagli
        files = [t for op, t in s.redirs if op == "<"]
        for k, a in enumerate(args):
            if a in ("-i", "--input") and k + 1 < len(args):
                files.append(args[k + 1])
            elif a.startswith("--input="):
                files.append(a.split("=", 1)[1])
        ri = _patch_inline_reason(s, files, cwd)
        if ri:
            return f"patch: {ri}"
        g = _patch_reason(files, s.heredoc, cwd)
        if g:
            return f"patch: la patch tocca un percorso protetto ({g})"
    if prog in EXTRACTORS:                                     # R3: estrarre un archivio dentro una cartella con moduli/hook (o che li contiene)
        extracting, dest = False, None
        if prog in ("tar", "bsdtar"):
            first = args[0] if args else ""
            extracting = bool(re.fullmatch(r"-?[A-Za-z]*x[A-Za-z]*", first)) or any(re.fullmatch(r"-[a-z]*x[a-z]*", a) or a in ("--extract", "--get") for a in low)
            for k, a in enumerate(args):
                if a in ("-C", "--directory") and k + 1 < len(args):
                    dest = args[k + 1]
                elif a.startswith("--directory="):
                    dest = a.split("=", 1)[1]
                elif a.startswith("-C") and len(a) > 2:
                    dest = a[2:]
        elif prog == "unzip":
            extracting = not any(a in ("-l", "-t", "-p", "-v", "-z") for a in low)
            for k, a in enumerate(args):
                if a == "-d" and k + 1 < len(args):
                    dest = args[k + 1]
                elif a.startswith("-d") and len(a) > 2:
                    dest = a[2:]
        elif prog == "expand-archive":
            extracting = True
            for k, a in enumerate(low):
                if a.startswith(("-destinationpath", "-dest", "-d")) and k + 1 < len(args):
                    dest = args[k + 1]
        else:                                                  # 7z x/e archivio -o<cartella>
            pos = _positionals(args)
            extracting = bool(pos) and pos[0].lower() in ("x", "e")
            for a in args:
                if a.startswith("-o") and len(a) > 2:
                    dest = a[2:]
        if extracting:
            eff = dest if dest is not None else (cwd or ".")
            if _dest_dangerous(eff, cwd):
                return f"{prog}: estrae dentro {eff}, che contiene moduli o hook protetti (o li contiene)"
    if prog in ("new-item", "ni") and any(a.lower() in LINK_TYPES for a in args):
        for a in args:                                          # R4 (D5): Junction / SymbolicLink / HardLink verso il pipeline, .claude, gli hook
            if not a.startswith("-") and (token_protected(a, cwd) or _dest_dangerous(a, cwd) or any(PIPE_DIR_END.search(v) for v in _variants(a, cwd))):
                return f"{prog}: collegamento ({'/'.join(sorted(LINK_TYPES & {x.lower() for x in args}))}) verso {a}, che contiene moduli o hook protetti"
    if prog in ANY_ARG_WRITERS:
        for a in args:
            if token_protected(a, cwd):
                return f"{prog} su {a}"
            if prog in DESTRUCTIVE and not a.startswith("-") and any(PIPE_DIR_END.search(v) for v in _variants(a, cwd)):
                return f"{prog} sull'intera cartella del pipeline ({a})"
        return None
    # R6 (confronto con la guardia installata): programmi che ne lanciano altri senza che li conosciamo (flock, taskset, strace, chroot, ssh, watch, parallel, busybox, su -c...): se piu' avanti c'e' un programma
    # che scrive, si analizza il comando da li' come se fosse scritto da solo; se una parola e' un'intera riga di comando (`script -qc 'cp a b'`), la si analizza come comando. Stessa severita' del testo della guardia installata.
    if prog in DATA_PROGS and not s.piped:                          # echo/printf/cat/grep... di un testo che nomina un comando: stampano, non eseguono (a meno che l'uscita vada a una pipe: `echo 'cp a b' | sh`)
        return None
    for op_, tg_ in s.redirs:                                       # here-string (`at now <<< 'cp a b'`): il testo e' un comando
        if op_ == "<<<" and re.search(r"\s", tg_) and depth < 4:
            r = shell_reason(tg_, cwd, depth + 1)
            if r:
                return f"dopo `{prog}` con here-string: {r}"
    for kx in range(0, len(words)):
        if kx and _late(words[kx]) in LATE_WRITERS:
            sub = Simple(); sub.words = words[kx:]; sub.heredoc = s.heredoc
            r = _analyze_simple(sub, cwd, depth + 1, full)
            if r:
                return f"dopo `{prog}`: {r}"
        elif re.search(r"\s", words[kx]) and depth < 4:
            r = shell_reason(words[kx], cwd, depth + 1)
            if r:
                return f"dopo `{prog}`: {r}"
    return None


def shell_reason(cmd, cwd="", depth=0):
    """Motivo se il comando di shell (Bash/PowerShell) scrive su un percorso protetto; altrimenti None. Analisi strutturale."""
    if depth > 4 or not cmd:
        return None
    cmds, balanced = parse_shell(cmd)
    if PIP_ENV.search(cmd):                                                                            # R4 (D1)
        return "variabile d'ambiente che sposta le installazioni di pip (PIP_USER, PIP_TARGET, PIP_PREFIX, PIP_ROOT, PYTHONUSERBASE): equivale a pip install --user"
    if PYDIR_QUERY.search(cmd) and (CMD_WRITE.search(cmd) or WRITE_PRIM.search(cmd)):                  # R4 (D1)
        return "comando che chiede a Python dove stanno le sue cartelle (user site, site-packages) e contiene una scrittura: nel dubbio negato"
    written = set()                                                                                    # R4 (D5): cosa scrivono, nella stessa riga, gli altri comandi
    for sc in cmds:
        for op, t in sc.redirs:
            if op in (">", ">>", "&>"):
                written |= _variants(t, cwd)
        pa = _prog_args(sc)
        if pa and pa[0] in ("tee", "tee-object"):
            for a in pa[1]:
                if not a.startswith("-"):
                    written |= _variants(a, cwd)
        if pa and pa[0] in DEST_LAST:
            pos = _positionals(pa[1])
            if pos:
                written |= _variants(pos[-1], cwd)
    for k, sc in enumerate(cmds):
        sc.written, sc.piped_in = frozenset(written), (k > 0 and cmds[k - 1].piped)
    if not balanced and CMD_PROTECTED.search(cmd) and CMD_WRITE.search(cmd):
        return "comando con virgolette non chiuse e un percorso protetto: nel dubbio negato"
    if DOTNET_WRITE.search(cmd) and (LOOSE.search(cmd) or LOOSE.search(_deconcat(cmd)) or _std_text(cmd)):          # R3: [IO.File]::WriteAllText(...) dallo strumento PowerShell diretto
        return "scrittura .NET (System.IO) che nomina un percorso protetto"
    for mc in DOTNET_COPY.finditer(cmd):
        src, dst = mc.group(2).strip().strip("\"'"), mc.group(3).strip().strip("\"'")
        if token_protected(dst, cwd) or token_protected(_deconcat(dst), cwd) or (mc.group(1).lower() == "move" and (token_protected(src, cwd) or token_protected(_deconcat(src), cwd))):
            return f"[IO.File]::{mc.group(1)} verso un percorso protetto ({dst})"
    if re.search(r"FromBase64String", cmd, re.I) and re.search(r"Invoke-Expression|\biex\b|\[scriptblock\]", cmd, re.I):
        return "PowerShell che decodifica (base64) ed esegue: opaco, nel dubbio negato"
    for k, s in enumerate(cmds):                                                                       # R3: `base64 -d | sh`
        if s.piped and k + 1 < len(cmds):
            a, b = _prog_args(s), _prog_args(cmds[k + 1])
            if a and b and _is_decoder(*a) and b[0] in DECODE_THEN_RUN:
                return "decodifica (base64, esadecimale) e passa il risultato a un interprete: comando opaco"
    for k, s in enumerate(cmds):                                                                       # R6: `echo 'cp a b' | sh`, `cat <<EOF | sh`: il testo che entra in una shell dalla pipe e' un comando
        if k > 0 and cmds[k - 1].piped:
            b2 = _prog_args(s)
            if b2 and b2[0] in SHELLS_C and "-c" not in b2[1]:
                prev = cmds[k - 1]
                r = shell_reason("\n".join([" ".join(prev.words[1:])] + prev.heredoc), cwd, depth + 1)
                if r:
                    return f"testo passato a una shell dalla pipe: {r}"
    for s in cmds:
        r = _analyze_simple(s, cwd, depth, cmd)
        if r:
            return r
    return None


def _lm(name, full):
    """Nome di opzione lunga (senza --) uguale a `full` o abbreviazione di almeno 3 lettere (curl e wget accettano le abbreviazioni univoche)."""
    return name == full or (len(name) >= 3 and full.startswith(name))


def _verb(v):
    return (v or "").strip().strip("\"'").lower()


def _curl_reason(args):
    """R5: (motivo | None, e_config). Le opzioni di curl si leggono come le legge curl: maiuscole/minuscole (-f fail, -F form), forme attaccate (-d'{..}', -XPATCH),
    gruppi (-sSfd), --opt=valore, abbreviazioni, -G (i dati diventano query)."""
    get_mode, data_opt, verb, cfg = False, None, None, False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            name = name.lower()
            if name == "get":
                get_mode = True
            elif _lm(name, "request"):
                verb = _verb(val if eq else (args[i] if i < len(args) else ""))
                i += 0 if eq else 1
            elif name == "config":
                cfg = True
            elif any(_lm(name, w) for w in CURL_WRITE_LONG):
                data_opt = a
        elif a.startswith("-") and len(a) > 1:
            for j in range(1, len(a)):
                ch = a[j]
                if ch == "G":
                    get_mode = True
                elif ch in "dFT":
                    data_opt = a
                    break
                elif ch == "X":
                    rest = a[j + 1:]
                    verb = _verb(rest if rest else (args[i] if i < len(args) else ""))
                    i += 0 if rest else 1
                    break
                elif ch == "K":
                    cfg = True
                    break
                elif ch in CURL_SHORT_ARG:
                    break
    if cfg:
        return "curl con -K/--config: le opzioni stanno in un file che il controllo non legge", True
    if verb in HTTP_VERBS:
        return f"curl con metodo {verb.upper()}", False
    if data_opt and not get_mode and verb not in ("get", "head", "options"):
        return f"curl {data_opt[:24]}: invio di dati", False
    return None, False


def _wget_reason(args):
    for i, a in enumerate(args):
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            name = name.lower()
            if _lm(name, "method"):
                if _verb(val if eq else (args[i + 1] if i + 1 < len(args) else "")) in HTTP_VERBS:
                    return "wget con metodo di scrittura"
            elif any(_lm(name, w) for w in WGET_WRITE_LONG):
                return f"wget {a[:24]}: invio di dati"
    return None


def _httpie_reason(args):
    low = [a.lower() for a in args]
    if any(x in HTTP_VERBS for x in low):
        return "metodo di scrittura"
    if any(x in ("--form", "-f", "--multipart", "--raw") for x in low):
        return "invio di dati"
    if any(re.match(r"^[^\s=:@/\-][^\s=:@/]*(?::=@?|=@|=(?!=))", a) for a in args if not a.startswith("-")):
        return "campi dati nel comando"
    return None


def _ps_reason(args, exact):
    """Parametri di Invoke-WebRequest/RestMethod: -Method X, -Method:X, -Body, -InFile, -Form (anche abbreviati: -Meth, -Bo). `exact` = solo i nomi interi (curl/wget usati come alias)."""
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if not re.match(r"^-[A-Za-z]+", a):
            continue
        name, colon, val = a[1:].partition(":")
        pn = name.lower()

        def is_(full, minlen):
            return pn == full if exact else (len(pn) >= minlen and full.startswith(pn))
        if is_("method", 2):
            if _verb(val if colon else (args[i] if i < len(args) else "")) in HTTP_VERBS:
                return "Invoke-WebRequest/RestMethod con metodo di scrittura"
        elif is_("body", 2) or is_("infile", 3) or is_("form", 3):
            return f"Invoke-WebRequest/RestMethod con {a.split(':')[0]}: invio di dati"
    return None


def _inline_write(text):
    """True se il codice (in linea, di un file o ricevuto da una pipe) nomina Notion E contiene una richiesta di scrittura."""
    t = _deconcat(text)
    if not NOTION_INLINE.search(t):
        return False
    return bool(HTTP_WRITE_WORD.search(t) or QUOTED_VERB.search(t) or KW_BODY.search(t) or POS_DATA.search(t) or NET_WRITE_API.search(t))


def _file_text(p, cwd):
    """Contenuto (max 300 KB) di un file di script citato dal comando, se esiste e non e' un modulo VERO del canale (riconosciuto dal percorso reale, non dal nome); altrimenti None."""
    try:
        full = os.path.expanduser(p)
        full = full if os.path.isabs(full) else os.path.join(cwd or os.getcwd(), full)
        full = os.path.realpath(full)
        if PIPE_REAL and os.path.normcase(os.path.dirname(full)) == os.path.normcase(PIPE_REAL) and os.path.basename(full).lower() in TRUSTED_SCRIPTS:
            return None
        if not os.path.isfile(full) or os.path.getsize(full) > 300000:
            return None
        with open(full, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def mcp_notion_reason(tool):
    """R5/D-4: nel cloud uno strumento MCP che nomina Notion passa SOLO se e' una lettura (lista bianca); tutto il resto, nomi nuovi compresi, e' negato."""
    if not re.search("notion", tool, re.I):
        return None
    action = tool.split("__", 2)[2] if tool.count("__") >= 2 else tool
    norm_a = re.sub(r"^(?:api-)?(?:notion-)?", "", re.sub(r"[_\s]+", "-", action.strip().lower()))
    if norm_a in NOTION_MCP_READS:
        return None
    return f"strumento del connettore Notion fuori dalla lista bianca delle sole letture ({action})"


def notion_write_reason(cmd, depth=0, cwd="", host_ctx=False):
    """R5: motivo (str) se il comando scrive DIRETTAMENTE su Notion (fuori da write_card.py / propose_spend.py); altrimenti None. Si usa solo nel cloud.
    EURISTICA sul testo (e sui file di script che il comando lancia): non e' un confine, vedi LEGGIMI."""
    if depth > 3 or not cmd:
        return None
    cmds, _bal = parse_shell(cmd)
    flat = _deconcat(cmd)
    host_any = bool(NOTION_HOST.search(flat)) or host_ctx                    # R6: host_ctx = il comando che contiene questo testo nomina gia' Notion (sh -c, xargs, find -exec)
    written_here = set()                                                    # percorsi scritti dallo stesso comando (redirezioni, tee, cp/mv/install): `printf ... > /tmp/w.py && python3 /tmp/w.py`
    for sc0 in cmds:
        for op, t in sc0.redirs:
            if op in (">", ">>", "&>"):
                written_here.add(os.path.normpath(t))
        pa0 = _prog_args(sc0)
        if pa0 and pa0[0] in ("tee", "cp", "mv", "install", "copy"):
            pos = _positionals(pa0[1])
            if pos:
                written_here.add(os.path.normpath(pos[-1] if pa0[0] != "tee" else pos[0]))
    def _is_written_here(a):
        n = os.path.normpath(a)
        return n in written_here or any(os.path.basename(n) == os.path.basename(w) for w in written_here)
    exec_written = False

    def check(k, sc, pa, xctx=False):
        """Motivo se il comando semplice `sc` (gia' ridotto a pa = (programma, argomenti)) scrive su Notion. xctx = il comando e' lanciato da xargs / find -exec: l'host puo' arrivare dall'ingresso."""
        nonlocal exec_written
        if not pa:
            return None
        prog, args = pa
        low = [a.lower() for a in args]
        piped_text = " ".join(cmds[k - 1].words[1:] + cmds[k - 1].heredoc) if k > 0 and cmds[k - 1].piped else ""
        if prog == "xargs":                                                    # R6 (V6-G1/M1): il comando che xargs lancia si analizza come se fosse scritto da solo
            rest = _xargs_rest(args)
            if rest:
                sub = Simple(); sub.words = rest; sub.heredoc = sc.heredoc
                return check(k, sub, _prog_args(sub), True)
            return None
        if prog == "find":                                                     # R6: idem per find -exec / -execdir / -ok / -okdir
            kx = 0
            while kx < len(args):
                if low[kx] in ("-exec", "-execdir", "-ok", "-okdir"):
                    jx, ex = kx + 1, []
                    while jx < len(args) and args[jx] not in (";", "+"):
                        ex.append(args[jx]); jx += 1
                    if ex:
                        sub = Simple(); sub.words = ex; sub.heredoc = sc.heredoc
                        rr = check(k, sub, _prog_args(sub), True)
                        if rr:
                            return rr
                    kx = jx
                kx += 1
            return None
        if prog in SHELLS_C or prog in PS_NAMES or prog in ("eval", "source", "."):
            if prog in SHELLS_C or prog in PS_NAMES:
                for kk, a in enumerate(args):
                    if a.lower() in ("-c", "-command", "-com") and kk + 1 < len(args):
                        r = notion_write_reason(args[kk + 1] if prog in SHELLS_C else " ".join(args[kk + 1:]), depth + 1, cwd, host_any)
                        if r:
                            return r
            if prog == "eval":
                r = notion_write_reason(" ".join(args), depth + 1, cwd, host_any)
                if r:
                    return r
            if piped_text:                                                    # echo '...' | sh
                r = notion_write_reason(piped_text, depth + 1, cwd, host_any)
                if r:
                    return r
            for a in [x for x in args if not x.startswith("-")][:2]:           # bash script.sh / source script.sh
                if not re.search(r"[./\\]", a):
                    continue
                tx = _file_text(a, cwd)
                if tx is None and _is_written_here(a):                           # il file non c'e' (ancora): se lo scrive lo stesso comando, il testo sta negli argomenti di echo/printf/cat/tee
                    tx = " ".join(w for s2 in cmds for w in (s2.words[1:] + s2.heredoc) if (_prog_args(s2) or ("",))[0] in ("echo", "printf", "cat", "tee"))
                if tx:
                    r = notion_write_reason(tx, depth + 1, cwd)
                    if r:
                        return "script di shell: " + r
            return None
        if prog in DOWNLOADERS or prog in HTTPIE:
            cfg_reason = None
            if prog == "curl":
                cfg_reason, is_cfg = _curl_reason(args)
                if is_cfg:
                    return cfg_reason                                          # -K/--config: nel cloud sempre negato
            own = any(NOTION_HOST.search(_deconcat(a)) for a in args) or (xctx and host_any)       # R6: sotto xargs / find -exec l'host puo' stare nell'ingresso
            indirect = host_any and any("$" in a or "`" in a for a in args if not a.startswith("-"))
            splat = host_any and any(re.fullmatch(r"@\w+", a) for a in args)     # Invoke-RestMethod @p: gli argomenti stanno in una variabile
            if splat:
                return f"{prog}: argomenti in una variabile (splatting) e Notion nel comando: nel dubbio negato"
            if not (own or indirect):
                return None
            if prog == "curl":
                r = cfg_reason
            elif prog == "wget":
                r = _wget_reason(args)
            elif prog in HTTPIE:
                r = _httpie_reason(args)
            else:
                r = None
            r = r or _ps_reason(args, exact=prog not in PS_DL)
            if r:
                return f"{prog}: {r}: scrittura diretta su Notion"
            return None
        if prog in SCRIPT_INTERP:
            inline = []
            for kk, a in enumerate(args):
                if a in INLINE_FLAGS or (a.startswith("-") and not a.startswith("--") and 2 < len(a) <= 6 and a[1:].isalpha() and a[-1] in "ceE"):
                    inline.extend(args[kk + 1:kk + 2])
                elif a.startswith("--eval="):
                    inline.append(a[7:])
            if prog in ("deno", "bun") and args and args[0] == "eval":
                inline.extend(args[1:2])
            if sc.heredoc:
                inline.extend(sc.heredoc)
            if "-" in args:
                inline.append("")
            if piped_text:
                inline.append(piped_text)
            text = "\n".join(inline)
            if text and _inline_write(text + (" api.notion.com" if xctx and host_any else "")):         # R6: sotto xargs / find -exec l'host puo' arrivare dall'ingresso
                return "codice in linea che scrive direttamente su Notion"
            if any(re.search(r"notion_client\.py$", a, re.I) for a in args) and NOTION_CLI_WRITE.search(" ".join([prog] + args)):
                return "notion_client.py con un comando di scrittura (set-fase...)"
            files = [a for a in args if not a.startswith("-") and re.search(r"[./\\]", a)][:3]
            if any(_is_written_here(a) for a in files):
                exec_written = True
            for a in files:
                tx = _file_text(a, cwd)
                if tx and _inline_write(tx):
                    return "script che scrive direttamente su Notion"
            return None
        return None
    for k, sc in enumerate(cmds):
        r = check(k, sc, _prog_args(sc))
        if r:
            return r
    if exec_written and _inline_write(cmd):                               # `printf '...' > /tmp/w.py && python3 /tmp/w.py`: il sorgente sta nello stesso comando
        return "codice (scritto e lanciato nello stesso comando) che scrive direttamente su Notion"
    if NOTION_CLI_WRITE.search(cmd):
        return "notion_client.py con un comando di scrittura (set-fase...)"
    if host_any and NET_WRITE_API.search(flat) and not re.search(r"\b(?:write_card|propose_spend|read_card)\.py\b", flat):      # PowerShell: [System.Net.WebClient]::new().UploadString(...)
        return "chiamata .NET di scrittura verso Notion"
    return None


def legacy_reason(cmd):
    return "euristica R1 (analisi strutturale non riuscita)" if (CMD_PROTECTED.search(cmd) and CMD_WRITE.search(cmd)) else None


# ------------------------------------------------------------------------------------------------ hook

def log(decision, agent, tool, what):
    try:
        p = pathlib.Path(__file__).with_name("protected-zone-guard.log")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {decision} {agent} {tool} {what[:200]!r}\n")
    except Exception:                                    # noqa: BLE001
        pass


def deny(reason, agent, tool, what):
    log("deny", agent, tool, what)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}}, ensure_ascii=False))
    sys.exit(0)


def main():
    agent = None
    try:
        ev = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace'))
        agent = ev.get("agent_id")
        if not agent and not CLOUD_STRICT:               # sessione principale: mai bloccata (tranne nel cloud, vedi CLOUD_STRICT)
            return
        who = ev.get("agent_type") or ("sessione-principale" if not agent else "?")
        tool = ev.get("tool_name") or ""
        ti = ev.get("tool_input") or {}
        cwd = ev.get("cwd") or ""
        msg = ("BLOCCATO - ZONA PROTETTA: gli agenti non modificano hook di sicurezza, permessi, direttore.md, "
               "verificatore.md, file .env, i moduli del controllo di spesa ne' il tetto di spesa. Scrivi la proposta nel tuo rapporto: "
               "la approva Cipriano.")
        fl = floor_reason(tool, ti)                      # R7 (V7-G1): PRIMA di ogni analisi, la regola della guardia installata (pavimento)
        if fl:
            deny(msg + f" ({fl[0]})", who, tool, fl[1])
        if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            raw = ti.get("file_path") or ti.get("notebook_path") or ""
            if path_protected(raw, cwd):
                deny(msg, who, tool, raw)
            fp = norm(raw, cwd)
            if PIPELINE in fp:
                text = " ".join(str(ti.get(k, "")) for k in ("content", "old_string", "new_string"))
                for e in ti.get("edits") or []:
                    text += " " + str(e.get("old_string", "")) + " " + str(e.get("new_string", ""))
                if CAP_TOKENS.search(text) or fp.endswith("models.yaml") and "budget" in text.lower():
                    deny(msg, who, tool, fp)
        elif tool in ("Agent", "Task"):
            allowed = AGENT_SPAWN_ALLOWED.get(who)
            sub_t = ti.get("subagent_type") or ti.get("agent_type") or ""
            if allowed is not None and sub_t not in allowed:
                deny(f"BLOCCATO - {who} puo' lanciare solo: {', '.join(sorted(allowed))}.", who, tool, sub_t)
        elif tool.startswith("mcp__") and CLOUD:
            if mcp_notion_reason(tool):
                deny("BLOCCATO - SCRITTURA DIRETTA SU NOTION: nel cloud il connettore Notion scrive COME Cipriano. Le scritture sulle schede e sulle proposte passano solo da "
                     "`write_card.py` (Fase, Owner, note [agente], Video finale, Task Pending) e `propose_spend.py` (proposta di spesa). Il resto lo scrivi nel rapporto.", who, tool, tool)
        elif tool == "Read":
            fp = norm(ti.get("file_path"), cwd)
            if ENV_FILE.search(fp):
                deny("BLOCCATO - i file .env non si leggono: puoi conoscere solo i NOMI delle variabili (usa .env.example).", who, tool, fp)
        elif tool in ("Bash", "PowerShell"):
            cmd = ti.get("command") or ""
            try:
                why = shell_reason(cmd, cwd)
            except Exception:                            # noqa: BLE001 - analisi strutturale fallita: si ripiega sul vecchio controllo (piu' severo)
                why = legacy_reason(cmd)
            if why:
                deny(msg + f" (comando che scrive su un percorso protetto: {why})", who, tool, cmd)
            if CLOUD:                                                       # R4: nel cloud nessuna scrittura DIRETTA su Notion (solo write_card.py / propose_spend.py)
                try:
                    nw = notion_write_reason(cmd, 0, cwd)
                except Exception:                                           # noqa: BLE001 - R5/D-4: nel cloud un errore interno NEGA (fail-closed)
                    nw = "errore interno del controllo delle scritture dirette su Notion: nel cloud nel dubbio negato"
                if nw:
                    deny("BLOCCATO - SCRITTURA DIRETTA SU NOTION: nel cloud si scrive sulle schede solo con `write_card.py` (Fase, Owner, note [agente], Video finale, Task Pending) "
                         f"e sulle proposte solo con `propose_spend.py` ({nw}). Tutto il resto lo scrivi nel rapporto.", who, tool, cmd)
            if ENV_TOKEN.search(cmd) and ENV_READ.search(cmd):
                deny("BLOCCATO - i file .env non si leggono (nemmeno via shell): puoi conoscere solo i NOMI delle variabili (usa .env.example).", who, tool, cmd)
    except SystemExit:
        raise
    except Exception:                                    # noqa: BLE001 - sul PC mai bloccare per un errore del hook; R5: nel cloud (dove vale) un errore interno NEGA
        if CLOUD and (CLOUD_STRICT or agent):
            deny("BLOCCATO - errore interno del controllo di sicurezza: nel cloud nel dubbio negato. Riprova con un comando piu' semplice o scrivi la richiesta nel rapporto.", "?", "?", "errore interno")
        return


if __name__ == "__main__":
    main()
