#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выгрузка данных WB по двум кабинетам одного бренда.

Два режима:
  * ежечасный  — заказы, продажи, реклама  (быстро, 6 запросов)
  * суточный   — плюс финотчёт, из него пересчитываются ставки юнит-экономики
                 (медленно, WB жёстко лимитирует финотчёт)

Ставки складываются в state/rates.json — он коммитится в репозиторий и живёт
между запусками, поэтому ежечасный прогон финотчёт не трогает.

ENV:
  WB_TOKEN_<КЛЮЧ>               — персональный токен на каждый кабинет из config.json
  WB_FIN_MAX_AGE_H              — через сколько часов обновлять финотчёт (24)
  WB_FORCE_FIN=1                — обновить финотчёт принудительно
  WINDOW_DAYS                   — глубина окна заказов (28)
"""
import os, sys, json, gzip, time, datetime, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wb_client import call, log, WBError

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
STATE = os.path.join(BASE, "state")
os.makedirs(DATA, exist_ok=True)
os.makedirs(STATE, exist_ok=True)

MSK = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime.now(MSK)
TODAY = NOW.date()
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "28"))
START = TODAY - datetime.timedelta(days=WINDOW_DAYS - 1)
FIN_WEEKS_BACK = int(os.environ.get("FIN_WEEKS_BACK", "6"))



def load_cabinets():
    """Кабинеты описываются в config.json — код ни к каким названиям не привязан.

    "cabinets": [{"key": "main", "title": "Основной", "env": "WB_TOKEN_MAIN"}, ...]
    Кабинетов может быть один, два или сколько угодно.
    """
    cfg = {}
    p = os.path.join(BASE, "config.json")
    if os.path.exists(p):
        try:
            cfg = json.load(open(p, encoding="utf-8"))
        except Exception:
            cfg = {}
    out = []
    if not cfg.get("cabinets"):
        # запасной путь: конфиг старый или без блока cabinets — находим кабинеты
        # по переменным окружения WB_TOKEN_*, чтобы сборка не падала на пустом месте
        found = sorted(k for k, v in os.environ.items()
                       if k.startswith("WB_TOKEN_") and v.strip())
        for env in found:
            key = env[len("WB_TOKEN_"):].lower()
            out.append((key, key.upper(), os.environ[env].strip()))
        if out:
            log("  в config.json нет блока cabinets — беру кабинеты из переменных: "
                + ", ".join(k for k, _, _ in out))
        return out
    for c in cfg.get("cabinets") or []:
        key = str(c.get("key") or "").strip()
        if not key:
            continue
        env = c.get("env") or ("WB_TOKEN_" + key.upper())
        tok = os.environ.get(env, "").strip()
        if tok:
            out.append((key, c.get("title") or key.upper(), tok))
        else:
            log(f"  кабинет «{c.get('title') or key}»: нет токена в {env} — пропускаю")
    return out


CABS = load_cabinets()

H_STAT = "statistics-api.wildberries.ru"
H_ADV = "advert-api.wildberries.ru"
H_FIN = "finance-api.wildberries.ru"
H_MP = "marketplace-api.wildberries.ru"
H_ANL = "seller-analytics-api.wildberries.ru"
FBS = "Склад продавца"


def save(name, obj):
    with gzip.open(os.path.join(DATA, name + ".json.gz"), "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def load(name, default=None):
    p = os.path.join(DATA, name + ".json.gz")
    if not os.path.exists(p):
        return default
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------- статистика
def pull_stat(token, path, name, date_from):
    """flag=0 отдаёт всё, что менялось с date_from. Заказ всегда меняется не
    раньше даты создания, поэтому окно по дате заказа покрывается полностью."""
    rows = call(token, H_STAT, path, query={"dateFrom": date_from.isoformat(), "flag": 0}) or []
    fbs = sum(1 for r in rows if r.get("warehouseType") == FBS)
    log(f"    {name}: {len(rows)} строк, FBS {fbs}")
    return rows


# ------------------------------------------------------------------ реклама
def pull_adv(token):
    try:
        upd = call(token, H_ADV, "/adv/v1/upd",
                   query={"from": START.isoformat(), "to": TODAY.isoformat()}) or []
    except WBError as e:
        log("    реклама недоступна:", e)
        return []
    log(f"    реклама: {len(upd)} списаний")
    return upd



# ------------------------------------------- возвраты продавцу (в том числе на ПВЗ)
def pull_returns(token, days=None):
    """Отчёт «Возвраты и перемещения товаров»: что едет обратно к продавцу.

    Это единственное место в API, где видно физический путь возврата:
    `readyToReturnDt` — момент, когда товар доехал до ПВЗ и готов к выдаче,
    `completedDt` — когда его забрали. В финотчёте такого события нет вообще,
    там обратная логистика списывается в день отказа.

    Тонкости, проверенные на живых данных:
      * окно запроса не больше 31 дня — иначе 400;
      * `orderDt` — день оформления возврата, то есть день отказа покупателя,
        а НЕ день исходного заказа (сверено с cancelDate: совпадает по дням,
        и приходит раньше, чем WB проставит isCancel);
      * свой srid вида `mp.<hex>.r` — с srid заказа не сшивается, связь только
        через nmId, размер и дату;
      * список товаров нигде не задаётся: какие артикулы подключены к возврату
        на ПВЗ, видно из самого отчёта — включили новый, он появится сам.
    """
    days = days or int(os.environ.get("RETURNS_DAYS", "60"))
    rows, seen = [], set()
    d2 = TODAY
    while d2 > TODAY - datetime.timedelta(days=days):
        d1 = max(d2 - datetime.timedelta(days=31), TODAY - datetime.timedelta(days=days))
        r = call(token, H_ANL, "/api/v1/analytics/goods-return",
                 query={"dateFrom": d1.isoformat(), "dateTo": d2.isoformat()}) or {}
        for x in (r.get("report") or []):
            k = (x.get("srid"), x.get("shkId"), x.get("nmId"))
            if k in seen:
                continue
            seen.add(k)
            rows.append(x)
        if d1 <= TODAY - datetime.timedelta(days=days):
            break
        d2 = d1
        time.sleep(3)
    kinds = collections.Counter(x.get("returnType") for x in rows)
    log(f"    возвраты: {len(rows)} строк; " +
        ", ".join(f"{k} — {v}" for k, v in kinds.most_common(3)))
    return rows


# ------------------------------------------------------- сборка и отгрузка
def pull_marketplace(token, days):
    """Операционная картина FBS: сборочные задания, их статусы и поставки.

    Это другой раздел API, не статистика. Здесь видно, что происходит с заказом
    физически: висит ли он на сборке, уехал ли в поставке, отменён ли.
    Дата отгрузки берётся из поставки — по closedAt, когда поставка закрыта.
    """
    frm = int(time.mktime((TODAY - datetime.timedelta(days=days)).timetuple()))
    orders, nxt = [], 0
    for _ in range(30):
        r = call(token, H_MP, "/api/v3/orders",
                 query={"limit": 1000, "next": nxt, "dateFrom": frm}) or {}
        batch = r.get("orders") or []
        orders += batch
        nxt = r.get("next") or 0
        if len(batch) < 1000 or not nxt:
            break
        time.sleep(0.4)

    statuses = []
    ids = [o["id"] for o in orders]
    for i in range(0, len(ids), 1000):
        r = call(token, H_MP, "/api/v3/orders/status", method="POST",
                 body={"orders": ids[i:i + 1000]}) or {}
        statuses += r.get("orders") or []
        time.sleep(0.4)

    supplies, nxt = [], 0
    for _ in range(20):
        r = call(token, H_MP, "/api/v3/supplies", query={"limit": 1000, "next": nxt}) or {}
        batch = r.get("supplies") or []
        supplies += batch
        nxt = r.get("next") or 0
        if len(batch) < 1000 or not nxt:
            break
        time.sleep(0.4)

    st = {s["id"]: s for s in statuses}
    slim = []
    for o in orders:
        s = st.get(o["id"]) or {}
        slim.append(dict(id=o["id"], createdAt=o.get("createdAt"), supplyId=o.get("supplyId"),
                         nmId=o.get("nmId"), article=o.get("article"),
                         warehouseId=o.get("warehouseId"),
                         price=(o.get("convertedPrice") or o.get("price") or 0) / 100,
                         supplierStatus=s.get("supplierStatus"), wbStatus=s.get("wbStatus")))
    # склады продавца: их может быть несколько, и отгружают они по-разному
    try:
        whs = call(token, H_MP, "/api/v3/warehouses") or []
    except Exception as e:
        log(f"    список складов недоступен: {e}")
        whs = []
    log(f"    сборка: {len(slim)} заданий, {len(supplies)} поставок, {len(whs)} складов")
    return dict(orders=slim,
                warehouses=[dict(id=w.get("id"), name=w.get("name")) for w in whs],
                supplies=[dict(id=s["id"], done=bool(s.get("done")),
                               createdAt=s.get("createdAt"), closedAt=s.get("closedAt"),
                               # scanDt — момент, когда поставку приняли на стороне WB;
                               # closedAt — когда её закрыл продавец. Разница важна для
                               # коэффициента скорости, поэтому храним оба
                               scanDt=s.get("scanDt"))
                          for s in supplies])


# ----------------------------------------------------------------- финотчёт
FIN_KEEP = ("rrdId", "srid", "nmId", "vendorCode", "subjectName", "sellerOperName",
            "quantity", "retailPrice", "retailPriceWithDisc", "retailAmount",
            "forPay", "acquiringFee", "commissionPercent", "deliveryAmount", "returnAmount",
            "deliveryService", "rebillLogisticCost", "penalty", "deduction",
            "paidStorage", "paidAcceptance", "deliveryMethod",
            "saleDt", "orderDt", "rrDate", "dateFrom", "dateTo")


def pull_finance(token):
    """Недельными окнами: одним куском WB отдаёт сотни мегабайт."""
    out, d0 = [], TODAY - datetime.timedelta(days=7 * FIN_WEEKS_BACK)
    while d0 <= TODAY:
        d1 = min(d0 + datetime.timedelta(days=6), TODAY)
        rrdid, pages = 0, 0
        while pages < 8:
            batch = call(token, H_FIN, "/api/finance/v1/sales-reports/detailed",
                         method="POST",
                         body={"dateFrom": d0.isoformat(), "dateTo": d1.isoformat(),
                               "rrdid": rrdid, "limit": 100000})
            pages += 1
            if not batch:
                break
            out.extend({k: r.get(k) for k in FIN_KEEP} for r in batch)
            rrdid = max(r["rrdId"] for r in batch)
            log(f"    финотчёт {d0}—{d1}: +{len(batch)} (всего {len(out)})")
            if len(batch) < 100000:
                break
            time.sleep(20)
        d0 = d1 + datetime.timedelta(days=1)
        time.sleep(10)
    return out


# ------------------------------------------------------- ставки из финотчёта
def fnum(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def cohort_rates(orders_raw, sold_srids, fin_by_srid, lo, hi, tag):
    """Ставки по когорте СЫРЫХ заказов (со всеми, кто потом отменится и не выкупится).

    Ключевой момент: у WB isCancel=true ставится и на невыкуп, поэтому у свежего
    дня отмен почти нет, а у зрелой когорты их 55–60 %. Считать экономику можно
    только от сырого заказа — иначе свежий день завышен вдвое.
    """
    cohort = [r for r in orders_raw if lo <= r["date"][:10] <= hi]
    n = len(cohort)
    if not n:
        return None
    a = collections.defaultdict(float)
    covered = 0
    for r in cohort:
        f = fin_by_srid.get(r["srid"])
        if not f:
            continue
        covered += 1
        for k, v in f.items():
            a[k] += v
    bought = sum(1 for r in cohort if r["srid"] in sold_srids)
    retail = a["retail"]
    return dict(
        tag=tag, window=f"{lo}—{hi}", orders_raw=n,
        covered=covered, coverage=round(covered / n, 3),
        bought=bought, buyout_of_raw=round(bought / n, 4),
        retail=round(retail, 2), customer=round(a["customer"], 2), forpay=round(a["forpay"], 2),
        payout_share=round(a["forpay"] / retail, 4) if retail else None,
        spp_share=round(1 - a["customer"] / retail, 4) if retail else None,
        logistics=round(a["log"], 2),
        logistics_per_order=round(a["log"] / n, 2),
        logistics_fwd_per_order=round((a["log_fwd"] + a["log_mix"] / 2) / n, 2),
        logistics_back_per_order=round((a["log_back"] + a["log_mix"] / 2) / n, 2),
        logistics_back_share=round((a["log_back"] + a["log_mix"] / 2) / a["log"], 4) if a["log"] else 0,
        events_per_order=round((a["dev"] + a["ret"]) / n, 3),
        deliveries=int(a["dev"]), returns=int(a["ret"]),
        handling_per_order=round(a["acc"] / n, 2),
        penalty_per_order=round(a["pen"] / n, 2),
        storage_per_order=round(a["sto"] / n, 2),
        avg_order_price=round(sum(fnum(r.get("priceWithDisc")) for r in cohort) / n, 2),
    )


def payout_block(fin, srids, tag):
    """Сколько из цены продавца реально доходит до продавца — по продажам,
    без привязки к когорте: это отношение, а не ставка на заказ.

    Считаем отдельно для FBS и FBW, потому что СПП WB компенсирует по-разному:
    на FBW доплачивает сверх того, что заплатил покупатель, на FBS почти нет.
    """
    rows = fin if srids is None else [r for r in fin if r.get("srid") in srids]
    sale = [r for r in rows if r["sellerOperName"] == "Продажа"]
    ret = [r for r in rows if r["sellerOperName"] == "Возврат"]
    if not sale:
        return None
    retail = sum(fnum(r["retailPriceWithDisc"]) for r in sale) \
        - sum(fnum(r["retailPriceWithDisc"]) for r in ret)
    customer = sum(fnum(r["retailAmount"]) for r in sale) \
        - sum(fnum(r["retailAmount"]) for r in ret)
    forpay = sum(fnum(r["forPay"]) for r in sale) - sum(fnum(r["forPay"]) for r in ret)
    qty = sum(fnum(r["quantity"]) for r in sale) - sum(fnum(r["quantity"]) for r in ret)
    base = [fnum(r.get("commissionPercent")) for r in sale if fnum(r.get("commissionPercent"))]
    if not retail:
        return None
    return dict(
        tag=tag, qty=int(qty),
        retail=round(retail, 2), customer=round(customer, 2), forpay=round(forpay, 2),
        payout_share=round(forpay / retail, 4),
        kept_share=round(1 - forpay / retail, 4),
        spp_share=round(1 - customer / retail, 4),
        spp_compensated=round(forpay / customer, 4) if customer else None,
        base_commission_pct=round(sum(base) / len(base), 2) if base else None,
        weeks=sorted({(r.get("dateFrom") or "")[:10] for r in rows if r.get("dateFrom")}),
    )


def index_finance(fin):
    """Финотчёт → срез по srid: сколько денег прошло по каждому заказу."""
    idx = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in fin:
        srid = r.get("srid")
        if not srid:
            continue
        a = idx[srid]
        cost = fnum(r.get("deliveryService")) + fnum(r.get("rebillLogisticCost"))
        dev, ret = fnum(r.get("deliveryAmount")), fnum(r.get("returnAmount"))
        a["log"] += cost
        a["dev"] += dev
        a["ret"] += ret
        # строка логистики относится либо к доставке покупателю, либо к обратному
        # плечу при невыкупе или возврате — раскладываем, чтобы их было видно врозь
        if ret > 0 and dev == 0:
            a["log_back"] += cost
        elif dev > 0 and ret == 0:
            a["log_fwd"] += cost
        else:
            a["log_mix"] += cost
        a["acc"] += fnum(r.get("paidAcceptance"))
        a["pen"] += fnum(r.get("penalty"))
        a["sto"] += fnum(r.get("paidStorage"))
        op = r.get("sellerOperName")
        if op == "Продажа":
            a["retail"] += fnum(r.get("retailPriceWithDisc"))
            a["customer"] += fnum(r.get("retailAmount"))
            a["forpay"] += fnum(r.get("forPay"))
            a["qty"] += fnum(r.get("quantity"))
        elif op == "Возврат":
            # в отчёте WB возвраты записаны положительными числами и вычитаются
            a["retail"] -= fnum(r.get("retailPriceWithDisc"))
            a["customer"] -= fnum(r.get("retailAmount"))
            a["forpay"] -= fnum(r.get("forPay"))
            a["qty"] -= fnum(r.get("quantity"))
    return idx


def rates_from_finance(fin, orders_all, sales_all):
    """Когортные ставки: сперва пробуем чистую FBS-выборку, если её мало —
    берём кабинет целиком (у него та же комиссия и та же логистика ПВЗ)."""
    if not fin:
        return dict(payout_share=None, error="финотчёт пуст",
                    updated_at=NOW.isoformat(timespec="seconds"))
    idx = index_finance(fin)
    covered_to = max((r.get("dateTo") or "")[:10] for r in fin)
    cov_d = datetime.date.fromisoformat(covered_to)
    # окно когорты: заказы, которые к дате закрытия отчёта уже успели доехать
    lo = (cov_d - datetime.timedelta(days=int(os.environ.get("COHORT_LO", "21")))).isoformat()
    hi = (cov_d - datetime.timedelta(days=int(os.environ.get("COHORT_HI", "8")))).isoformat()

    sold = {r["srid"] for r in sales_all if str(r.get("saleID", "")).startswith("S")}
    fbs_orders = [r for r in orders_all if r.get("warehouseType") == FBS]

    whole = cohort_rates(orders_all, sold, idx, lo, hi, "кабинет целиком")
    fbs = cohort_rates(fbs_orders, sold, idx, lo, hi, "только FBS")

    MIN_ORD, MIN_COV = 150, 0.6
    use_fbs = bool(fbs and fbs["orders_raw"] >= MIN_ORD and fbs["coverage"] >= MIN_COV
                   and fbs["payout_share"])
    src = fbs if use_fbs else whole

    # предварительный, ещё не дозревший срез по FBS — просто чтобы видеть тренд
    fbs_preview = None
    if fbs_orders:
        f_lo = min(r["date"][:10] for r in fbs_orders)
        fbs_preview = cohort_rates(fbs_orders, sold, idx, f_lo, hi, "FBS, предварительно")

    # доля к перечислению — только по FBS, если выборка есть: у FBW она другая
    fbs_srids = {r["srid"] for r in fbs_orders} | \
        {r["srid"] for r in sales_all if r.get("warehouseType") == FBS}
    pay_fbs = payout_block(fin, fbs_srids, "FBS")
    pay_fbw = payout_block(fin, None, "кабинет целиком")
    MIN_PAY = int(os.environ.get("MIN_PAYOUT_QTY", "30"))
    use_pay_fbs = bool(pay_fbs and pay_fbs["qty"] >= MIN_PAY)

    # сколько дней проходит от заказа до возврата товара на склад при невыкупе:
    # берём дату строки обратной логистики в финотчёте
    lag = []
    ord_date = {r["srid"]: r["date"][:10] for r in fbs_orders}
    for r in fin:
        if fnum(r.get("returnAmount")) > 0 and r.get("srid") in ord_date and r.get("rrDate"):
            try:
                dd = (datetime.date.fromisoformat(str(r["rrDate"])[:10])
                      - datetime.date.fromisoformat(ord_date[r["srid"]])).days
            except Exception:
                continue
            if 0 < dd < 60:
                lag.append(dd)
    lag.sort()
    return_lag = dict(qty=len(lag),
                      median=lag[len(lag) // 2] if lag else None,
                      p25=lag[int(len(lag) * .25)] if lag else None,
                      p75=lag[int(len(lag) * .75)] if lag else None) if lag else None

    out = dict(
        return_lag=return_lag,
        payout_share=(pay_fbs if use_pay_fbs else src)["payout_share"] if src or pay_fbs else None,
        payout_source=("FBS, %d шт" % pay_fbs["qty"]) if use_pay_fbs
                      else "кабинет целиком (FBS-продаж мало)",
        payout_fbs=pay_fbs, payout_whole=pay_fbw,
        payout_fbs_qty=(pay_fbs or {}).get("qty", 0),
        spp_share=src["spp_share"] if src else None,
        logistics_per_order=src["logistics_per_order"] if src else None,
        logistics_fwd_per_order=src["logistics_fwd_per_order"] if src else None,
        logistics_back_per_order=src["logistics_back_per_order"] if src else None,
        handling_per_order=src["handling_per_order"] if src else None,
        penalty_per_order=src["penalty_per_order"] if src else None,
        buyout_of_raw=src["buyout_of_raw"] if src else None,
        source="FBS" if use_fbs else "кабинет целиком (FBS ещё не дозрел)",
        cohort_whole=whole, cohort_fbs=fbs, cohort_fbs_preview=fbs_preview,
        covered_to=covered_to,
        updated_at=NOW.isoformat(timespec="seconds"),
    )
    return out


# ---------------------------------------------------------------------- main
# суммы и обороты в state/rates.json не храним: файл лежит в публичном
# репозитории, а для расчёта нужны только коэффициенты и размеры выборок
MONEY_KEYS = ("retail", "customer", "forpay", "logistics", "avg_order_price")


def strip_money(obj):
    if isinstance(obj, dict):
        return {k: strip_money(v) for k, v in obj.items() if k not in MONEY_KEYS}
    if isinstance(obj, list):
        return [strip_money(v) for v in obj]
    return obj


def main():
    if not CABS:
        sys.exit("не задан ни один токен: нужна переменная WB_TOKEN_<КЛЮЧ> на каждый кабинет из config.json")
    state_path = os.path.join(STATE, "rates.json")
    state = {}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except Exception:
            state = {}

    max_age = float(os.environ.get("WB_FIN_MAX_AGE_H", "24"))
    force = os.environ.get("WB_FORCE_FIN") == "1"

    def stale(key):
        if force or key not in state:
            return True
        try:
            prev = datetime.datetime.fromisoformat(state[key]["updated_at"])
        except Exception:
            return True
        return (NOW - prev).total_seconds() > max_age * 3600

    log(f"окно заказов: {START} — {TODAY} (МСК {NOW:%H:%M})")

    orders, sales = {}, {}
    for key, title, tok in CABS:
        log(f"  [{title}] заказы")
        orders[key] = pull_stat(tok, "/api/v1/supplier/orders", "заказы", START)
        save(f"orders_{key}", orders[key])

    time.sleep(62)
    for key, title, tok in CABS:
        log(f"  [{title}] продажи")
        sales[key] = pull_stat(tok, "/api/v1/supplier/sales", "продажи", START)
        save(f"sales_{key}", sales[key])

    for key, title, tok in CABS:
        log(f"  [{title}] реклама")
        save(f"adv_{key}", pull_adv(tok))

    asm_days = int(os.environ.get("ASSEMBLY_DAYS", "10"))
    for key, title, tok in CABS:
        log(f"  [{title}] сборка и отгрузка")
        try:
            save(f"mp_{key}", pull_marketplace(tok, asm_days))
        except Exception as e:
            log(f"    раздел сборки недоступен: {e}")

    for key, title, tok in CABS:
        log(f"  [{title}] возвраты продавцу")
        try:
            save(f"ret_{key}", pull_returns(tok))
        except Exception as e:
            log(f"    отчёт по возвратам недоступен: {e}")

    for key, title, tok in CABS:
        if not stale(key):
            log(f"  [{title}] финотчёт свежий ({state[key]['updated_at']}) — пропускаю")
            continue
        log(f"  [{title}] финотчёт (долго)")
        try:
            fin = pull_finance(tok)
            state[key] = rates_from_finance(fin, orders[key], sales[key])
            log(f"    ставки: до продавца доходит {state[key]['payout_share']}, "
                f"логистика/заказ {state[key]['logistics_per_order']}, "
                f"выкуп {state[key]['buyout_of_raw']}, источник — {state[key]['source']}")
        except Exception as e:
            log(f"    финотчёт не собран: {e}")
            if key not in state:
                state[key] = dict(payout_share=None, error=str(e),
                                  updated_at=NOW.isoformat(timespec="seconds"))

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(strip_money(state), f, ensure_ascii=False, indent=1)
    log("готово")


if __name__ == "__main__":
    main()
