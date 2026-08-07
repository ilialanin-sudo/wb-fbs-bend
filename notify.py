#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Короткая сводка по FBS + ссылка на дашборд в Telegram.

ENV:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   — обязательны
  DASHBOARD_URL                          — адрес страницы, попадёт кнопкой в сообщение
  NOTIFY_PERIOD                          — какой период слать: yday (по умолчанию), week, d14, d28
"""
import json, os, sys, urllib.request, urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
URL = os.environ.get("DASHBOARD_URL", "").strip()
PER = os.environ.get("NOTIFY_PERIOD", "yday").strip() or "yday"

if not (TOKEN and CHAT):
    sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — сводка не отправлена")

d = json.load(open(os.path.join(BASE, "dashboard_data.json"), encoding="utf-8"))
meta = next((m for m in d["period_meta"] if m["key"] == PER), d["period_meta"][1])
tot = d["total"][PER]

n = lambda v: "—" if v is None else f"{round(v):,}".replace(",", " ")
p = lambda v, k=1: "—" if v is None else f"{v*100:.{k}f}".replace(".", ",") + " %"
dmy = lambda s: ".".join(reversed(s.split("-")))[:5] if s else ""


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


head = f"<b>FBS · {esc(d['brand'] or 'дашборд')}</b>\n<i>{esc(meta['label'].lower())}"
head += f", {dmy(meta['start'])}" + (f"–{dmy(meta['end'])}" if meta["start"] != meta["end"] else "")
head += " · только склад продавца</i>\n"

lines = [head]
lines.append(f"Заказов — <b>{n(tot['ord_qty'])} шт</b> на {n(tot['ord_sum'])} ₽")
lines.append(f"Доля FBS в кабинетах — {p(tot['fbs_share_qty'])} заказов")
lines.append(f"Ожидаемый выкуп — {p(tot['buyout'])} ≈ {n(tot['exp_buyout_qty'])} шт\n")

if tot.get("margin_wb") is not None:
    rev = tot["revenue"] or 1
    lines.append(f"Выручка ожидаемая — <b>{n(tot['revenue'])} ₽</b>")
    lines.append(f"  комиссия и СПП −{n(tot['commission'])} ({p(tot['commission']/rev,0)})")
    lines.append(f"  логистика −{n(tot['logistics'])} ({p(tot['logistics']/rev,0)})")
    lines.append(f"  реклама −{n(tot['ads'])} ({p(tot['ads']/rev,0)})")
    if tot.get("penalty"):
        lines.append(f"  штрафы −{n(tot['penalty'])}")
    if tot.get("cogs") is not None:
        lines.append(f"  себестоимость −{n(tot['cogs'])} ({p(tot['cogs']/rev,0)})")
    val = tot.get("gross") if tot.get("gross") is not None else tot["margin_wb"]
    lab = "Валовая маржа" if tot.get("gross") is not None else "После вычетов WB"
    lines.append(f"\n<b>{lab} — {n(val)} ₽</b>")
    pc = tot.get("gross_pct") if tot.get("gross") is not None else tot.get("margin_wb_pct")
    per_ord = tot.get("gross_per_order") if tot.get("gross") is not None else tot.get("margin_wb_per_order")
    lines.append(f"{p(pc)} от выручки · {n(per_ord)} ₽ с заказа")

# по кабинетам
lines.append("")
for c in d["cabinets"]:
    x = d["data"][c["key"]][PER]
    g = x.get("gross") if x.get("gross") is not None else x.get("margin_wb")
    gp = x.get("gross_pct") if x.get("gross") is not None else x.get("margin_wb_pct")
    lines.append(f"<b>{esc(c['title'])}</b> — {n(x['ord_qty'])} шт / {n(x['ord_sum'])} ₽ · маржа {n(g)} ₽ ({p(gp)})")

# предупреждения
warn = []
rr = [d["rates"][c["key"]] for c in d["cabinets"]]
if any("прокси" in (r.get("buyout_source") or "") for r in rr):
    warn.append("выкуп FBS ещё не дозрел — подставлен по кабинету")
if tot.get("cost_cover") is not None and tot["cost_cover"] < 0.95:
    warn.append(f"себестоимость известна для {p(tot['cost_cover'],0)} заказов")
if warn:
    lines.append("\n<i>" + esc("; ".join(warn)) + "</i>")

lines.append(f"\n<i>обновлено {esc(d['generated_human'])} МСК</i>")

txt = "\n".join(lines)
payload = {"chat_id": CHAT, "text": txt, "parse_mode": "HTML",
           "disable_web_page_preview": "true"}
if URL:
    payload["reply_markup"] = json.dumps(
        {"inline_keyboard": [[{"text": "Открыть дашборд", "url": URL}]]})

req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % TOKEN,
                             data=urllib.parse.urlencode(payload).encode(), method="POST")
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        print("telegram:", r.status)
except Exception as e:
    body = getattr(e, "read", lambda: b"")().decode()[:300]
    sys.exit(f"telegram не принял сообщение: {e} {body}")
