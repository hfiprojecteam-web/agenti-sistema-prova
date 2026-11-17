"""
vp_common.py — helper condivisi dal video pipeline (universale, non legato a un libro/progetto).

Contiene:
  - risoluzione progetto (ID pagina Notion -> cartella) da projects_map.yaml, FAIL-LOUD se non mappato
  - mapping Formato -> categoria da formats.yaml
  - path: root progetto, cartella *-Contenuti, cartella grezzi per-card (chiave univoca)
  - quote di costo (unica fonte per run.py / batch.py / 01_generate.py)
  - exit code condivisi (leggibili da un orchestrator/agente esterno)
"""
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spend_gate   # registro costi con blocco e tetto giornaliero (18/10/2026)

SCRIPT_DIR = Path(__file__).parent
WORKSPACE = SCRIPT_DIR.parents[2]          # Sistema/scripts/video-pipeline -> root workspace
PROJECTS_MAP = SCRIPT_DIR / "projects_map.yaml"
FORMATS_MAP = SCRIPT_DIR / "formats.yaml"

# Costi stimati (USD). Il costo del clip Veo non dipende dalla durata (verificato: gg06-A3 = 3 clip = $0.90).
COST_PER_CLIP = {"Fast": 0.30, "Lite": 0.15, "Quality": 1.25}
SCENE_MASTER_COST = 0.01   # fal Flux schnell ~$0.003, con margine

# Modelli "avatar": immagine + NOSTRO audio -> video con lip-sync. Costo al secondo di audio (listino kie 2026-09-30).
# Il costo reale viene poi letto da creditsConsumed (1 credito = $0.005).
AVATAR_MODELS = {"kling/ai-avatar-standard": 0.04, "kling/ai-avatar-pro": 0.08}

# Modelli "take" (ripresa unica, parlato nativo, riferimenti immagine nominati Image1, Image2...). Prezzi di listino kie al secondo (2026-10-04):
# il costo reale si legge poi da creditsConsumed. Nuovo modello take = una riga qui (+ la sua ricetta in video-bootcamp/models.yaml).
# NIENTE riferimenti audio: la voce si descrive nel prompt (accento incluso) e il modello la genera (regola validata, vedi recipe-wan30-*).
# "style" = come si costruisce l'ingresso dell'API kie: "wan" (default; audio:true, risoluzione MAIUSCOLA) oppure "seedance"
# (generate_audio:true, web_search:false, risoluzione minuscola). Le risoluzioni nelle tabelle e nelle schede restano MAIUSCOLE.
# Seedance 2.5 aggiunto il 18/10/2026 (punto 9, sfidanti): solo riferimenti IMMAGINE, nessun riferimento audio (voce descritta a parole).
# Prezzi di listino kie (models.yaml, 2026-10-03): 0.14 / 0.315 / 0.79 USD al secondo; il costo reale resta quello di creditsConsumed.
TAKE_MODELS = {
    "wan/3-0-video": {"label": "Wan 3.0", "rates": {"480P": 0.04, "720P": 0.09, "1080P": 0.16},
                      "default_resolution": "480P", "min_s": 2, "max_s": 30, "style": "wan"},
    "bytedance/seedance-2-5": {"label": "Seedance 2.5", "rates": {"480P": 0.14, "720P": 0.315, "1080P": 0.79},
                               "default_resolution": "480P", "min_s": 4, "max_s": 30, "style": "seedance"},
}


def take_payload(model, clip_prompt, refs, res, duration):
    """Corpo della richiesta kie per un clip 'take', secondo lo stile del modello (TAKE_MODELS[model]['style'])."""
    tm = TAKE_MODELS[model]
    style = tm.get("style", "wan")
    if style == "seedance":
        inp = {"prompt": clip_prompt, "reference_image_urls": refs, "generate_audio": True, "resolution": str(res).lower(),
               "aspect_ratio": "9:16", "duration": int(duration), "web_search": False}
    else:
        inp = {"prompt": clip_prompt, "reference_image_urls": refs, "resolution": res,
               "duration": int(duration), "aspect_ratio": "9:16", "audio": True}
    return {"model": model, "input": inp}

