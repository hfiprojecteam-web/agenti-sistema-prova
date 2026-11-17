#!/usr/bin/env python
"""
session-digest.py - hook SessionStart (startup|resume|compact). Ripropone a Claude le regole fisse e lo stato corrente
all'avvio, alla ripresa e dopo ogni compattazione, cosi' non dipendono dalla memoria della conversazione (17/10/2026).
Legge due file brevi e li stampa come contesto aggiuntivo. Qualsiasi errore: non stampa nulla (exit 0).
"""
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
FILES = [ROOT / "Sistema" / "architettura-agenti" / "REGOLE-FISSE.md",
         ROOT / "Sistema" / "architettura-agenti" / "STATO-CORRENTE.md"]
MAX_CHARS = 9000


def main():
    try:
        try:
            json.load(sys.stdin)
        except Exception:                                # noqa: BLE001
            pass
        parts = []
        for f in FILES:
            if f.exists():
                parts.append(f.read_text(encoding="utf-8").strip())
        if not parts:
            return
        text = "PROMEMORIA AUTOMATICO (regole fisse e stato corrente, riletti da file):\n\n" + "\n\n---\n\n".join(parts)
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "\n[...troncato: leggere i file per intero]"
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}, ensure_ascii=False))
    except Exception:                                    # noqa: BLE001
        return


if __name__ == "__main__":
    main()
