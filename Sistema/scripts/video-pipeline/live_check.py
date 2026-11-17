"""
live_check.py - PROVA DAL VIVO di scrittura del registro costi condiviso, SOLO sul database di PROVA.

    python live_check.py --target 6068f89a-f791-4242-97a1-25d890226041 --origin PC
    python live_check.py --target 6068f89a-f791-4242-97a1-25d890226041 --origin Cloud

Cosa fa (scrive righe vere ma da 0,01 USD, tutte con Voce che inizia per "LIVECHECK", tutte ANNULLATE alla fine, mai cancellate):
  1. selfcheck: fuso di Roma (tzdata), tetti, variabili d'ambiente (solo presenza), fonte della chiave. Nessun segreto.
  2. prenota 0,01 USD (Business = Test): la riga nasce 'Prenotato'.
  3. legge i totali: la prenotazione deve contare.
  4. conferma a 0,01: la riga diventa 'Confermato'.
  5. prenota e rilascia una seconda riga: diventa 'Annullato' e non conta.
  6. confine di mezzanotte: scrive una riga alle 00:10 di Roma di oggi e una alle 23:50 di ieri, poi rilegge i totali con "adesso" =
     00:30 di Roma di oggi espresso in UTC (22:30Z d'estate, 23:30Z d'inverno: in UTC e' ancora IERI). La riga di oggi deve contare per
     il giorno di Roma di oggi, quella di ieri no.
  7. pulizia: TUTTE le righe LIVECHECK create sono segnate 'Annullato' con Nota 'prova dal vivo' e importo 0 (mai cancellate).
  8. riepilogo con codici HTTP (conteggi) e stati. Non stampa MAI chiavi, intestazioni o corpi delle risposte.

Chiave: sul PC NOTION_TOKEN dall'ambiente, altrimenti la chiave del pipeline tramite notion_client (.env); nel cloud nessuna chiave: la
richiesta parte senza Authorization e la aggiunge il proxy (network secret). Rifiuta di lavorare su un database diverso da quello di
prova a meno che non si passi --i-know-this-is-not-the-test-db.
Uscita: 0 = tutte le prove passate e pulizia riuscita; 1 = almeno una prova fallita o pulizia incompleta; 2 = parametri non validi.
"""
import argparse
import datetime
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cost_ledger as cl                 # noqa: E402

REQUIRED_VERSION = 5
try:
    _v = int(getattr(cl, "__version__", "0"))
except ValueError:
    _v = 0
if _v < REQUIRED_VERSION:
    sys.exit(f"ERRORE: serve cost_ledger.py v{REQUIRED_VERSION} o successiva accanto a live_check.py (trovata: v{getattr(cl, '__version__', '< 4, senza numero')} "
             f"in {Path(cl.__file__).parent}). Nel cloud copiare ENTRAMBI i file in Sistema/scripts/video-pipeline/ prima di lanciare la prova.")

TEST_DB = "6068f89a-f791-4242-97a1-25d890226041"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ALL_STATES = cl.ACTIVE_STATES + cl.RELEASE_STATES
AMOUNT = 0.01


class Counting:
    """Avvolge il trasporto e conta (metodo, tipo di richiesta, codice HTTP). Non conserva ne' stampa intestazioni o corpi."""

    def __init__(self, inner):
        self.inner, self.counts = inner, {}

    def __call__(self, method, url, headers, body, timeout):
        kind = "query" if url.endswith("/query") else ("pagina" if "/pages" in url else "altro")
        key = f"{method} {kind}"
        try:
            status, hdrs, data = self.inner(method, url, headers, body, timeout)
        except Exception:
            self.counts.setdefault(key, {}).setdefault("errore-di-rete", 0)
            self.counts[key]["errore-di-rete"] += 1
            raise
        self.counts.setdefault(key, {}).setdefault(str(status), 0)
        self.counts[key][str(status)] += 1
        return status, hdrs, data


def _spool_leftovers(spool):
    """Nomi dei file di coda rimasti per `spool` (v8.2): il principale (se non vuoto), gli `.item-*`, i `.tmp`, i `.flush*` (in rinvio). Il `.lock` non conta.
    Si elenca con os.scandir (solleva gli errori: una cartella non elencabile NON e' 'coda vuota'); cartella assente = nessuna coda. Prima si leggeva solo
    il file principale e le voci in `.item-*` (dove va tutto dalla v7) non si vedevano: la prova dichiarava la coda vuota con voci dentro."""
    out = []
    try:
        it = os.scandir(str(spool.parent))
    except FileNotFoundError:
        return out
    with it:
        for e in it:
            n = os.path.normcase(e.name)
            base = os.path.normcase(spool.name)
            if not n.startswith(base) or n.endswith(".lock"):
                continue
            if n == base:
                if e.stat().st_size > 0:
                    out.append(e.name)
            else:
                out.append(e.name)
    return out