# Exit code: contratto per orchestrator/agenti esterni
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_APPROVAL = 2   # serve --max-usd (spesa non approvata) o cap superato
EXIT_NOT_READY = 3        # card incompleta (es. Clip generation spec mancante)
EXIT_SKIP = 4             # Formato non gestito da run.py (SKIP / MANUAL)


class PipelineExit(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def setup_stdio():
    """Evita UnicodeEncodeError su console Windows (cp1252)."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def slugify(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def norm_id(s):
    """Normalizza un ID pagina Notion (con/senza trattini, maiuscole)."""
    return re.sub(r"[^0-9a-f]", "", str(s or "").lower())


# ---------------------------------------------------------------- progetti

def load_projects():
    if not PROJECTS_MAP.exists():
        raise PipelineExit(EXIT_ERROR, f"ERROR: {PROJECTS_MAP.name} non trovato")
    cfg = yaml.safe_load(PROJECTS_MAP.read_text(encoding="utf-8")) or {}
    cfg.setdefault("relation_properties", ["Libro collegato"])
    projects = {}
    for pid, entry in (cfg.get("projects") or {}).items():
        e = dict(entry)
        e["id"] = str(pid)
        projects[norm_id(pid)] = e
    cfg["_by_id"] = projects
    return cfg


def find_project(cfg, ref):
    """Trova un progetto per ID pagina Notion o per nome (case-insensitive)."""
    by_id = cfg["_by_id"]
    if norm_id(ref) in by_id and norm_id(ref):
        return by_id[norm_id(ref)]
    for e in by_id.values():
        if (e.get("name") or "").strip().lower() == str(ref).strip().lower():
            return e
    return None


def resolve_project(props, cfg, project_arg=None, root_arg=None):
    """
    Ritorna dict {name, root, kind, id, contents_folder?}.
    Ordine: --project-root (override totale) > --project > relation della card.
    Se non risolvibile -> PipelineExit (MAI un fallback silenzioso a un progetto di default).
    """
    if root_arg:
        return {"name": project_arg or Path(root_arg).name, "root": str(root_arg),
                "kind": "custom", "id": None}
    if project_arg:
        e = find_project(cfg, project_arg)
        if not e:
            raise PipelineExit(EXIT_ERROR, f"ERROR: --project '{project_arg}' non presente in {PROJECTS_MAP.name}")
        return e

    ids = []
    for prop in cfg["relation_properties"]:
        v = props.get(prop) or []
        ids += v if isinstance(v, list) else [v]
    ids = [i for i in ids if i]
    if not ids:
        raise PipelineExit(
            EXIT_ERROR,
            "ERROR: la card non ha nessun progetto collegato "
            f"(property: {', '.join(cfg['relation_properties'])}). "
            "Collega la card in Notion, oppure passa --project <nome|id> / --project-root <path>.")
    mapped = [cfg["_by_id"][norm_id(i)] for i in ids if norm_id(i) in cfg["_by_id"]]
    if not mapped:
        raise PipelineExit(
            EXIT_ERROR,
            f"ERROR: progetto collegato non mappato: {', '.join(ids)}. Aggiungi in {PROJECTS_MAP.name}:\n"
            f'  "{ids[0]}":\n    name: <nome>\n    root: <path relativo al workspace>\n    kind: book|newsletter|product|other')
    return mapped[0]


# ---------------------------------------------------------------- formati

def load_formats():
    if not FORMATS_MAP.exists():
        raise PipelineExit(EXIT_ERROR, f"ERROR: {FORMATS_MAP.name} non trovato")
    cfg = yaml.safe_load(FORMATS_MAP.read_text(encoding="utf-8")) or {}
    fmts = {}
    for name, v in (cfg.get("formats") or {}).items():
        v = {"category": v} if isinstance(v, str) else dict(v)
        v["category"] = str(v.get("category", "AUTO")).upper()
        fmts[str(name).strip().lower()] = v
    cfg["_formats"] = fmts
    cfg.setdefault("default", "AUTO")
    cfg.setdefault("platform_folders", {})
    return cfg


def resolve_category(formato, clip_spec, fcfg):
    """Ritorna (categoria, warning|None). Categoria: A | B | SKIP | MANUAL."""
    entry = fcfg["_formats"].get((formato or "").strip().lower())
    warn = None
    if entry is None:
        cat = str(fcfg["default"]).upper()
        warn = f"Formato '{formato}' non presente in {FORMATS_MAP.name}: trattato come {cat}"
    else:
        cat = entry["category"]
        warn = entry.get("warn")
    if cat == "AUTO":
        cat = "A" if (clip_spec or "").strip() else "B"
    return cat, warn


def platform_folder(canale, fcfg):
    return fcfg["platform_folders"].get(canale, canale)


# ---------------------------------------------------------------- path

def resolve_project_root(root_str):
    p = Path(root_str)
    return p if p.is_absolute() else WORKSPACE / p


def find_contents_folder(project_root: Path):
    """Cartella '{NN}-Contenuti' esistente, oppure il primo numero libero dopo l'ultima tematica."""
    existing = sorted(project_root.glob("*-Contenuti"))
    if existing:
        return existing[0]
    numbered = sorted(d for d in project_root.iterdir() if d.is_dir() and d.name[:2].isdigit())
    if not numbered:
        return project_root / "01-Contenuti"
    return project_root / f"{max(int(d.name[:2]) for d in numbered) + 1:02d}-Contenuti"


def contents_folder(brief):
    root = resolve_project_root(brief["project_root"])
    if brief.get("contents_folder"):
        return root / brief["contents_folder"]
    return find_contents_folder(root)


def run_key(brief):
    """Chiave UNIVOCA per card: data-angle-id8. Evita che card con lo stesso 'angle' (es. A3) si sovrascrivano."""
    parts = [str(brief.get("date") or "nodate"),
             slugify(brief.get("angle") or "clip"),
             norm_id(brief.get("notion_card_id"))[:8]]
    return "-".join(p for p in parts if p)


def grezzi_dir(brief):
    return contents_folder(brief) / "_grezzi" / run_key(brief)


def voice_dir(brief):
    """Voce del video (lettura unica + pezzi per clip): {grezzi}/voice/. Vedi voice_split.py."""
    return grezzi_dir(brief) / "voice"


def is_avatar(brief):
    return (brief.get("sourcing", {}).get("model") or "") in AVATAR_MODELS


def is_take(brief):
    return (brief.get("sourcing", {}).get("model") or "") in TAKE_MODELS


def take_refs(brief, clip):
    """Riferimenti immagine di un clip 'take': quelli del clip (se presenti) altrimenti quelli del brief. Ordine = Image1, Image2..."""
    return list(clip.get("refs") or brief.get("sourcing", {}).get("refs") or [])


def is_url(ref):
    return str(ref).lower().startswith(("http://", "https://"))


def local_ref_path(brief_or_root, ref):
    """Riferimento locale (non URL) -> Path assoluto, risolto rispetto alla cartella del progetto (o gia' assoluto). URL -> None.
    Accetta il brief (dict) oppure la root del progetto (str/Path)."""
    if is_url(ref):
        return None
    root = brief_or_root["project_root"] if isinstance(brief_or_root, dict) else brief_or_root
    p = Path(str(ref))
    return p if p.is_absolute() else resolve_project_root(str(root)) / p


def ref_fingerprints(brief, refs):
    """Per ogni riferimento: sha1 del file se locale, None se URL. Serve a riconoscere un'immagine sostituita con lo stesso nome."""
    out = []
    for r in refs:
        lp = local_ref_path(brief, r)
        out.append(file_sha1(lp) if lp is not None and lp.exists() else None)
    return out


def clip_audio(brief, clip):
    """Pezzo di voce del clip (solo modelli avatar): {grezzi}/voice/{clip_id}.wav"""
    return voice_dir(brief) / f"{clip['id']}.wav"


def media_duration(path):
    import subprocess
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def file_sha1(path):
    import hashlib
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- stato clip + quote

def clip_state(brief, clip):
    """
    ('ok', path)      clip già generato su disco (e prompt/durata invariati)
    ('stale', path)   esiste ma prompt/durata nel brief sono cambiati dopo la generazione
    ('missing', None) da generare (spesa)
    """
    candidates = []
    p = clip.get("path")
    if p and not str(p).startswith("TODO"):
        candidates.append(Path(p))
    candidates.append(grezzi_dir(brief) / f"{clip['id']}.mp4")
    for c in candidates:
        if c.exists():
            side = c.with_suffix(".json")
            if side.exists():
                try:
                    s = json.loads(side.read_text(encoding="utf-8"))
                except Exception:
                    s = {}
                expected_ref = (clip.get("image_url") or brief.get("sourcing", {}).get("scene_master_url") or "").strip()
                used_refs = [r for r in (s.get("refs") or []) if r]
                ref_changed = bool(expected_ref and used_refs and used_refs != [expected_ref])
                if is_take(brief):
                    exp = take_refs(brief, clip)
                    used_refs = [r for r in (s.get("ref_sources") or s.get("refs") or []) if r]
                    if s.get("ref_sha1") is not None and ref_fingerprints(brief, exp) != s["ref_sha1"]:
                        used_refs = ["(file di riferimento cambiato)"]
                    res_now = brief["sourcing"].get("resolution") or TAKE_MODELS[brief["sourcing"]["model"]]["default_resolution"]
                    ref_changed = bool(exp and used_refs != exp) or (
                        (s.get("params") or {}).get("resolution") not in (None, res_now))
                audio_changed = False
                if is_avatar(brief) and s.get("audio_sha1"):
                    a = clip_audio(brief, clip)
                    audio_changed = (not a.exists()) or file_sha1(a) != s["audio_sha1"]
                if "prompt" in s and (
                        (s.get("prompt") or "").strip() != (clip.get("prompt") or "").strip()
                        or s.get("params", {}).get("duration") != clip.get("duration")
                        or ref_changed or audio_changed):
                    return "stale", c
            return "ok", c
    return "missing", None


def quote_brief(brief, regenerate=False, only=None):
    """Quote di spesa per un brief. Cat B / non-A = 0."""
    q = {"category": brief.get("category"), "clips": [], "clip_cost": 0.0,
         "scene_master": 0.0, "total": 0.0, "billable": 0, "stale": []}
    if brief.get("category") != "A":
        return q
    src = brief.get("sourcing", {})
    tier = src.get("tier", "Fast")
    avatar_rate = AVATAR_MODELS.get(src.get("model") or "")
    per_clip = COST_PER_CLIP.get(tier, 0.30)
    q["tier"], q["per_clip"] = tier, per_clip
    take = TAKE_MODELS.get(src.get("model") or "")
    take_rate = None
    if take:
        res = src.get("resolution") or take["default_resolution"]
        take_rate = take["rates"][res]
        q["tier"], q["per_second"], q["model"], q["resolution"] = f"{take['label']} {res}", take_rate, src.get("model"), res
    if avatar_rate:
        q["tier"], q["per_second"], q["model"] = src.get("model"), avatar_rate, src.get("model")
        q["missing_audio"] = []
    for c in src.get("clips", []):
        state, path = clip_state(brief, c)
        billable = state == "missing" or (state == "stale" and regenerate)
        if only and c["id"] not in only:
            billable = False        # --only: i clip non selezionati non si generano (e non costano)
        if state == "stale":
            q["stale"].append(c["id"])
        cost, secs = per_clip, None
        if take_rate:
            secs = float(c.get("duration") or 0)
            cost = round(secs * take_rate, 4)
        if avatar_rate:
            a = clip_audio(brief, c)
            if a.exists():
                secs = media_duration(a)
            else:
                secs = float(c.get("duration") or 8)
                if billable:
                    q["missing_audio"].append(c["id"])
            import math
            cost = round(math.ceil(secs) * avatar_rate, 4)   # arrotondato al secondo intero: stima prudente
        q["clips"].append({"id": c["id"], "state": state, "path": str(path) if path else None,
                           "billable": billable, "cost": cost if billable else 0.0, "seconds": secs})
    q["billable"] = sum(1 for c in q["clips"] if c["billable"])
    q["clip_cost"] = round(sum(c["cost"] for c in q["clips"]), 4)
    if q["billable"] and not take and not (src.get("scene_master_url") or "").strip():
        q["scene_master"] = SCENE_MASTER_COST
    q["total"] = round(q["clip_cost"] + q["scene_master"], 4)
    return q


def print_quote(q, label=""):
    if q["category"] != "A":
        print(f"[quote] {label}category={q['category']} - nessuna spesa API")
        return
    if q.get("per_second"):
        unit = "di video" if q.get("resolution") else "di audio"
        print(f"[quote] {label}model={q['model']}{' ' + q['resolution'] if q.get('resolution') else ''} @ ${q['per_second']:.3f}/s {unit}")
    else:
        print(f"[quote] {label}tier={q['tier']} @ ${q['per_clip']:.2f}/clip")
    for c in q["clips"]:
        secs = f" ({c['seconds']:.1f}s)" if c.get("seconds") else ""
        note = {"ok": "gia' generato (0)", "missing": f"DA GENERARE{secs} ${c['cost']:.2f}",
                "stale": "PROMPT/DURATA/IMMAGINE CAMBIATI dopo la generazione" +
                         (f" -> rigenera ${c['cost']:.2f}" if c["billable"] else " (usa --regenerate)")}[c["state"]]
        print(f"[quote]   {c['id']}: {note}")
    if q["scene_master"]:
        print(f"[quote]   scene master: ${q['scene_master']:.2f}")
    if q.get("missing_audio"):
        print(f"[quote]   ATTENZIONE: manca la voce per {', '.join(q['missing_audio'])} (stima sulla durata nominale). "
              f"Prima: voice_split.py")
    print(f"[quote] TOTALE SPESA: ${q['total']:.2f}")


COSTS_JSON = SCRIPT_DIR / "generations" / "costs.json"
MODELS_YAML = WORKSPACE / ".claude" / "skills" / "video-bootcamp" / "models.yaml"


def content_cap_usd():
    """Tetto di spesa per contenuto (retry inclusi) da models.yaml: budget_cap_per_content_usd. Default 5.0."""
    try:
        m = re.search(r"^budget_cap_per_content_usd:\s*([0-9.]+)", MODELS_YAML.read_text(encoding="utf-8"), re.M)
        return float(m.group(1)) if m else 5.0
    except OSError:
        return 5.0


def card_spent_usd(brief):
    """
    Spesa gia' sostenuta per questa card: somma delle voci di generations/costs.json E delle spese pagate e non registrate (costs.json.unrecorded-*,
    v8.6 punto 87: le STESSE fonti del cancello giornaliero, via spend_gate._gate_entries) il cui percorso asset
    INIZIA con la run_key della card (data-angle-id8) seguita da "/". Corrispondenza ESATTA: l'id8 da solo NON e' univoco
    (quasi tutte le pagine del database iniziano con 3e263594), quindi non si usa mai da solo (corretto il 15/10/2026).
    Limiti, a favore della sicurezza (mai falsi positivi): non agganciati i pilot storici (cartelle a mano) e le iterazioni
    girate con una data di pubblicazione diversa dall'attuale; per quelli vedi scripts/agent-package/card_costs.py --cost-match.
    Anche gli asset scritti con un percorso diverso da run_key/... (maiuscole, backslash, "./", assente) non si sommano: limite preesistente, dichiarato.
    Registro illeggibile, rotto o non elencabile, voce che non e' un oggetto o con un costo non valido = SpendGateError (approval_gate NEGA).
    costs.json ASSENTE (e nessuna voce non registrata): 0.0 come sempre.
    """
    if not norm_id(brief.get("notion_card_id")):
        return 0.0
    shared = spend_gate.content_spent_usd(run_key(brief))      # registro condiviso (Notion); None = backend locale
    if shared is not None:
        return round(shared, 4)                                # se Notion non risponde alza LedgerError: cancello chiuso
    prefix = run_key(brief) + "/"
    entries = spend_gate._gate_entries(COSTS_JSON)      # v8.6 (punto 87): costs.json + voci pagate e non registrate, senza doppioni, come spent_today_usd; errore = SpendGateError (nega)
    return round(sum((spend_gate.entry_cost_usd(e) for e in entries if str(e.get("asset", "")).startswith(prefix)), 0.0), 4)      # P4-ora: LedgerError se una voce della card ha un costo non valido


def approval_gate(q, max_usd, brief=None, allow_over_cap=False, retry=False):
    """
    None se la spesa e' approvata (o nulla). Altrimenti PipelineExit(EXIT_NEEDS_APPROVAL).
    Una approvazione = un run: --max-usd N vale solo per questa invocazione.
    Con brief: tetto CUMULATIVO per contenuto (spesa gia' sostenuta + quota di questo run <= budget_cap_per_content_usd
    di models.yaml). Superarlo richiede --allow-over-cap, cioe' una decisione esplicita di Cipriano (2026-10-14).
    Tetto GIORNALIERO (spend_gate, 18/10/2026): 10 USD al giorno, 8 per una prima generazione (retry=True o card con
    spesa gia' sostenuta o nessun brief = tentativo ripetuto, tetto 10). Registro illeggibile = cancello chiuso.
    Anche il tetto giornaliero si supera solo con --allow-over-cap.
    """
    if q["total"] <= 0:
        return
    if max_usd is None:
        raise PipelineExit(EXIT_NEEDS_APPROVAL,
                           f"APPROVAL REQUIRED: spesa stimata ${q['total']:.2f}. "
                           f"Nessuna chiamata a pagamento eseguita. Rilancia con --max-usd {q['total']:.2f} (o piu') per approvare.")
    if q["total"] > max_usd + 1e-9:
        raise PipelineExit(EXIT_NEEDS_APPROVAL,
                           f"CAP EXCEEDED: spesa stimata ${q['total']:.2f} > --max-usd {max_usd:.2f}. Nessuna chiamata eseguita.")
    if brief is not None and not allow_over_cap:
        try:
            spent, cap = card_spent_usd(brief), content_cap_usd()
        except spend_gate.SpendGateError as e:
            raise PipelineExit(EXIT_NEEDS_APPROVAL, str(e))
        if spent + q["total"] > cap + 1e-9:
            raise PipelineExit(EXIT_NEEDS_APPROVAL,
                               f"CONTENT CAP EXCEEDED: gia' speso ${spent:.2f} su questa card + ${q['total']:.2f} di questo run "
                               f"= ${spent + q['total']:.2f} > tetto per contenuto ${cap:.2f}. Nessuna chiamata eseguita. "
                               f"Serve la decisione di Cipriano: rilancia con --allow-over-cap solo se approva l'eccezione.")
    if not allow_over_cap:
        try:
            is_retry = bool(retry) or brief is None or card_spent_usd(brief) > 0
            spend_gate.daily_gate(q["total"], is_retry)
        except spend_gate.SpendGateError as e:
            raise PipelineExit(EXIT_NEEDS_APPROVAL, str(e))
        if brief is not None and str(brief.get("project")) == spend_gate.LAB_PROJECT:      # budget del lab (punto 41)
            try:
                info = spend_gate.lab_gate(q["total"])
            except spend_gate.SpendGateError as e:
                raise PipelineExit(EXIT_NEEDS_APPROVAL, str(e))
            for al in info["alerts"]:
                print(f"[lab] ATTENZIONE: con questa prova il mese arriva a ${info['after']:.2f} su ${info['monthly']:.0f} (oltre il {int(al * 100)}% del budget del lab).")
