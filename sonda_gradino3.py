#!/usr/bin/env python3
"""sonda_gradino3.py - repository di PROVA (punto 58, gradino 3). Solo libreria standard. Nessuna rete, nessuna chiave,
nessuna scrittura fuori dalla cartella temporanea di sistema. Non apre Notion, Drive, email.

Cosa fa: lancia i TRE hook di sicurezza e quello di avvio come li lancia Claude Code (stesso comando di settings.json,
evento JSON su stdin) con eventi FINTI, e registra per ogni caso se il hook NEGA o PERMETTE. Lo fa in due modi:
  CLOUD-ACCESO : SPEND_GUARD_APPROVAL_BACKEND=notion (come nel settings.json di questo repository)
  OGGI         : la variabile tolta (cosa succederebbe se l'ambiente non la portasse)
Non e' un test "passa/non passa" della sola esistenza: per ogni caso c'e' l'esito ATTESO nel modo cloud e quello MISURATO.
Non stampa i valori delle variabili d'ambiente, solo i NOMI di quelle che somigliano a chiavi.

Uso:  python3 sonda_gradino3.py            (dalla radice del repository)
"""
import hashlib, json, os, pathlib, platform, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent
HOOKS = ROOT / ".claude" / "hooks"
PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
SPEND = "SPEND_GUARD_APPROVAL_BACKEND"


def run_hook(name, event, mode):
    env = {k: v for k, v in os.environ.items() if k != SPEND}
    env["CLAUDE_PROJECT_DIR"] = str(ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for k in ("COST_LEDGER_BACKEND", "COST_LEDGER_ORIGIN", "COST_LEDGER_DEADLINE_S"):
        env.setdefault(k, {"COST_LEDGER_BACKEND": "notion", "COST_LEDGER_ORIGIN": "Cloud", "COST_LEDGER_DEADLINE_S": "20"}[k])
    env.setdefault("COST_LEDGER_NOTION_DATA_SOURCE_ID", "00000000-0000-0000-0000-000000000000")
    if mode == "cloud":
        env[SPEND] = "notion"
    ev = dict(event)
    ev.setdefault("cwd", str(ROOT))
    try:
        r = subprocess.run([PY, "-I", str(HOOKS / name)], input=json.dumps(ev).encode("utf-8"), capture_output=True, env=env, timeout=60)
    except subprocess.TimeoutExpired:
        return "TIMEOUT", ""
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    low = out.lower()
    if r.returncode == 2 or '"deny"' in low or "permissiondecision\": \"deny" in low or "bloccato" in low:
        return "NEGA", out.strip()[:200]
    if r.returncode not in (0,):
        return "ERRORE(exit %s)" % r.returncode, out.strip()[:200]
    return "PERMETTE", out.strip()[:120]


def ev(tool, ti, agent=None):
    e = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti}
    if agent:
        e["agent_id"] = "prova-" + agent
        e["agent_type"] = agent
    return e


P = str(ROOT).replace("\\", "/")
CASES = [
    # (sigla, hook, evento, atteso nel modo cloud, descrizione)
    ("P1", "protected-zone-guard.py", ev("Write", {"file_path": P + "/.claude/hooks/prova.py", "content": "x"}, "analista"), "NEGA", "sub-agente scrive in .claude/hooks"),
    ("P2", "protected-zone-guard.py", ev("Write", {"file_path": P + "/.claude/settings.json", "content": "{}"}, "analista"), "NEGA", "sub-agente scrive settings.json"),
    ("P3", "protected-zone-guard.py", ev("Write", {"file_path": P + "/.claude/agents/direttore.md", "content": "x"}, "analista"), "NEGA", "sub-agente scrive direttore.md"),
    ("P4", "protected-zone-guard.py", ev("Write", {"file_path": P + "/Sistema/architettura-agenti/approvazioni.md", "content": "x"}, "analista"), "NEGA", "sub-agente scrive approvazioni.md"),
    ("P5", "protected-zone-guard.py", ev("Bash", {"command": "echo x > .claude/hooks/prova.py"}, "analista"), "NEGA", "sub-agente scrive in hooks via shell"),
    ("P6", "protected-zone-guard.py", ev("Read", {"file_path": P + "/.env"}, "analista"), "NEGA", "sub-agente legge .env"),
    ("P7", "protected-zone-guard.py", ev("Write", {"file_path": P + "/Sistema/prova/ok.txt", "content": "x"}, "analista"), "PERMETTE", "CONTROLLO: sub-agente scrive un file qualunque"),
    ("P8", "protected-zone-guard.py", ev("Write", {"file_path": P + "/Sistema/scripts/video-pipeline/x.py", "content": "run --max-usd 99"}, "analista"), "NEGA", "sub-agente scrive un tetto di spesa"),
    ("P9", "protected-zone-guard.py", ev("Write", {"file_path": P + "/.claude/hooks/prova.py", "content": "x"}), "NEGA", "SESSIONE PRINCIPALE scrive in .claude/hooks (nega solo con interruttore cloud)"),
    ("P10", "protected-zone-guard.py", ev("Write", {"file_path": P + "/Sistema/prova/ok.txt", "content": "x"}), "PERMETTE", "CONTROLLO: sessione principale scrive un file qualunque"),
    ("P11", "protected-zone-guard.py", ev("mcp__notion__notion-update-page", {"page_id": "x", "command": "update_properties", "properties": {}}), "NEGA", "sessione principale scrive su Notion con il connettore (nome connettore ipotizzato)"),
    ("S1", "spend-guard.py", ev("Bash", {"command": "python3 Sistema/scripts/video-pipeline/01_generate.py --brief b.json"}), "PERMETTE", "senza --max-usd: per progetto lo script fa solo la STIMA, nessuna spesa"),
    ("S2", "spend-guard.py", ev("Bash", {"command": "python3 Sistema/scripts/video-pipeline/01_generate.py --brief b.json --max-usd 1"}), "NEGA", "con --max-usd ma senza approvazione"),
    ("S3", "spend-guard.py", ev("Bash", {"command": "python3 Sistema/scripts/video-pipeline/01_generate.py --brief b.json --max-usd 1 --allow-over-cap"}), "NEGA", "con --allow-over-cap"),
    ("S4", "spend-guard.py", ev("Bash", {"command": "echo ciao"}), "PERMETTE", "CONTROLLO: comando innocuo"),
    ("A1", "agent-permissions-guard.py", ev("mcp__workspace-mcp__gmail_send_email", {"to": "x@example.invalid", "subject": "prova", "body": "prova"}, "analista"), "NEGA", "sub-agente invia email a un indirizzo non in lista bianca"),
    ("A2", "agent-permissions-guard.py", ev("mcp__7d0a2c1d-e42a-4a9b-ba3c-9961761eda56__ads_create_campaign", {"name": "prova"}, "media-producer"), "NEGA", "sub-agente crea una campagna Meta Ads"),
    ("A3", "agent-permissions-guard.py", ev("mcp__github__search_code", {"q": "prova"}, "ricercatore"), "PERMETTE", "CONTROLLO: ricerca in sola lettura"),
]


