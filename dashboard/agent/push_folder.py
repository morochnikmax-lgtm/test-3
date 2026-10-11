#!/usr/bin/env python3
"""Läuft auf dem Kassen-/Büro-PC: schickt neue CSV-Exporte eines Ordners ans Dashboard.
Aufruf: python3 push_folder.py <Ordner> <vectron|televend> <http://dashboard:8080> [Token]
Per Windows-Aufgabenplanung beim Start ausführen. Reine Standardbibliothek."""
import os, sys, time, urllib.request
folder, source, base = sys.argv[1:4]
token = sys.argv[4] if len(sys.argv) > 4 else ""
sent = {}
while True:
    for n in os.listdir(folder):
        p = os.path.join(folder, n)
        if n.lower().endswith((".csv", ".txt")) and sent.get(n) != os.path.getmtime(p):
            r = urllib.request.Request(f"{base}/api/ingest/{source}", data=open(p, "rb").read(), method="POST")
            if token: r.add_header("Authorization", "Bearer " + token)
            try:
                urllib.request.urlopen(r, timeout=60); sent[n] = os.path.getmtime(p); print("gesendet", n)
            except Exception as e:
                print("Fehler", n, e)
    time.sleep(60)
