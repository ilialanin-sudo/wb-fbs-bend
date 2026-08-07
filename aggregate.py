#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Свод FBS по двум кабинетам + юнит-экономика → dashboard_data.json.

Главное соглашение: базой везде служит СЫРОЙ заказ — строка из /supplier/orders
независимо от isCancel. У WB isCancel=true означает и отмену клиентом, и невыкуп,
и проставляется он с задержкой в неделю-полторы. Поэтому «живые» заказы свежего
дня завышены примерно вдвое, и любая экономика от них врёт.
"""
import os, sys, json, gzip, datetime, collections

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
STATE = os.path.join(BASE, "state")

MSK = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime.now(MSK)
TODAY = NOW.date()
YEST = TODAY - datetime.timedelta(days=1)
FBS = "Склад продавца"


def load(name, default=None):
    p = os.path.join(DATA, name + ".json.gz")
    if not os.path.exists(p):
        return default
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def jload(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


CFG = jload(os.path.join(BASE, "config.json"), {}) or {}
RATES = jload(os.path.join(STATE, "rates.json"), {}) or {}
COST = jload(os.path.join(BASE, "cost.json"), {}) or {}

WINDOW = int(CFG.get("window_days", 28))
MATURITY = int(CFG.get("maturity_days", 14))      # когда когорта считается дозревшей
MIN_MATURE = int(CFG.get("min_mature_orders", 300))
ADS_MODE = CFG.get("ads_mode", "fbs_share")
BRAND = CFG.get("brand", "")
OVR = CFG.get("override", {}) or {}
CABS = [(str(c["key"]), c.get("title") or str(c["key"]).upper())
        for c in (CFG.get("cabinets") or []) if c.get("key")]
if not CABS:
    # конфиг старый — восстанавливаем список кабинетов по тому, что реально выгружено
    import glob
    CABS = [(os.path.basename(p)[len("orders_"):-len(".json.gz")].lower(),
             os.path.basename(p)[len("orders_"):-len(".json.gz")].upper())
            for p in sorted(glob.glob(os.path.join(DATA, "orders_*.json.gz")))]
    if CABS:
        print("в config.json нет блока cabinets — беру кабинеты из выгрузок:",
              ", ".join(k for k, _ in CABS))
if not CABS:
    sys.exit("не найден ни один кабинет: добавьте блок \"cabinets\" в config.json")
FALLBACK = CFG.get("rates_fallback", "cabinet")   # cabinet | none

cost_by_nm = {int(k): float(v) for k, v in (COST.get("by_nmid") or {}).items() if v}
cost_by_art = {str(k).strip(): float(v) for k, v in (COST.get("by_article") or {}).items() if v}


def unit_cost(r):
    return cost_by_nm.get(r.get("nmId")) or cost_by_art.get(str(r.get("supplierArticle", "")).strip())


def ovr(cab, field):
    v = (OVR.get(cab) or {}).get(field)
    return float(v) if v not in (None, "", 0) else None


def monday(d):
    return d - datetime.timedelta(days=d.weekday())


PERIODS = [
    ("today", "Сегодня", TODAY, TODAY, True),
    ("yday", "Вчера", YEST, YEST, False),
    ("week", "Неделя с пн", monday(TODAY), TODAY, True),
    ("d14", "14 дней", YEST - datetime.timedelta(days=13), YEST, False),
    ("d28", "28 дней", YEST - datetime.timedelta(days=27), YEST, False),
]
PERIOD_META = [dict(key=k, label=lb, start=s.isoformat(), end=e.isoformat(),
                    partial=p, days=(e - s).days + 1) for k, lb, s, e, p in PERIODS]

CANCEL_WB = {"canceled", "canceled_by_client", "declined_by_client"}

# Пороги скорости отгрузки, по которым WB считает поправку к комиссии.
# WB объявил их временными — если поменяются, правится только config.json.
SPEED_DEFAULT = [
    {"to": 13, "label": "до 13 ч", "delta": -5.0},
    {"to": 42, "label": "13–42 ч", "delta": -3.5},
    {"to": 48, "label": "42–48 ч", "delta": 0.0},
    {"to": 54, "label": "48–54 ч", "per_hour": 0.30, "from_hour": 48},
    {"to": 60, "label": "54–60 ч", "per_hour": 0.35, "from_hour": 48},
    {"to": None, "label": "свыше 60 ч", "per_hour": 0.45, "from_hour": 48},
]
SPEED = CFG.get("speed_buckets") or SPEED_DEFAULT
BEST_DELTA = min(b.get("delta", 0) for b in SPEED)
# Чем заканчивается отсчёт: scan — приёмка на стороне WB, closed — закрытие поставки
# продавцом. Официальной формулировки WB не даёт; скан позже закрытия на 0,6–1,9 ч,
# и около 5 % заданий из-за этого переезжают в худшую корзину. Берём скан как более
# строгую базу, с откатом на закрытие, пока WB не проставил отметку.
SPEED_BASIS = CFG.get("speed_basis", "scan")


def speed_bucket(h):
    for i, b in enumerate(SPEED):
        if b["to"] is None or h < b["to"]:
            return i
    return len(SPEED) - 1


def speed_delta(h):
    """Поправка к комиссии в процентных пунктах за такую скорость отгрузки."""
    b = SPEED[speed_bucket(h)]
    if "per_hour" in b:
        return b["per_hour"] * max(0.0, h - b.get("from_hour", 48))
    return b.get("delta", 0.0)


def _ts(s):
    return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))

ASM_PERIODS = [("today", "Сегодня", 0, 0), ("yday", "Вчера", 1, 1),
               ("d3", "3 дня", 2, 0), ("week", "Неделя", 6, 0)]


def assembly(cab):
    """Операционная картина: что висит на сборке, что отгружено, что отменено.

    Источник — раздел сборочных заданий (marketplace-api), а не статистика.
    Дата отгрузки берётся из поставки: задание считается отгруженным в тот день,
    когда закрыли поставку, в которой оно уехало.
    """
    mp = load(f"mp_{cab}")
    if not mp:
        return None
    orders = mp.get("orders") or []
    closed = {s["id"]: (s.get("closedAt") or "")[:10]
              for s in (mp.get("supplies") or []) if s.get("done") and s.get("closedAt")}
    open_sup = {s["id"] for s in (mp.get("supplies") or []) if not s.get("done")}

    def cls(o):
        # Важно: отказ покупателя приходит на заказ, который уже уехал в поставке.
        # Для операционной картины это отгрузка, а не отмена — иначе у дозревших
        # дней «отгружено» падает вдвое, хотя товар физически уехал.
        # Отменой считается только то, что не успело уехать.
        ship = o.get("supplyId") in closed
        if o.get("supplierStatus") == "cancel" or (o.get("wbStatus") in CANCEL_WB and not ship):
            return "cancel"
        if o.get("wbStatus") == "defect":
            return "defect"
        if ship:
            return "shipped"
        return "assembling"

    rows = []
    for o in orders:
        d = (o.get("createdAt") or "")[:10]
        if not d:
            continue
        rows.append(dict(day=d, state=cls(o),
                         ship_day=closed.get(o.get("supplyId")),
                         price=o.get("price") or 0,
                         taken=o.get("supplierStatus") in ("confirm", "complete"),
                         in_open=o.get("supplyId") in open_sup))

    # снимок «прямо сейчас»
    wait = [r for r in rows if r["state"] == "assembling"]
    now = dict(
        total=len(wait),
        total_sum=round(sum(r["price"] for r in wait), 2),
        fresh=sum(1 for r in wait if not r["taken"]),          # ещё не взяты в работу
        in_supply=sum(1 for r in wait if r["in_open"]),        # собраны в открытую поставку
        oldest=min((r["day"] for r in wait), default=None),
    )

    # по периодам: слева — что стало с заданиями, созданными в периоде,
    # справа — сколько физически уехало в эти дни
    per = {}
    for key, label, back, back_hi in ASM_PERIODS:
        lo = (TODAY - datetime.timedelta(days=back)).isoformat()
        hi = (TODAY - datetime.timedelta(days=back_hi)).isoformat()
        sub = [r for r in rows if lo <= r["day"] <= hi]
        per[key] = dict(
            label=label, start=lo, end=hi, days=back - back_hi + 1,
            created=len(sub),
            created_sum=round(sum(r["price"] for r in sub), 2),
            shipped=sum(1 for r in sub if r["state"] == "shipped"),
            cancel=sum(1 for r in sub if r["state"] == "cancel"),
            defect=sum(1 for r in sub if r["state"] == "defect"),
            assembling=sum(1 for r in sub if r["state"] == "assembling"),
            shipped_in_days=sum(1 for r in rows if r["ship_day"] and lo <= r["ship_day"] <= hi),
            shipped_in_days_sum=round(sum(r["price"] for r in rows
                                          if r["ship_day"] and lo <= r["ship_day"] <= hi), 2),
        )

    # дневной ряд отгрузок
    ship_days = collections.defaultdict(lambda: dict(qty=0, sum=0.0))
    made_days = collections.defaultdict(lambda: dict(qty=0, sum=0.0))
    for r in rows:
        if r["ship_day"]:
            ship_days[r["ship_day"]]["qty"] += 1
            ship_days[r["ship_day"]]["sum"] += r["price"]
        made_days[r["day"]]["qty"] += 1
        made_days[r["day"]]["sum"] += r["price"]
    days = sorted(set(ship_days) | set(made_days))
    series = [dict(date=d,
                   shipped=ship_days[d]["qty"], shipped_sum=round(ship_days[d]["sum"], 2),
                   created=made_days[d]["qty"], created_sum=round(made_days[d]["sum"], 2))
              for d in days]

    # --------------------------------------------- где сейчас едут заказы
    # У WB нет отметки «прибыл в ПВЗ», но есть текущий статус. Считаем по когортам:
    # какая доля заказов возраста N дней уже доехала до ПВЗ или дальше.
    AT_PVZ = {"ready_for_pickup", "sold", "canceled_by_client", "defect"}
    STAGE = [("waiting", "В обработке и сборке"), ("sorted", "В пути"),
             ("ready_for_pickup", "Ждут на ПВЗ"), ("sold", "Получены покупателем"),
             ("canceled_by_client", "Отменены покупателем"),
             ("declined_by_client", "Отказ до сборки"), ("defect", "Брак")]
    now_u = datetime.datetime.now(datetime.timezone.utc)
    stage = collections.defaultdict(list)
    by_age = collections.defaultdict(lambda: [0, 0])
    for o in orders:
        if not o.get("createdAt"):
            continue
        age = (now_u - _ts(o["createdAt"])).total_seconds() / 86400
        stage[o.get("wbStatus")].append(age)
        if o.get("wbStatus") != "declined_by_client":
            b = by_age[int(age)]
            b[0] += 1
            if o.get("wbStatus") in AT_PVZ:
                b[1] += 1
    def med(v):
        v = sorted(v)
        return round(v[len(v) // 2], 1) if v else None
    funnel = [dict(key=k, label=lb, qty=len(stage.get(k) or []),
                   median_age=med(stage.get(k) or []))
              for k, lb in STAGE if stage.get(k)]
    pvz_curve = [dict(age=a, qty=by_age[a][0],
                      share=round(by_age[a][1] / by_age[a][0], 3))
                 for a in sorted(by_age) if by_age[a][0] >= 20]
    to_pvz = next((c["age"] for c in pvz_curve if c["share"] >= 0.5), None)

    # ------------------------------------------------ скорость отгрузки
    sup = {x["id"]: x for x in (mp.get("supplies") or [])}
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    by_day = collections.defaultdict(lambda: dict(qty=0, sum=0.0,
                                                  b=[0] * len(SPEED), bs=[0.0] * len(SPEED),
                                                  delta_w=0.0))
    queue = dict(qty=0, b=[0] * len(SPEED), lost=0.0, sum=0.0)
    fb_qty = 0
    for o in orders:
        if not o.get("createdAt"):
            continue
        st = cls(o)
        if st in ("cancel", "defect"):
            continue
        price = o.get("price") or 0
        if st == "shipped":
            sp = sup.get(o.get("supplyId")) or {}
            end = sp.get("scanDt") if SPEED_BASIS == "scan" else None
            fallback = end is None
            end = end or sp.get("closedAt")
            if not end:
                continue
            h = (_ts(end) - _ts(o["createdAt"])).total_seconds() / 3600
            if fallback:
                fb_qty += 1
            d = o["createdAt"][:10]
            a = by_day[d]
            i = speed_bucket(h)
            a["qty"] += 1
            a["sum"] += price
            a["b"][i] += 1
            a["bs"][i] += price
            a["delta_w"] += speed_delta(h) * price
        else:
            h = (now_utc - _ts(o["createdAt"])).total_seconds() / 3600
            queue["qty"] += 1
            queue["sum"] += price
            queue["b"][speed_bucket(h)] += 1
            queue["lost"] += (speed_delta(h) - BEST_DELTA) * price

    # что стало с заказами дня: уехало / ещё на сборке / отменено до отгрузки.
    # Нужно, чтобы строка складывалась в 100 % от заказанного и не возникал
    # вопрос «отгружено 234, а остальные где».
    made = collections.Counter(r["day"] for r in rows)
    ship_ok = collections.Counter(r["day"] for r in rows if r["state"] == "shipped")
    pend = collections.Counter(r["day"] for r in rows if r["state"] == "assembling")
    canc = collections.Counter(r["day"] for r in rows if r["state"] in ("cancel", "defect"))
    speed_days = []
    for d in sorted(set(by_day) | set(made)):
        a = by_day[d]
        speed_days.append(dict(date=d, created=made.get(d, 0), shipped=ship_ok.get(d, 0),
                               pending=pend.get(d, 0), cancel=canc.get(d, 0),
                               qty=a["qty"], sum=round(a["sum"], 2),
                               buckets=a["b"], buckets_sum=[round(x, 2) for x in a["bs"]],
                               delta=round(a["delta_w"] / a["sum"], 3) if a["sum"] else None))
    tot_created = sum(x["created"] for x in speed_days)
    tot_shipped = sum(x["shipped"] for x in speed_days)
    tot_qty = sum(x["qty"] for x in speed_days)
    tot_sum = sum(x["sum"] for x in speed_days)
    tot_b = [sum(x["buckets"][i] for x in speed_days) for i in range(len(SPEED))]
    tot_delta = (sum(by_day[d]["delta_w"] for d in by_day) / tot_sum) if tot_sum else None
    speed = dict(
        days=speed_days, created=tot_created, shipped=tot_shipped,
        qty=tot_qty, sum=round(tot_sum, 2), buckets=tot_b,
        delta=round(tot_delta, 3) if tot_delta is not None else None,
        best_delta=BEST_DELTA,
        gap=round(abs(BEST_DELTA - tot_delta), 3) if tot_delta is not None else None,
        gap_money=round(abs(BEST_DELTA - tot_delta) / 100 * tot_sum, 2) if tot_delta is not None else None,
        queue_qty=queue["qty"], queue_buckets=queue["b"], queue_sum=round(queue["sum"], 2),
        queue_lost=round(queue["lost"] / queue["sum"], 3) if queue["sum"] else 0,
        queue_lost_money=round(queue["lost"] / 100, 2),
        basis=SPEED_BASIS,
        fallback_qty=fb_qty,
        fallback_share=round(fb_qty / tot_qty, 3) if tot_qty else 0,
    )

    # заказ → выкуп, по сопоставлению srid со статистикой продаж
    o_stat = load(f"orders_{cab}", []) or []
    s_stat = load(f"sales_{cab}", []) or []
    fbs_ord = {r["srid"]: r for r in o_stat if r.get("warehouseType") == FBS}
    okrug_ord = collections.Counter(r.get("oblastOkrugName") or "не указан"
                                    for r in fbs_ord.values())
    okrug_lag = collections.defaultdict(list)
    lags = []
    for r in s_stat:
        if not str(r.get("saleID", "")).startswith("S"):
            continue
        src = fbs_ord.get(r["srid"])
        if not src:
            continue
        dd = (_ts(r["date"] + "+03:00") - _ts(src["date"] + "+03:00")).total_seconds() / 86400
        if 0 < dd < 40:
            lags.append(dd)
            okrug_lag[src.get("oblastOkrugName") or "не указан"].append(round(dd, 2))
    lags.sort()
    to_buyout = dict(qty=len(lags),
                     median=round(lags[len(lags) // 2], 1) if lags else None,
                     p25=round(lags[int(len(lags) * .25)], 1) if lags else None,
                     p75=round(lags[int(len(lags) * .75)], 1) if lags else None) if lags else None

    okrugs = [dict(name=k.replace(" федеральный округ", ""), orders=v,
                   lags=sorted(okrug_lag.get(k) or []))
              for k, v in okrug_ord.most_common()]

    timing = dict(funnel=funnel, pvz_curve=pvz_curve, to_pvz=to_pvz, okrugs=okrugs,
                  to_buyout=to_buyout,
                  return_lag=(RATES.get(cab) or {}).get("return_lag"))

    return dict(now=now, periods=per, days=series, speed=speed,
                timing=timing, total_tasks=len(rows))


ECON_KEYS = ["revenue", "payout", "commission", "logistics", "handling",
             "penalty", "ads", "cogs", "margin_wb", "gross"]


def cabinet(cab):
    orders = load(f"orders_{cab}", []) or []
    sales = load(f"sales_{cab}", []) or []
    upd = load(f"adv_{cab}", []) or []

    fo = [r for r in orders if r.get("warehouseType") == FBS]          # сырые заказы FBS
    isS = lambda r: str(r.get("saleID", "")).startswith("S")
    isR = lambda r: str(r.get("saleID", "")).startswith("R")
    sold_srid = {r["srid"] for r in sales if isS(r)}
    ret_srid = {r["srid"] for r in sales if isR(r)}

    st = RATES.get(cab) or {}

    # ---------------------------------------------- созревание когорт по дням
    cutoff = (TODAY - datetime.timedelta(days=MATURITY)).isoformat()
    cohort = collections.defaultdict(lambda: dict(raw=0, raw_sum=0.0, bought=0,
                                                  bought_sum=0.0, cancel=0, ret=0,
                                                  customer=0.0, cost_known=0.0, cost_qty=0))
    for r in fo:
        d = r["date"][:10]
        c = cohort[d]
        c["raw"] += 1
        c["raw_sum"] += r.get("priceWithDisc") or 0
        c["customer"] += r.get("finishedPrice") or 0
        uc = unit_cost(r)
        if uc is not None:
            c["cost_known"] += uc
            c["cost_qty"] += 1
        if r["srid"] in sold_srid:
            c["bought"] += 1
            c["bought_sum"] += r.get("priceWithDisc") or 0
        elif r.get("isCancel"):
            c["cancel"] += 1
        if r["srid"] in ret_srid:
            c["ret"] += 1
    coh_list = []
    for d in sorted(cohort):
        c = cohort[d]
        age = (TODAY - datetime.date.fromisoformat(d)).days
        coh_list.append(dict(
            date=d, age=age, raw=c["raw"], raw_sum=round(c["raw_sum"], 2),
            customer=round(c["customer"], 2),
            cost_known=round(c["cost_known"], 2), cost_qty=c["cost_qty"],
            bought=c["bought"], bought_sum=round(c["bought_sum"], 2),
            cancel=c["cancel"], ret=c["ret"],
            pending=c["raw"] - c["bought"] - c["cancel"],
            buyout=round(c["bought"] / c["raw"], 4) if c["raw"] else 0,
            cancel_rate=round(c["cancel"] / c["raw"], 4) if c["raw"] else 0,
            mature=age >= MATURITY))

    # выкуп по дозревшим когортам FBS
    mat = [c for c in coh_list if c["mature"]]
    mat_raw = sum(c["raw"] for c in mat)
    mat_buy = sum(c["bought"] for c in mat)

    # --------------------------------------------------------------- ставки
    fin_buyout = st.get("buyout_of_raw")
    fbs_rates_ready = (st.get("source") or "").startswith("FBS")
    proxy_ok = FALLBACK == "cabinet"
    if ovr(cab, "buyout"):
        buyout, bsrc, bproxy = ovr(cab, "buyout"), "задан вручную в config.json", False
    elif mat_raw >= MIN_MATURE:
        buyout = mat_buy / mat_raw
        bsrc = f"дозревшие когорты FBS: выкуплено {mat_buy} из {mat_raw} заказов старше {MATURITY} дн."
        bproxy = False
    elif fin_buyout and proxy_ok:
        buyout = fin_buyout
        w = (st.get("cohort_whole") or {}).get("window", "")
        bsrc = (f"прокси: выкуп кабинета целиком за {w}. Дозревших когорт FBS пока "
                f"{mat_raw} из нужных {MIN_MATURE}")
        bproxy = True
    else:
        buyout, bsrc, bproxy = 0.0, "данных мало — ждём дозревания когорт FBS", True

    # Комиссия у FBS и FBW разная (у FBS выше), поэтому берём ставку по FBS-продажам.
    # Если в этом кабинете FBS-продаж ещё мало — одалживаем ставку у соседнего
    # кабинета того же бренда: это ближе, чем собственный FBW.
    payout, payout_src = ovr(cab, "payout_share"), "задана вручную в config.json"
    # комиссия из отчёта должна приезжать из того же источника, что и ставка,
    # иначе у кабинета с одолженной ставкой рядом стоит чужая комиссия
    comm_pct = None
    if payout is None:
        own = st.get("payout_fbs")
        if own and own.get("qty", 0) >= 30:
            payout = own["payout_share"]
            comm_pct = own.get("base_commission_pct")
            payout_src = f"FBS этого кабинета, {own['qty']} шт продаж"
        else:
            for other, otitle in CABS:
                if other == cab:
                    continue
                b = (RATES.get(other) or {}).get("payout_fbs")
                if b and b.get("qty", 0) >= 30:
                    payout = b["payout_share"]
                    comm_pct = b.get("base_commission_pct")
                    payout_src = (f"FBS кабинета «{otitle}», {b['qty']} шт — "
                                  f"в этом кабинете FBS-продаж пока мало")
                    break
    if payout is None and (fbs_rates_ready or proxy_ok):
        payout = st.get("payout_share")
        comm_pct = (st.get("payout_whole") or {}).get("base_commission_pct")
        payout_src = "кабинет целиком, включая FBW — комиссия там ниже, маржа завышена"
    logi_ord = ovr(cab, "logistics_per_order")
    if logi_ord is None and (fbs_rates_ready or proxy_ok):
        # собираем из двух плеч, чтобы строки таблицы складывались точно
        fwd_r, back_r = st.get("logistics_fwd_per_order"), st.get("logistics_back_per_order")
        logi_ord = (fwd_r + back_r) if (fwd_r is not None and back_r is not None) \
            else st.get("logistics_per_order")
    handling = ovr(cab, "handling_per_order")
    if handling is None:
        handling = (st.get("handling_per_order") or 0.0) if (fbs_rates_ready or proxy_ok) else 0.0
    penalty_ord = ovr(cab, "penalty_per_order")
    if penalty_ord is None:
        penalty_ord = (st.get("penalty_per_order") or 0.0) if (fbs_rates_ready or proxy_ok) else 0.0
    rates_proxy = bool(bproxy or (not fbs_rates_ready and payout is not None))

    # ------------------------------------------------ реклама и доля FBS
    # Одна карточка (nmId) продаётся и по FBS, и по FBW, кампания на неё общая,
    # поэтому «чисто FBS-расход» из API не достаётся. Разводим по доле FBS
    # в заказах того же nmId: если есть детализация fullstats — по каждому nmId,
    # иначе — одной общей долей по кабинету.
    adv_day = collections.defaultdict(float)
    for u in upd:
        adv_day[str(u.get("updTime", ""))[:10]] += float(u.get("updSum") or 0)

    all_day, fbs_day = collections.defaultdict(float), collections.defaultdict(float)
    all_qty, fbs_qty = collections.defaultdict(int), collections.defaultdict(int)
    nm_day = collections.defaultdict(lambda: collections.defaultdict(float))   # nm → день → ₽ всего
    nmf_day = collections.defaultdict(lambda: collections.defaultdict(float))  # nm → день → ₽ FBS
    for r in orders:
        d = r["date"][:10]
        v = r.get("priceWithDisc") or 0
        all_day[d] += v
        all_qty[d] += 1
        nm_day[r["nmId"]][d] += v
        if r.get("warehouseType") == FBS:
            fbs_day[d] += v
            fbs_qty[d] += 1
            nmf_day[r["nmId"]][d] += v

    # доля FBS в заказах кабинета по дням — единственное место, где рядом с FBS
    # показывается FBW, и только как знаменатель
    share_days = [dict(date=d, all_qty=all_qty[d], fbs_qty=fbs_qty.get(d, 0),
                       all_sum=round(all_day[d], 2), fbs_sum=round(fbs_day.get(d, 0), 2),
                       share_qty=round(fbs_qty.get(d, 0) / all_qty[d], 4) if all_qty[d] else 0,
                       share_sum=round(fbs_day.get(d, 0) / all_day[d], 4) if all_day[d] else 0)
                  for d in sorted(all_day)]

    # расход рекламы по nmId и дням из /adv/v3/fullstats (если собран)
    advnm = load(f"advnm_{cab}", None)
    adv_nm_day = collections.defaultdict(lambda: collections.defaultdict(float))
    if advnm:
        for row in advnm:
            adv_nm_day[int(row["nmId"])][row["date"]] += float(row.get("sum") or 0)

    def ads_for(dd):
        """Расход рекламы, приходящийся на FBS, за список дней."""
        total_spend = sum(adv_day.get(x, 0) for x in dd)
        if ADS_MODE == "none":
            return 0.0, 0.0, total_spend, "выключено в config.json"
        if adv_nm_day:
            alloc = 0.0
            covered = 0.0
            for nm, byd in adv_nm_day.items():
                for x in dd:
                    sp = byd.get(x, 0)
                    if not sp:
                        continue
                    covered += sp
                    base = nm_day[nm].get(x, 0)
                    if base:
                        alloc += sp * (nmf_day[nm].get(x, 0) / base)
            if covered > 0:
                # масштабируем на полный расход из /adv/v1/upd (в нём есть НДС и прочее)
                k = total_spend / covered if covered else 1
                return alloc * k, (alloc / covered if covered else 0), total_spend, \
                    "по каждому nmId: доля FBS в заказах этой же карточки"
        base = sum(all_day.get(x, 0) for x in dd)
        fbsv = sum(fbs_day.get(x, 0) for x in dd)
        share = fbsv / base if base else 0
        return total_spend * share, share, total_spend, \
            "по доле FBS в заказах кабинета (детализации по nmId нет)"

    # ------------------------------------------------------ почасовой профиль
    hours = collections.defaultdict(lambda: dict(qty=0, sum=0.0))
    for r in fo:
        if r["date"][:10] == TODAY.isoformat():
            h = int(r["date"][11:13])
            hours[h]["qty"] += 1
            hours[h]["sum"] += r.get("priceWithDisc") or 0

    # ---------------------------------------------------------------- периоды
    per = {}
    for k, label, s, e, partial in PERIODS:
        lo, hi = s.isoformat(), e.isoformat()
        rows = [r for r in fo if lo <= r["date"][:10] <= hi]
        n = len(rows)
        a = dict(
            ord_qty=n,
            ord_sum=sum(r.get("priceWithDisc") or 0 for r in rows),
            ord_customer=sum(r.get("finishedPrice") or 0 for r in rows),
            cancel_qty=sum(1 for r in rows if r.get("isCancel")),
            bought_qty=sum(1 for r in rows if r["srid"] in sold_srid),
            bought_sum=sum(r.get("priceWithDisc") or 0 for r in rows if r["srid"] in sold_srid),
            ret_qty=sum(1 for r in rows if r["srid"] in ret_srid),
            sku_qty=len({r["nmId"] for r in rows}),
        )
        a["pending_qty"] = n - a["bought_qty"] - a["cancel_qty"]
        a["avg_price"] = a["ord_sum"] / n if n else 0
        a["avg_customer"] = a["ord_customer"] / n if n else 0
        a["spp_fact"] = 1 - a["ord_customer"] / a["ord_sum"] if a["ord_sum"] else 0
        a["buyout_now"] = a["bought_qty"] / n if n else 0
        a["cancel_now"] = a["cancel_qty"] / n if n else 0

        dd = [(s + datetime.timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]
        ads_val, share, adv_total, ads_note = ads_for(dd)
        a["adv_cabinet"] = adv_total
        a["fbs_share"] = share
        a["ads"] = ads_val
        a["ads_note"] = ads_note
        a["all_ord_qty"] = sum(all_qty.get(x, 0) for x in dd)
        a["all_ord_sum"] = sum(all_day.get(x, 0) for x in dd)
        a["fbs_share_qty"] = n / a["all_ord_qty"] if a["all_ord_qty"] else 0
        a["fbs_share_sum"] = a["ord_sum"] / a["all_ord_sum"] if a["all_ord_sum"] else 0

        # ---------- прогнозная юнит-экономика на сырые заказы периода ----------
        a["buyout"] = buyout
        exp_qty = n * buyout
        a["exp_buyout_qty"] = exp_qty
        a["revenue"] = a["ord_sum"] * buyout
        a["payout"] = a["revenue"] * payout if payout else None
        a["commission"] = (a["revenue"] - a["payout"]) if payout else None
        a["logistics"] = n * logi_ord if logi_ord is not None else None
        fwd = st.get("logistics_fwd_per_order")
        back = st.get("logistics_back_per_order")
        a["logistics_fwd"] = n * fwd if fwd is not None else None
        a["logistics_back"] = n * back if back is not None else None
        a["handling"] = n * handling
        a["penalty"] = n * penalty_ord

        known, cov = 0.0, 0
        for r in rows:
            c = unit_cost(r)
            if c is not None:
                known += c
                cov += 1
        a["cost_cover"] = cov / n if n else 0
        a["cost_unknown_qty"] = n - cov
        avg_cost = known / cov if cov else None
        a["avg_cost"] = avg_cost
        a["cogs"] = avg_cost * exp_qty if avg_cost else None

        if payout is not None and a["logistics"] is not None:
            a["margin_wb"] = a["payout"] - a["logistics"] - a["handling"] - a["penalty"] - a["ads"]
            a["margin_wb_pct"] = a["margin_wb"] / a["revenue"] if a["revenue"] else 0
            a["margin_wb_of_ordered"] = a["margin_wb"] / a["ord_sum"] if a["ord_sum"] else 0
            a["margin_wb_per_order"] = a["margin_wb"] / n if n else 0
            if a["cogs"] is not None:
                a["gross"] = a["margin_wb"] - a["cogs"]
                a["gross_pct"] = a["gross"] / a["revenue"] if a["revenue"] else 0
                a["gross_of_ordered"] = a["gross"] / a["ord_sum"] if a["ord_sum"] else 0
                a["gross_per_order"] = a["gross"] / n if n else 0
                a["gross_per_buyout"] = a["gross"] / exp_qty if exp_qty else 0
            else:
                a["gross"] = a["gross_pct"] = a["gross_of_ordered"] = None
                a["gross_per_order"] = a["gross_per_buyout"] = None
        else:
            for f in ("margin_wb", "margin_wb_pct", "margin_wb_of_ordered",
                      "margin_wb_per_order", "gross", "gross_pct", "gross_of_ordered",
                      "gross_per_order", "gross_per_buyout"):
                a[f] = None
        per[k] = a

    # ------------------------------------------------------- разрез по SKU 28д
    s28 = (YEST - datetime.timedelta(days=27)).isoformat()
    sku = collections.defaultdict(lambda: dict(raw=0, raw_sum=0.0, bought=0, cancel=0,
                                               art="", subject="", nmId=0))
    for r in fo:
        if r["date"][:10] < s28:
            continue
        x = sku[r["nmId"]]
        x["nmId"], x["art"], x["subject"] = r["nmId"], r.get("supplierArticle", ""), r.get("subject", "")
        x["raw"] += 1
        x["raw_sum"] += r.get("priceWithDisc") or 0
        if r["srid"] in sold_srid:
            x["bought"] += 1
        elif r.get("isCancel"):
            x["cancel"] += 1
    sku_list = []
    for nm, x in sku.items():
        x["avg_price"] = round(x["raw_sum"] / x["raw"], 2) if x["raw"] else 0
        x["cost"] = cost_by_nm.get(nm) or cost_by_art.get(str(x["art"]).strip())
        x["buyout_now"] = round(x["bought"] / x["raw"], 4) if x["raw"] else 0
        if payout and logi_ord is not None:
            rev = x["raw_sum"] * buyout
            m = rev * payout - x["raw"] * (logi_ord + handling + penalty_ord)
            x["revenue"] = round(rev, 2)
            x["margin_wb"] = round(m, 2)
            x["margin_wb_pct"] = round(m / rev, 4) if rev else 0
            if x["cost"]:
                g = m - x["cost"] * x["raw"] * buyout
                x["gross"] = round(g, 2)
                x["gross_pct"] = round(g / rev, 4) if rev else 0
                x["gross_per_order"] = round(g / x["raw"], 2) if x["raw"] else 0
            else:
                x["gross"] = x["gross_pct"] = x["gross_per_order"] = None
        else:
            x["revenue"] = None
            x["margin_wb"] = x["margin_wb_pct"] = x["gross"] = x["gross_pct"] = x["gross_per_order"] = None
        x["raw_sum"] = round(x["raw_sum"], 2)
        sku_list.append(x)
    sku_list.sort(key=lambda z: -z["raw_sum"])

    return dict(
        daily=dict(adv={k: round(v, 2) for k, v in sorted(adv_day.items())},
                   all_sum={k: round(v, 2) for k, v in sorted(all_day.items())},
                   all_qty=dict(all_qty), fbs_sum={k: round(v, 2) for k, v in sorted(fbs_day.items())},
                   ads_note=ads_for([TODAY.isoformat()])[3]),
        periods=per, cohorts=coh_list, hours={str(h): v for h, v in sorted(hours.items())},
        sku=sku_list,
        rates=dict(
            buyout=buyout, buyout_source=bsrc,
            mature_raw=mat_raw, mature_bought=mat_buy, maturity_days=MATURITY,
            payout_share=payout, logistics_per_order=logi_ord,
            handling_per_order=handling, penalty_per_order=penalty_ord,
            payout_source=payout_src,
            base_commission=comm_pct,
            logistics_fwd_per_order=st.get("logistics_fwd_per_order"),
            logistics_back_per_order=st.get("logistics_back_per_order"),
            payout_fbs=st.get("payout_fbs"), payout_whole=st.get("payout_whole"),
            spp_share=st.get("spp_share"), source=st.get("source"),
            covered_to=st.get("covered_to"), updated_at=st.get("updated_at"),
            cohort_whole=st.get("cohort_whole"), cohort_fbs=st.get("cohort_fbs"),
            cohort_fbs_preview=st.get("cohort_fbs_preview"),
            overrides={k: v for k, v in (OVR.get(cab) or {}).items() if v},
        ),
        share_days=share_days,
        first_order=min((r["date"][:10] for r in fo), default=None),
        warehouses=sorted({r.get("warehouseName", "") for r in fo}),
        adv_day={k: round(v, 2) for k, v in sorted(adv_day.items())},
    )


# --------------------------------------------------------------------- сборка
ASM = {}
res = {}
for cab, title in CABS:
    if os.path.exists(os.path.join(DATA, f"orders_{cab}.json.gz")):
        res[cab] = cabinet(cab)
        a = assembly(cab)
        if a:
            ASM[cab] = a
if not res:
    sys.exit("нет выгрузок в data/ — сначала запустите collect.py")

SUMS = ["ord_qty", "ord_sum", "ord_customer", "cancel_qty", "bought_qty", "bought_sum",
        "ret_qty", "pending_qty", "adv_cabinet", "exp_buyout_qty", "cost_unknown_qty",
        "all_ord_qty", "all_ord_sum", "logistics_fwd", "logistics_back"] + ECON_KEYS

total = {}
for k, *_ in PERIODS:
    parts = [res[c]["periods"][k] for c in res]
    a = {}
    for f in SUMS:
        vals = [p.get(f) for p in parts]
        a[f] = None if any(v is None for v in vals) else sum(vals)
    n = a["ord_qty"]
    a["cancel_now"] = a["cancel_qty"] / n if n else 0
    a["buyout_now"] = a["bought_qty"] / n if n else 0
    a["avg_price"] = a["ord_sum"] / n if n else 0
    a["avg_customer"] = a["ord_customer"] / n if n else 0
    a["spp_fact"] = 1 - a["ord_customer"] / a["ord_sum"] if a["ord_sum"] else 0
    a["buyout"] = a["exp_buyout_qty"] / n if n else 0          # взвешенный по штукам
    a["buyout_rub"] = a["revenue"] / a["ord_sum"] if a["ord_sum"] else 0   # по деньгам
    a["sku_qty"] = sum(p["sku_qty"] for p in parts)
    a["cost_cover"] = 1 - a["cost_unknown_qty"] / n if n else 0
    a["avg_cost"] = a["cogs"] / a["exp_buyout_qty"] if a.get("cogs") and a["exp_buyout_qty"] else None
    a["fbs_share"] = None
    a["fbs_share_qty"] = n / a["all_ord_qty"] if a.get("all_ord_qty") else 0
    a["fbs_share_sum"] = a["ord_sum"] / a["all_ord_sum"] if a.get("all_ord_sum") else 0
    a["margin_wb_pct"] = a["margin_wb"] / a["revenue"] if a["margin_wb"] is not None and a["revenue"] else None
    a["margin_wb_of_ordered"] = a["margin_wb"] / a["ord_sum"] if a["margin_wb"] is not None and a["ord_sum"] else None
    a["margin_wb_per_order"] = a["margin_wb"] / n if a["margin_wb"] is not None and n else None
    a["gross_pct"] = a["gross"] / a["revenue"] if a["gross"] is not None and a["revenue"] else None
    a["gross_of_ordered"] = a["gross"] / a["ord_sum"] if a["gross"] is not None and a["ord_sum"] else None
    a["gross_per_order"] = a["gross"] / n if a["gross"] is not None and n else None
    a["gross_per_buyout"] = a["gross"] / a["exp_buyout_qty"] if a["gross"] is not None and a["exp_buyout_qty"] else None
    total[k] = a

# сводные когорты
coh_all = collections.defaultdict(lambda: collections.defaultdict(float))
for c in res:
    for row in res[c]["cohorts"]:
        t = coh_all[row["date"]]
        for f in ("raw", "raw_sum", "bought", "bought_sum", "cancel", "ret", "pending"):
            t[f] += row[f]
coh_total = []
for d in sorted(coh_all):
    t = coh_all[d]
    age = (TODAY - datetime.date.fromisoformat(d)).days
    coh_total.append(dict(date=d, age=age, mature=age >= MATURITY,
                          **{f: round(t[f], 2) for f in t},
                          buyout=round(t["bought"] / t["raw"], 4) if t["raw"] else 0,
                          cancel_rate=round(t["cancel"] / t["raw"], 4) if t["raw"] else 0))

share_all = collections.defaultdict(lambda: collections.defaultdict(float))
for c in res:
    for row in res[c]["share_days"]:
        t = share_all[row["date"]]
        for f in ("all_qty", "fbs_qty", "all_sum", "fbs_sum"):
            t[f] += row[f]
share_total = [dict(date=d, **{f: round(t[f], 2) for f in t},
                    share_qty=round(t["fbs_qty"] / t["all_qty"], 4) if t["all_qty"] else 0,
                    share_sum=round(t["fbs_sum"] / t["all_sum"], 4) if t["all_sum"] else 0)
               for d, t in sorted(share_all.items())]

# сводная сборка по всем кабинетам
asm_total = None
if ASM:
    asm_total = dict(now={}, periods={}, days=[], total_tasks=sum(a["total_tasks"] for a in ASM.values()))
    for f in ("total", "total_sum", "fresh", "in_supply"):
        asm_total["now"][f] = sum(a["now"][f] for a in ASM.values())
    asm_total["now"]["oldest"] = min((a["now"]["oldest"] for a in ASM.values()
                                      if a["now"]["oldest"]), default=None)
    for key, label, back, back_hi in ASM_PERIODS:
        agg = dict(label=label, days=back - back_hi + 1)
        first = next(iter(ASM.values()))["periods"][key]
        agg["start"], agg["end"] = first["start"], first["end"]
        for f in ("created", "created_sum", "shipped", "cancel", "defect",
                  "assembling", "shipped_in_days", "shipped_in_days_sum"):
            agg[f] = sum(a["periods"][key][f] for a in ASM.values())
        asm_total["periods"][key] = agg
    # скорость отгрузки — суммарно
    nb = len(SPEED)
    sd = collections.defaultdict(lambda: dict(qty=0, sum=0.0, b=[0] * nb, dw=0.0,
                                              created=0, shipped=0, pending=0, cancel=0))
    q = dict(qty=0, sum=0.0, b=[0] * nb, lost=0.0)
    for a in ASM.values():
        sp = a.get("speed") or {}
        for row in sp.get("days") or []:
            t = sd[row["date"]]
            t["qty"] += row["qty"]
            t["sum"] += row["sum"]
            t["dw"] += (row["delta"] or 0) * row["sum"]
            t["created"] += row.get("created", 0)
            t["shipped"] += row.get("shipped", 0)
            t["pending"] += row.get("pending", 0)
            t["cancel"] += row.get("cancel", 0)
            for i in range(nb):
                t["b"][i] += row["buckets"][i]
        q["qty"] += sp.get("queue_qty", 0)
        q["sum"] += sp.get("queue_sum", 0)
        q["lost"] += sp.get("queue_lost_money", 0) * 100
        for i in range(nb):
            q["b"][i] += (sp.get("queue_buckets") or [0] * nb)[i]
    days_sp = [dict(date=d, created=t["created"], shipped=t["shipped"],
                    pending=t["pending"], cancel=t["cancel"],
                    qty=t["qty"], sum=round(t["sum"], 2), buckets=t["b"],
                    delta=round(t["dw"] / t["sum"], 3) if t["sum"] else None)
               for d, t in sorted(sd.items())]
    tq = sum(t["qty"] for t in sd.values())
    ts_ = sum(t["sum"] for t in sd.values())
    tdw = sum(t["dw"] for t in sd.values())
    td = (tdw / ts_) if ts_ else None
    asm_total["speed"] = dict(
        days=days_sp, created=sum(t["created"] for t in sd.values()),
        shipped=sum(t["shipped"] for t in sd.values()), qty=tq, sum=round(ts_, 2),
        buckets=[sum(t["b"][i] for t in sd.values()) for i in range(nb)],
        delta=round(td, 3) if td is not None else None, best_delta=BEST_DELTA,
        gap=round(abs(BEST_DELTA - td), 3) if td is not None else None,
        gap_money=round(abs(BEST_DELTA - td) / 100 * ts_, 2) if td is not None else None,
        queue_qty=q["qty"], queue_buckets=q["b"], queue_sum=round(q["sum"], 2),
        queue_lost=round(q["lost"] / q["sum"], 3) if q["sum"] else 0,
        queue_lost_money=round(q["lost"] / 100, 2),
        basis=SPEED_BASIS,
        fallback_qty=sum((a.get("speed") or {}).get("fallback_qty", 0) for a in ASM.values()),
        fallback_share=round(sum((a.get("speed") or {}).get("fallback_qty", 0) for a in ASM.values())
                             / tq, 3) if tq else 0)

    # сроки — суммарно по кабинетам
    fl = collections.defaultdict(lambda: dict(qty=0, ages=[]))
    ca = collections.defaultdict(lambda: [0, 0])
    lag_all, ret_all = [], []
    for a in ASM.values():
        t = a.get("timing") or {}
        for f_ in t.get("funnel") or []:
            fl[f_["key"]]["qty"] += f_["qty"]
            fl[f_["key"]]["ages"].append((f_["median_age"], f_["qty"]))
        for c in t.get("pvz_curve") or []:
            ca[c["age"]][0] += c["qty"]
            ca[c["age"]][1] += c["share"] * c["qty"]
        tb = t.get("to_buyout")
        if tb and tb.get("median") is not None:
            lag_all.append((tb["median"], tb["qty"]))
        rl = t.get("return_lag")
        if rl and rl.get("median") is not None:
            ret_all.append((rl["median"], rl["qty"]))
    LBL = {k: lb for k, lb in [("waiting", "В обработке и сборке"), ("sorted", "В пути"),
                               ("ready_for_pickup", "Ждут на ПВЗ"), ("sold", "Получены покупателем"),
                               ("canceled_by_client", "Отменены покупателем"),
                               ("declined_by_client", "Отказ до сборки"), ("defect", "Брак")]}
    wavg = lambda pairs: (round(sum(v * w for v, w in pairs) / sum(w for v, w in pairs), 1)
                          if pairs and sum(w for v, w in pairs) else None)
    curve = [dict(age=a, qty=ca[a][0], share=round(ca[a][1] / ca[a][0], 3))
             for a in sorted(ca) if ca[a][0]]
    ok_all = collections.defaultdict(lambda: dict(orders=0, lags=[]))
    for a in ASM.values():
        for x in (a.get("timing") or {}).get("okrugs") or []:
            ok_all[x["name"]]["orders"] += x["orders"]
            ok_all[x["name"]]["lags"] += x["lags"]
    okrugs_total = [dict(name=k, orders=v["orders"], lags=sorted(v["lags"]))
                    for k, v in sorted(ok_all.items(), key=lambda z: -z[1]["orders"])]

    asm_total["timing"] = dict(
        okrugs=okrugs_total,
        funnel=[dict(key=k, label=LBL.get(k, k), qty=v["qty"], median_age=wavg(v["ages"]))
                for k, v in fl.items() if v["qty"]],
        pvz_curve=curve,
        to_pvz=next((c["age"] for c in curve if c["share"] >= 0.5), None),
        to_buyout=dict(qty=sum(w for _, w in lag_all), median=wavg(lag_all)) if lag_all else None,
        return_lag=dict(qty=sum(w for _, w in ret_all), median=wavg(ret_all)) if ret_all else None)

    dd = collections.defaultdict(lambda: collections.defaultdict(float))
    for a in ASM.values():
        for row in a["days"]:
            for f in ("shipped", "shipped_sum", "created", "created_sum"):
                dd[row["date"]][f] += row[f]
    asm_total["days"] = [dict(date=d, **{f: round(v, 2) for f, v in x.items()})
                         for d, x in sorted(dd.items())]

out = dict(
    generated_at=NOW.isoformat(timespec="seconds"),
    generated_human=NOW.strftime("%d.%m.%Y %H:%M"),
    brand=BRAND, model="FBS · склад продавца",
    maturity_days=MATURITY, ads_mode=ADS_MODE, window_days=WINDOW,
    period_meta=PERIOD_META,
    cabinets=[dict(key=c, title=t) for c, t in CABS if c in res],
    data={c: res[c]["periods"] for c in res},
    total=total,
    cohorts={c: res[c]["cohorts"] for c in res},
    cohorts_total=coh_total,
    assembly={c: ASM[c] for c in ASM},
    assembly_total=asm_total,
    assembly_periods=[dict(key=k, label=l, days=b - h + 1) for k, l, b, h in ASM_PERIODS],
    speed_buckets=SPEED,
    share_days={c: res[c]["share_days"] for c in res},
    share_total=share_total,
    hours={c: res[c]["hours"] for c in res},
    sku={c: res[c]["sku"] for c in res},
    rates={c: res[c]["rates"] for c in res},
    daily={c: res[c]["daily"] for c in res},
    adv_day={c: res[c]["adv_day"] for c in res},
    meta={c: dict(first_order=res[c]["first_order"], warehouses=res[c]["warehouses"]) for c in res},
    has_cost=bool(cost_by_nm or cost_by_art),
    cost_filled=len(cost_by_nm) or len(cost_by_art),
)

with open(os.path.join(BASE, "dashboard_data.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=str)

for c, t in CABS:
    if c not in res:
        continue
    p, r = res[c]["periods"]["today"], res[c]["rates"]
    print(f"{t}: сегодня {p['ord_qty']} зак. / {p['ord_sum']:,.0f} ₽ | "
          f"выкуп {r['buyout']:.1%} [{r['buyout_source']}] | "
          f"доходит {r['payout_share']} | логистика/зак {r['logistics_per_order']}")
def money(v, pct=None):
    if v is None:
        return "н/д"
    s = f"{round(v):,} ₽".replace(",", " ")
    return s + (f" ({pct:.1%})" if pct is not None else "")


for k, lbl in (("today", "СЕГОДНЯ"), ("yday", "ВЧЕРА"), ("d28", "28 ДНЕЙ")):
    t = total[k]
    print(f"ИТОГО {lbl}: {t['ord_qty']} зак. / {money(t['ord_sum'])} | "
          f"после вычетов WB {money(t['margin_wb'], t['margin_wb_pct'])} | "
          f"валовая {money(t['gross'], t['gross_pct'])}")
