#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""template.html + dashboard_data.json → готовая страница."""
import json, os

BASE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(BASE, "dist")
os.makedirs(DIST, exist_ok=True)

data = json.load(open(os.path.join(BASE, "dashboard_data.json"), encoding="utf-8"))
tpl = open(os.path.join(BASE, "template.html"), encoding="utf-8").read()

payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")) \
    .replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
html = tpl.replace("__DATA__", payload)

for name in ("index.html", "wb_fbs_dashboard.html"):
    with open(os.path.join(DIST, name), "w", encoding="utf-8") as f:
        f.write(html)
# сырой JSON в публикацию не кладём: данные и так вшиты в страницу,
# а отдельный файл поисковики индексируют мимо noindex
with open(os.path.join(DIST, "robots.txt"), "w", encoding="utf-8") as f:
    f.write("User-agent: *\nDisallow: /\n")

kb = len(html.encode()) / 1024
print(f"dist/index.html — {kb:.0f} КБ, данные на {data['generated_human']}")
