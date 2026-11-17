#!/usr/bin/env python
"""
correction-reminder.py - hook UserPromptSubmit (17/10/2026). Se il messaggio di Cipriano contiene una correzione o
un richiamo a qualcosa gia' detto, inietta un promemoria deterministico (trigger T09): scrivere subito la regola o la
correzione in REGOLE-FISSE.md e dirglielo. Qualsiasi errore: non stampa nulla.
"""
import json, re, sys

PAT = re.compile(
    r"(ti\s+sbagli|ti\s+sei\s+sbagliat|sbagli\s+spesso|ti\s+contraddic|ti\s+sei\s+contraddett|ti\s+dimentic|dimentichi|"
    r"ti\s+ricordi|te\s+l.avevo\s+(gi[aà]'?\s+)?detto|(te|ti)\s+(l.)?ho\s+gi[aà]'?\s+detto|non\s+ricordi|"
    r"ho\s+gi[aà]'?\s+detto|come\s+mai\s+non\s+ricord|da\s+ora\s+in\s+poi|ricorda\s+sempre|ricordati\s+sempre|"
    r"non\s+(dovevi|devi)\s+(farlo|fare)|ti\s+avevo\s+(gi[aà]'?\s+)?(detto|chiesto))", re.I)
MSG = ("PROMEMORIA (trigger T09, correzioni e regole durevoli): il messaggio di Cipriano sembra una correzione, un richiamo "
       "a qualcosa gia' detto o una regola nuova. Prima di rispondere controlla `Sistema/architettura-agenti/REGOLE-FISSE.md` e "
       "`STATO-CORRENTE.md` (non rispondere a memoria). Se e' un errore ripetuto o una regola durevole, scrivila subito in "
       "REGOLE-FISSE.md (senza duplicare) e dillo a Cipriano in una riga; se tocca CLAUDE.md o CLAUDE-BRAIN.md, segnala eventuali conflitti.")


# Trigger T11 (18/10/2026): link, repository o spunti nuovi portati in sessione -> valutare e mettere in lista con priorita', non implementare.
LINK = re.compile(r"(https?://\S+|\bwww\.\S+\.\S+|github\.com/\S+|\S+\.git\b)", re.I)
MSG_LINK = ("PROMEMORIA (trigger T11, spunti nuovi): il messaggio contiene un link o un repository. Regola 'fedelta' ai punti': NON "
            "implementare e NON scaricare codice subito. Valuta in sola lettura (sicurezza e validita'), inserisci un punto nella lista "
            "`Sistema/architettura-agenti/2026-10-15-punti-rimasti-e-pausa.md` (sezione K) con una priorita', continua il punto in corso "
            "e di' a Cipriano in una riga dove l'hai messo. Se Cipriano chiede esplicitamente di farlo subito, fallo.")


def main():
    try:
        ev = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace'))
        prompt = ev.get("prompt") or ev.get("user_prompt") or ""
        if PAT.search(prompt):
            print(MSG)
        if LINK.search(prompt):
            print(MSG_LINK)
    except Exception:                                    # noqa: BLE001
        return


if __name__ == "__main__":
    main()
