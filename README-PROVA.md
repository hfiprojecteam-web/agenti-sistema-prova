# Repository di PROVA (punto 58, gradino 3)

Contiene SOLO codice dei hook di sicurezza e della catena del controllo di spesa (copiati dal repository di produzione al commit `3300088`), file finti e uno script di sonda. Nessun dato, nessuna chiave, nessun documento vero: `approvazioni.md`, lista bianca, regole e stato sono testi finti. Gli script `01_generate.py`, `run.py` e `00_scene_master.py` sono FINTI: stampano una riga e basta (nessuna generazione, nessuna rete).

Serve a un'unica cosa: provare nel cloud, senza rischio, che i hook arrivano, si caricano e bloccano. Si lancia con `python3 sonda_gradino3.py`.