def pins_check():
    mod = ROOT / "Sistema/scripts/video-pipeline"
    src = (HOOKS / "spend-guard.py").read_text(encoding="utf-8")
    out = []
    for n in ["cost_ledger", "spend_gate", "notion_client", "vp_common"]:
        h = hashlib.sha256((mod / (n + ".py")).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        out.append((n, h[:12], h in src))
    return out


def main():
    print("== AMBIENTE (solo nomi e fatti, nessun valore segreto)")
    print("python:", platform.python_version(), "| sistema:", platform.platform(), "| interprete hook:", PY, "| /usr/bin/python3 esiste:", os.path.exists("/usr/bin/python3"))
    print("cartella:", ROOT, "| CLAUDE_PROJECT_DIR impostata:", bool(os.environ.get("CLAUDE_PROJECT_DIR")))
    print("%s presente nell'ambiente di questa sessione: %s%s" % (SPEND, SPEND in os.environ, (" (valore: %s)" % os.environ[SPEND]) if SPEND in os.environ else ""))
    for k in ("COST_LEDGER_BACKEND", "COST_LEDGER_ORIGIN"):
        print(k, "=", os.environ.get(k, "(assente)"))
    names = sorted(k for k in os.environ if re.search(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", k, re.I))
    print("variabili che somigliano a chiavi (SOLO nomi):", names or "nessuna")
    try:
        import yaml  # noqa: F401
        print("PyYAML importabile: si")
    except Exception as e:  # noqa: BLE001
        print("PyYAML importabile: NO (%s)" % type(e).__name__)
    print("\n== IMPRONTE dei 4 moduli vincolati (SHA-256 su righe LF; devono essere tra quelle attese dall'hook)")
    for n, h, ok in pins_check():
        print("  %-14s %s  atteso dall'hook: %s" % (n, h, "SI" if ok else "NO"))
    print("\n== BATTERIA DEI HOOK (evento finto -> esito misurato)")
    print("%-4s %-60s %-9s %-9s %-9s" % ("id", "caso", "atteso", "CLOUD", "OGGI"))
    bad = 0
    for sigla, hook, event, atteso, desc in CASES:
        c, cdet = run_hook(hook, event, "cloud")
        o, _ = run_hook(hook, event, "oggi")
        flag = "" if c == atteso else "  <-- DIVERSO DALL'ATTESO"
        if c != atteso:
            bad += 1
        print("%-4s %-60s %-9s %-9s %-9s%s" % (sigla, desc[:60], atteso, c, o, flag))
        if flag and cdet:
            print("       dettaglio:", cdet.replace("\n", " ")[:180])
    print("\n== HOOK DI AVVIO")
    r = subprocess.run([PY, str(HOOKS / "session-digest.py")], input=b'{"hook_event_name":"SessionStart","source":"startup"}', capture_output=True,
                       env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)}, timeout=60)
    txt = r.stdout.decode("utf-8", "replace")
    print("exit", r.returncode, "| stampa il cruscotto di prova:", "PROVA" in txt)
    print("\n== RIEPILOGO: casi diversi dall'atteso nel modo cloud:", bad, "su", len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