def _pipeline_dir_for_keys():
    ws = cl.find_workspace()
    return (ws / "Sistema" / "scripts" / "video-pipeline") if ws else None


def _resolve_token(token, token_source):
    """(token, fonte). token=None -> NOTION_TOKEN o, sul PC, la chiave del pipeline tramite notion_client; altrimenti nessuna (proxy)."""
    if token is not None:
        return token, (token_source or "parametro") if token else "proxy"
    import os
    if os.environ.get("NOTION_TOKEN"):
        return os.environ["NOTION_TOKEN"], "ambiente (NOTION_TOKEN)"
    pipe = _pipeline_dir_for_keys()
    if pipe is not None and pipe.is_dir():
        sys.path.append(str(pipe))             # in coda: il modulo cost_ledger resta quello di questa cartella
    try:
        import notion_client
        return notion_client.load_token(), ".env del pipeline tramite notion_client"
    except (ImportError, SystemExit):
        return "", "proxy"


class Report:
    def __init__(self, out):
        self.out, self.lines, self.steps = out, [], []

    def say(self, text=""):
        self.lines.append(str(text))
        self.out(str(text))

    def step(self, name, ok, detail=""):
        self.steps.append({"prova": name, "ok": bool(ok), "dettaglio": detail})
        self.say(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
        return ok


def run_live_check(target, origin, transport=None, token=None, token_source=None, clock=None, out=print, spool_dir=None, settle_s=1.0,
                   min_interval_s=0.35, sleep=time.sleep, allow_other_target=False):
    """Esegue la prova. Ritorna (ok, riepilogo). Rifiuta (anche se chiamata come funzione) un target diverso dal database di PROVA, salvo
    allow_other_target=True. `transport`/`clock`/`min_interval_s`/`sleep` servono solo ai test con Notion simulato."""
    if target != TEST_DB and not allow_other_target:
        out(f"RIFIUTATO: {str(target)[:8]}... non e' il database di PROVA ({TEST_DB[:8]}...): questa prova scrive righe. Nessuna richiesta fatta.")
        return False, {"esito": "RIFIUTATO: target diverso dal database di prova", "prove": [], "http": {}, "righe_create": 0, "righe_non_annullate": []}
    rep = Report(out)
    token, src = _resolve_token(token, token_source)
    counting = Counting(transport or cl._urllib_transport)
    clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
    spool_dir = Path(spool_dir) if spool_dir else Path(tempfile.mkdtemp(prefix="live_check_"))
    spool = spool_dir / "live-check-pending.jsonl"           # coda DEDICATA: mai quella di produzione
    ck = f"LIVECHECK-{clock().astimezone(datetime.timezone.utc):%Y%m%dT%H%M%S}"
    created, leftovers = [], []
    ledger = None
    rep.say(f"live_check: target={target[:8]}... origine={origin} fonte-chiave={src} contenuto={ck}")
    try:
        # ---------------------------------------------------------------- 1. selfcheck
        ok_sc, data = cl.selfcheck_data()
        rep.step("1 selfcheck (fuso di Roma, tzdata, tetti, variabili)", ok_sc,
                 f"fuso={data.get('fuso')} variabili={ {k: v for k, v in data.get('variabili', {}).items() if v == 'impostata'} }")
        be = cl.NotionBackend(data_source_id=target, token=token, token_source=src, transport=counting, min_interval_s=min_interval_s, sleep=sleep)
        ledger = cl.CostLedger(be, policy=cl.load_policy(), origin=origin, clock=clock, settle_s=settle_s, sleep=sleep, spool_path=spool)
        tz = cl._tz()
        now_real = clock().astimezone(tz)
        today = now_real.date()

        def guarded(name, fn):
            try:
                ok, detail = fn()
                rep.step(name, ok, detail)
            except Exception as e:                           # noqa: BLE001 - la prova continua e la pulizia gira comunque
                rep.step(name, False, f"{type(e).__name__}: {cl.redact(e, token)[:200]}")

        # ---------------------------------------------------------------- 2. prenotazione
        state = {}

        def reserve_probe(asset):
            """Prenota 0,01. Se il TETTO GIORNALIERO del database di prova e' gia' raggiunto (non e' un difetto: sono i tetti veri a lavorare)
            si ripete con allow_over_cap per poter provare comunque la scrittura, e lo si dice nel dettaglio."""
            kw = dict(amount_usd=AMOUNT, business="Test", content_key=ck, asset=asset, model="live-check", provider="Altro", retry=True,
                      note="LIVECHECK")
            try:
                return ledger.reserve(**kw), ""
            except cl.CapExceeded as e:
                return ledger.reserve(allow_over_cap=True, **kw), (f" [NOTA: tetto '{e.cap_name}' gia' raggiunto nel database di prova ({e.spent:.2f} USD "
                                                                  f"spesi): NON e' un difetto; la scrittura e' stata provata con allow_over_cap]")

        def s2():
            rk, note = reserve_probe("prenota-conferma")
            created.append(rk)
            state["rk1"] = rk
            rec = ledger.backend.find(rk)
            return (rec is not None and rec.state == "Prenotato" and abs(rec.amount_usd - AMOUNT) < 1e-9 and rec.business == "Test"
                    and rec.origin == origin), f"riga {rk.split('~')[0]} Prenotato {AMOUNT} USD{note}"
        guarded("2 prenotazione 0,01 USD (Business=Test)", s2)

        # ---------------------------------------------------------------- 3. totali contano la prenotazione
        def s3():
            c = ledger.totals(content_key=ck)
            d = ledger.totals(day="today", content_key=ck, states=("Prenotato",))
            return abs(c - AMOUNT) < 1e-9 and abs(d - AMOUNT) < 1e-9, f"contenuto={c} oggi(Prenotato)={d}"
        guarded("3 i totali contano la prenotazione", s3)

        # ---------------------------------------------------------------- 4. conferma
        def s4():
            ledger.confirm(state["rk1"], AMOUNT)
            rec = ledger.backend.find(state["rk1"])
            return rec is not None and rec.state == "Confermato" and abs(rec.amount_usd - AMOUNT) < 1e-9 and abs(ledger.totals(content_key=ck) - AMOUNT) < 1e-9, \
                "Confermato 0,01"
        guarded("4 conferma a 0,01", s4)

        # ---------------------------------------------------------------- 5. rilascio
        def s5():
            rk2, _note = reserve_probe("rilascio")
            created.append(rk2)
            ledger.release(rk2, "prova dal vivo: rilascio")
            rec = ledger.backend.find(rk2)
            return rec is not None and rec.state == "Annullato" and abs(ledger.totals(content_key=ck) - AMOUNT) < 1e-9, "Annullato, non conta"
        guarded("5 rilascio di una seconda prenotazione", s5)

        # ---------------------------------------------------------------- 6. confine di mezzanotte di Roma
        def s6():
            r_today = datetime.datetime(today.year, today.month, today.day, 0, 10, tzinfo=tz)
            yday = today - datetime.timedelta(days=1)
            r_yday = datetime.datetime(yday.year, yday.month, yday.day, 23, 50, tzinfo=tz)
            k_today = ledger.record(amount_usd=AMOUNT, business="Test", content_key=ck, asset="mezzanotte-oggi", model="live-check",
                                    note="LIVECHECK", when=r_today)
            created.append(k_today)
            k_yday = ledger.record(amount_usd=AMOUNT, business="Test", content_key=ck, asset="mezzanotte-ieri", model="live-check",
                                   note="LIVECHECK", when=r_yday)
            created.append(k_yday)
            back = ledger.backend.find(k_today)
            roundtrip = back is not None and back.when.astimezone(tz).strftime("%Y-%m-%d %H:%M") == r_today.strftime("%Y-%m-%d %H:%M")
            now_utc = datetime.datetime(today.year, today.month, today.day, 0, 30, tzinfo=tz).astimezone(datetime.timezone.utc)
            utc_date_is_yesterday = now_utc.date() == yday
            t_today = ledger.totals(day="today", now=now_utc, content_key=ck)         # il giorno di Roma di `now_utc` e' OGGI
            t_yday = ledger.totals(day=yday, content_key=ck)                          # il giorno di Roma di ieri
            # oggi: prenotazione confermata (0,01) + riga delle 00:10 (0,01); ieri: solo la riga delle 23:50 (0,01)
            # il filtro e' rifatto anche in locale, quindi un errore del "server" per eccesso verrebbe nascosto: si guarda anche la risposta
            # GREZZA di Notion per il giorno di oggi (deve contenere esattamente le 2 righe di oggi, non quella di ieri)
            ms, mu = cl.day_bounds(today, tz)
            raw = be._raw_query(be._filter(cl.ACTIVE_STATES, ms, mu, ck + "/", None, None))
            raw_ok = len(raw) == 2
            ok = (roundtrip and utc_date_is_yesterday and abs(t_today - 2 * AMOUNT) < 1e-9 and abs(t_yday - AMOUNT) < 1e-9 and raw_ok)
            return ok, (f"adesso={now_utc:%Y-%m-%dT%H:%MZ} (data UTC = ieri: {utc_date_is_yesterday}); riga 00:10 riletta alle "
                        f"{back.when.astimezone(tz):%H:%M} Roma ({roundtrip}); totale oggi={t_today} (atteso {2 * AMOUNT}), ieri={t_yday} (atteso {AMOUNT}); "
                        f"righe di oggi nella risposta grezza di Notion={len(raw)} (attese 2)")
        guarded("6 filtro di data a cavallo della mezzanotte di Roma", s6)
    except Exception as e:                                   # noqa: BLE001 - es. tzdata mancante, chiave non valida: si segnala e si ripulisce
        rep.step("esecuzione", False, f"{type(e).__name__}: {cl.redact(e, token)[:200]}")
    finally:
        # ---------------------------------------------------------------- 7. pulizia: tutto Annullato, mai cancellato
        cleaned = 0
        if ledger is not None:
            keys = list(created)
            try:
                for r in ledger.backend.query(states=ALL_STATES, prefix=ck + "/"):
                    if r.run_key not in keys:
                        keys.append(r.run_key)
            except Exception as e:                           # noqa: BLE001
                rep.say(f"   (elenco delle righe LIVECHECK non leggibile: {type(e).__name__}: {cl.redact(e, token)[:120]})")
            for k in keys:
                try:
                    ledger.backend.update(k, state=cl.STATE_CANCELLED, amount_usd=0.0, note="prova dal vivo")
                    cleaned += 1
                except Exception as e:                       # noqa: BLE001
                    leftovers.append(k)
                    rep.say(f"   ATTENZIONE: riga {k.split('~')[0]} NON annullata ({type(e).__name__}): va annullata a mano")
            try:
                still_active = ledger.backend.query(states=cl.ACTIVE_STATES, prefix=ck + "/")
            except Exception:                                # noqa: BLE001
                still_active = None
            clean_ok = not leftovers and still_active == []
            rep.step("7 pulizia: righe LIVECHECK annullate (Nota 'prova dal vivo'), nessuna cancellata", clean_ok,
                     f"annullate={cleaned} non annullate={len(leftovers)} ancora attive={'?' if still_active is None else len(still_active)}")
        # coda: la prova non deve lasciare nulla in sospeso
        try:
            pend = _spool_leftovers(spool)
            pend_err = None
        except OSError as e:                                  # cartella non elencabile: NON si dichiara la coda vuota
            pend, pend_err = [], f"{type(e).__name__}"
        if pend or pend_err:
            rep.step("coda locale della prova vuota", False,
                     (f"coda non elencabile ({pend_err}) in {spool.parent}: non verificabile, rifare la prova" if pend_err
                      else f"{len(pend)} file di coda rimasti in {spool.parent} ({', '.join(pend[:3])}; Notion non ha risposto): rifare la prova"))

    # ---------------------------------------------------------------- 8. riepilogo
    text = "\n".join(rep.lines)
    bad = (token and token in text) or re.search(r"(secret_|ntn_)[A-Za-z0-9_\-]{6,}", text)
    summary = {"target": target[:8] + "...", "origine": origin, "fonte_chiave": src,
               "prove": [(s["prova"], "PASS" if s["ok"] else "FAIL") for s in rep.steps],
               "http": counting.counts, "righe_create": len(created), "righe_non_annullate": leftovers}
    ok = all(s["ok"] for s in rep.steps) and not bad and bool(rep.steps)
    summary["esito"] = "OK" if ok else "FALLITO"
    if bad:
        summary["esito"] = "FALLITO: un segreto e' comparso nell'output"
        ok = False
    rep.say("RIEPILOGO " + json.dumps(summary, ensure_ascii=False))
    return ok, summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Prova dal vivo del registro costi sul database di PROVA (scrive righe LIVECHECK e le annulla).")
    ap.add_argument("--target", default=TEST_DB, help=f"data source del database di prova (default {TEST_DB})")
    ap.add_argument("--origin", default="PC", choices=cl.ORIGINI)
    ap.add_argument("--i-know-this-is-not-the-test-db", action="store_true", dest="force")
    a = ap.parse_args(argv)
    if not UUID_RE.fullmatch(a.target):
        print("ERRORE: --target deve essere l'identificativo (UUID) del data source.", file=sys.stderr)
        return 2
    if a.target != TEST_DB and not a.force:
        print(f"ERRORE: {a.target[:8]}... non e' il database di PROVA ({TEST_DB[:8]}...). Questa prova scrive righe: rifiutata. "
              f"Per sceglierlo davvero: --i-know-this-is-not-the-test-db.", file=sys.stderr)
        return 2
    ok, _ = run_live_check(a.target, a.origin, allow_other_target=a.force)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
