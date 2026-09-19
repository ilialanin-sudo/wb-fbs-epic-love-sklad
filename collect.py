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


def _cfg():
    p = os.path.join(BASE, "config.json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


CFG = _cfg()
# Кабинет может торговать десятком брендов, а дашборд нужен по одному.
# brand_filter — список брендов (как они написаны в карточках), по которым
# оставляем данные. Пусто — берём кабинет целиком, как раньше.
_bf = CFG.get("brand_filter")
if isinstance(_bf, str):
    _bf = [_bf]
BRANDS = {str(b).strip().lower() for b in (_bf or []) if str(b).strip()}
# Складской режим: страница нужна сборщикам, деньги в неё не попадают вообще —
# ни в интерфейс, ни в данные. Значит, и собирать их незачем: ни финотчёта,
# ни рекламы, ни продаж. Прогон становится втрое короче.
WAREHOUSE = str(CFG.get("mode", "")).lower() == "warehouse"
H_CONTENT = "content-api.wildberries.ru"
CARDS = {}      # nmId → карточка: артикул, название, предмет, картинка
SIZES = {}      # chrtId и баркод → размер

# start_date — день, раньше которого смотреть нечего (бренд ещё не продавался).
# Обрезает и окно выгрузки, и глубину сборочных заданий с возвратами.
_sd = os.environ.get("START_DATE") or CFG.get("start_date")
START_DATE = datetime.date.fromisoformat(str(_sd)[:10]) if _sd else None
if START_DATE and START_DATE > START:
    START = START_DATE



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
def pull_stat(token, path, name, date_from, key="srid"):
    """flag=0 отдаёт всё, что менялось с date_from. Заказ всегда меняется не
    раньше даты создания, поэтому окно по дате заказа покрывается полностью.

    WB отдаёт не больше 80 000 строк за запрос и молча обрезает остальное.
    В большом кабинете 28 дней в один запрос не влезают, поэтому идём
    страницами: следующий запрос начинается с максимального lastChangeDate.
    Ключ склейки у заказов — srid, у продаж — пара (saleID, srid): у продажи
    и возврата один и тот же srid, и по одному srid возврат затирает продажу.
    """
    rows, cur = {}, date_from.isoformat()
    for page in range(12):
        batch = call(token, H_STAT, path, query={"dateFrom": cur, "flag": 0}) or []
        for r in batch:
            rows[tuple(r.get(k) for k in key.split("+"))] = r
        mx = max((r.get("lastChangeDate") or "" for r in batch), default=cur)
        log(f"    {name}: стр.{page} +{len(batch)}, всего {len(rows)} (по {mx})")
        if len(batch) < 80000 or not mx or mx == cur:
            break
        cur = mx
        time.sleep(65)
    out = list(rows.values())
    fbs = sum(1 for r in out if r.get("warehouseType") == FBS)
    log(f"    {name}: итого {len(out)} строк, FBS {fbs}")
    return out


# ------------------------------------------------------- номенклатуры бренда
def brand_nmids(token):
    """nmId всех карточек нужных брендов. Без этого бренд не отделить:
    в сборочных заданиях, возвратах и финотчёте поля brand нет вообще."""
    if not BRANDS:
        return None, {}
    nms, art, cur = set(), {}, {"limit": 100}
    CARDS.clear()
    SIZES.clear()
    for _ in range(60):
        r = call(token, H_CONTENT, "/content/v2/get/cards/list", method="POST",
                 body={"settings": {"cursor": cur, "filter": {"withPhoto": -1}}}) or {}
        cards = r.get("cards") or []
        for c in cards:
            if str(c.get("brand") or "").strip().lower() in BRANDS:
                nms.add(int(c["nmID"]))
                art[int(c["nmID"])] = c.get("vendorCode")
                ph = (c.get("photos") or [{}])[0]
                CARDS[int(c["nmID"])] = dict(
                    article=c.get("vendorCode"), title=c.get("title"),
                    subject=c.get("subjectName"),
                    photo=ph.get("tm") or ph.get("c246x328") or ph.get("square"))
                # размер заказа приходит номером chrtId и баркодом —
                # без этой таблицы сборщик не поймёт, какую вещь снимать с полки
                for z in (c.get("sizes") or []):
                    if z.get("chrtID"):
                        SIZES[int(z["chrtID"])] = z.get("techSize") or z.get("wbSize") or ""
                    for bc in (z.get("skus") or []):
                        SIZES[str(bc)] = z.get("techSize") or z.get("wbSize") or ""
        c2 = r.get("cursor") or {}
        if len(cards) < 100:
            break
        cur = {"limit": 100, "updatedAt": c2.get("updatedAt"), "nmID": c2.get("nmID")}
        time.sleep(0.3)
    log(f"    карточки бренда: {len(nms)} nmId")
    return nms, art


def by_brand(rows, nms):
    """Строки статистики несут поле brand — по нему и режем, оно точнее списка
    карточек: карточку могли удалить, а заказ по ней в окне ещё есть."""
    if not BRANDS:
        return rows
    out = [r for r in rows
           if str(r.get("brand") or "").strip().lower() in BRANDS
           or (nms and r.get("nmId") in nms)]
    return out


def by_nm(rows, nms):
    if not BRANDS or nms is None:
        return rows
    return [r for r in rows if r.get("nmId") in nms]


# ------------------------------------------------------------------ реклама
def pull_adv(token, nms):
    """Списания по кампаниям и, если задан бренд, только по его кампаниям.

    Кампания привязана к карточкам, а не к бренду, поэтому:
      1) /adv/v1/upd — какие кампании вообще тратили деньги в окне;
      2) /api/advert/v2/adverts — какие nmId в этих кампаниях;
      3) оставляем те, где есть карточки бренда.
    Кампания на одну карточку (а таких почти все) раскладывается точно.
    Смешанные — через /adv/v3/fullstats, там расход разложен по nmId и дням.
    """
    try:
        upd = call(token, H_ADV, "/adv/v1/upd",
                   query={"from": START.isoformat(), "to": TODAY.isoformat()}) or []
    except WBError as e:
        log("    реклама недоступна:", e)
        return [], []
    log(f"    реклама: {len(upd)} списаний по {len({u.get('advertId') for u in upd})} кампаниям")
    if not BRANDS or nms is None:
        return upd, []

    ids = sorted({u.get("advertId") for u in upd if u.get("advertId")})
    camp = {}
    for i in range(0, len(ids), 50):
        try:
            r = call(token, H_ADV, "/api/advert/v2/adverts",
                     query={"ids": ",".join(map(str, ids[i:i + 50]))}) or {}
        except WBError as e:
            log(f"    состав кампаний недоступен: {e}")
            return [], []
        for a in (r.get("adverts") or []):
            camp[a.get("id")] = {x.get("nm_id") for x in (a.get("nm_settings") or [])
                                 if x.get("nm_id")}
        time.sleep(0.4)

    mine = {k: v for k, v in camp.items() if v & nms}
    mixed = [k for k, v in mine.items() if v - nms]
    upd_b = [u for u in upd if u.get("advertId") in mine]
    spend = sum(float(u.get("updSum") or 0) for u in upd_b)
    log(f"    кампании бренда: {len(mine)} из {len(camp)}, расход {spend:,.0f} ₽"
        .replace(",", " ") + (f", смешанных {len(mixed)}" if mixed else ""))

    # расход по nmId и дням: у кампании на одну карточку — прямо из upd,
    # у смешанной — из fullstats
    advnm = []
    for u in upd_b:
        c = mine.get(u.get("advertId")) or set()
        if len(c) == 1:
            advnm.append(dict(nmId=next(iter(c)), date=str(u.get("updTime", ""))[:10],
                              sum=float(u.get("updSum") or 0)))
    if mixed:
        for i in range(0, len(mixed), 50):
            try:
                st = call(token, H_ADV, "/adv/v3/fullstats",
                          query={"ids": ",".join(map(str, mixed[i:i + 50])),
                                 "beginDate": START.isoformat(),
                                 "endDate": (TODAY - datetime.timedelta(days=1)).isoformat()}) or []
            except WBError as e:
                log(f"    fullstats недоступен: {e}")
                break
            for c in st:
                for d in (c.get("days") or []):
                    day = str(d.get("date", ""))[:10]
                    for app in (d.get("apps") or []):
                        for nm in (app.get("nm") or []):
                            if nm.get("nmId") in nms:
                                advnm.append(dict(nmId=nm["nmId"], date=day,
                                                  sum=float(nm.get("sum") or 0)))
            if i + 50 < len(mixed):
                time.sleep(62)
    return upd_b, advnm



# ------------------------------------------- возвраты продавцу (в том числе на ПВЗ)
def pull_returns(token, days=None, nms=None):
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
    if nms is not None:
        before = len(rows)
        rows = [x for x in rows if x.get("nmId") in nms]
        log(f"    бренд: {len(rows)} строк из {before}")
    kinds = collections.Counter(x.get("returnType") for x in rows)
    log(f"    возвраты: {len(rows)} строк; " +
        ", ".join(f"{k} — {v}" for k, v in kinds.most_common(3)))
    return rows


# ------------------------------------------------------- сборка и отгрузка
def pull_marketplace(token, days, nms=None):
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
    # сколько заданий в поставке всего, до фильтра по бренду: поставка общая
    # на кабинет, и сборщику важно видеть, что в коробке едет не только он
    sup_all = collections.Counter(o.get("supplyId") for o in orders if o.get("supplyId"))
    if nms is not None:
        before = len(orders)
        orders = [o for o in orders if o.get("nmId") in nms]
        log(f"    бренд: {len(orders)} заданий из {before}")
    slim = []
    for o in orders:
        s = st.get(o["id"]) or {}
        sku = (o.get("skus") or [None])[0]
        chrt = o.get("chrtId")
        slim.append(dict(id=o["id"], createdAt=o.get("createdAt"), supplyId=o.get("supplyId"),
                         nmId=o.get("nmId"), article=o.get("article"),
                         warehouseId=o.get("warehouseId"),
                         price=(o.get("convertedPrice") or o.get("price") or 0) / 100,
                         sku=sku, chrtId=chrt,
                         size=SIZES.get(chrt) or SIZES.get(str(sku)) or "",
                         office=(o.get("offices") or [None])[0],
                         rid=o.get("rid"), cargo=o.get("cargoType"),
                         supplierStatus=s.get("supplierStatus"), wbStatus=s.get("wbStatus")))
    # склады продавца: их может быть несколько, и отгружают они по-разному
    try:
        whs = call(token, H_MP, "/api/v3/warehouses") or []
    except Exception as e:
        log(f"    список складов недоступен: {e}")
        whs = []
    sup_out = [dict(id=s["id"], done=bool(s.get("done")),
                    name=s.get("name") or "",
                    createdAt=s.get("createdAt"), closedAt=s.get("closedAt"),
                    # scanDt — момент, когда поставку приняли на стороне WB;
                    # closedAt — когда её закрыл продавец. Разница важна для
                    # коэффициента скорости, поэтому храним оба
                    scanDt=s.get("scanDt"),
                    tasks_all=sup_all.get(s["id"], 0))
               for s in supplies]

    log(f"    сборка: {len(slim)} заданий, {len(supplies)} поставок, {len(whs)} складов")
    return dict(orders=slim,
                warehouses=[dict(id=w.get("id"), name=w.get("name")) for w in whs],
                supplies=sup_out)


# ----------------------------------------------------------------- финотчёт
FIN_KEEP = ("rrdId", "srid", "nmId", "vendorCode", "subjectName", "sellerOperName",
            "quantity", "retailPrice", "retailPriceWithDisc", "retailAmount",
            "forPay", "acquiringFee", "commissionPercent", "deliveryAmount", "returnAmount",
            "deliveryService", "rebillLogisticCost", "penalty", "deduction",
            "paidStorage", "paidAcceptance", "deliveryMethod",
            "saleDt", "orderDt", "rrDate", "dateFrom", "dateTo")


FIN_OPS_BY_NM = ("Доставка",)          # строки логистики FBS: свой srid, сшиваем по nmId
def fin_keep(r, keep_srids, keep_nms):
    """Что оставляем из финотчёта.

    Строки «Доставка» берём целиком: их мало (около тысячи в день на весь
    кабинет), а именно в них лежит логистика FBS — и сшить их по srid
    невозможно, у них собственный srid возврата.
    """
    if (r.get("sellerOperName") or "") in FIN_OPS_BY_NM:
        return True
    if keep_nms and r.get("nmId") in keep_nms:
        return True
    return keep_srids is not None and r.get("srid") in keep_srids


def pull_finance(token, keep_srids=None, keep_nms=None):
    """Недельными окнами: одним куском WB отдаёт сотни мегабайт.

    В большом кабинете за шесть недель это миллионы строк, и все они в памяти
    не нужны: экономику считаем по FBS-заказам окна и по карточкам бренда.
    Фильтруем сразу на приёме."""
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
            for r in batch:
                if keep_srids is not None and not fin_keep(r, keep_srids, keep_nms):
                    continue
                out.append({k: r.get(k) for k in FIN_KEEP})
            rrdid = max(r["rrdId"] for r in batch)
            log(f"    финотчёт {d0}—{d1}: пришло {len(batch)}, оставлено всего {len(out)}")
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


def orders_window(orders, nms, lo, hi):
    """Сырые FBS-заказы окна — знаменатель всех ставок на заказ."""
    out = [r for r in orders if r.get("warehouseType") == FBS and lo <= r["date"][:10] <= hi]
    if nms is not None:
        out = [r for r in out if r.get("nmId") in nms]
    return out


def buyout_block(orders, sold, nms, lo, hi, tag):
    """Выкуп по когорте СЫРЫХ заказов — со всеми, кто потом отменится.

    У WB isCancel=true ставится и на невыкуп, поэтому у свежего дня отмен почти
    нет, а у дозревшей когорты их больше половины. Считать выкуп можно только
    от сырого заказа, иначе свежий день завышен вдвое.
    """
    rows = orders_window(orders, nms, lo, hi)
    n = len(rows)
    if not n:
        return None
    bought = sum(1 for r in rows if r["srid"] in sold)
    return dict(tag=tag, window=f"{lo}—{hi}", orders_raw=n, bought=bought,
                buyout_of_raw=round(bought / n, 4),
                avg_order_price=round(sum(fnum(r.get("priceWithDisc")) for r in rows) / n, 2))


def cost_block(fin, nms, lo, hi, n, tag):
    """Что WB списал на заказы окна — по паре «артикул + дата заказа».

    Сшивать по srid нельзя. Логистика FBS приходит строкой «Доставка» с
    собственным srid вида mp.<хеш>.r — это номер возврата, с srid заказа он не
    сшивается вообще, и попытка склеить по нему даёт логистику в копейки вместо
    75–130 ₽ за штуку. Зато в строке есть nmId и orderDt — день исходного
    заказа, — и этого достаточно: ставка нужна на заказ, а не на конкретный srid.

    Знаменатель — сырые заказы окна, включая отменённые: WB платят и за
    доставку покупателю, и за обратный путь невыкупа.
    """
    if not n:
        return None
    a = collections.defaultdict(float)
    for r in fin:
        if nms is not None and r.get("nmId") not in nms:
            continue
        d = str(r.get("orderDt") or "")[:10]
        if not (lo <= d <= hi):
            continue
        op = r.get("sellerOperName") or ""
        if op == "Доставка":
            if not str(r.get("deliveryMethod") or "").upper().startswith("FBS"):
                continue
            a["log"] += fnum(r.get("deliveryService")) + fnum(r.get("rebillLogisticCost"))
            a["dev"] += fnum(r.get("deliveryAmount"))
            a["ret"] += fnum(r.get("returnAmount"))
            continue
        a["acc"] += fnum(r.get("paidAcceptance"))
        a["pen"] += fnum(r.get("penalty"))
        a["sto"] += fnum(r.get("paidStorage"))
        if op == "Продажа":
            a["retail"] += fnum(r.get("retailPriceWithDisc"))
            a["customer"] += fnum(r.get("retailAmount"))
            a["forpay"] += fnum(r.get("forPay"))
            a["qty"] += fnum(r.get("quantity"))
        elif op == "Возврат":
            a["retail"] -= fnum(r.get("retailPriceWithDisc"))
            a["customer"] -= fnum(r.get("retailAmount"))
            a["forpay"] -= fnum(r.get("forPay"))
            a["qty"] -= fnum(r.get("quantity"))
    retail = a["retail"]
    return dict(
        tag=tag, window=f"{lo}—{hi}", orders_raw=n,
        logistics=round(a["log"], 2),
        logistics_per_order=round(a["log"] / n, 2),
        deliveries=int(a["dev"]), returns=int(a["ret"]),
        events_per_order=round((a["dev"] + a["ret"]) / n, 3),
        handling_per_order=round(a["acc"] / n, 2),
        penalty_per_order=round(a["pen"] / n, 2),
        storage_per_order=round(a["sto"] / n, 2),
        sale_qty=int(a["qty"]),
        retail=round(retail, 2), forpay=round(a["forpay"], 2),
        payout_share=round(a["forpay"] / retail, 4) if retail else None,
        spp_share=round(1 - a["customer"] / retail, 4) if retail else None,
    )


def payout_block(fin, srids, tag):
    """Сколько из цены продавца доходит до продавца — по продажам, без когорты:
    это отношение, а не ставка на заказ. Строки «Продажа» и «Возврат» приходят
    с srid заказа, поэтому здесь сшивка по srid работает."""
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


def rates_from_finance(fin, orders_all, sales_all, nms=None):
    """Ставки юнит-экономики. Каждая берётся оттуда, где её вообще можно измерить.

    Выкуп — только по дозревшей когорте, и у молодого бренда её нет, поэтому он
    одалживается у FBS всего кабинета. Логистика и приёмка — физика конкретного
    товара, их берём по бренду, как только в отчёте появятся его строки. Доля к
    перечислению — отношение по продажам, ей когорта не нужна.
    Источник каждой ставки записан рядом и виден в дашборде.
    """
    if not fin:
        return dict(payout_share=None, error="финотчёт пуст",
                    updated_at=NOW.isoformat(timespec="seconds"))
    covered_to = max((r.get("dateTo") or "")[:10] for r in fin)
    cov_d = datetime.date.fromisoformat(covered_to)
    fbs_all = [r for r in orders_all if r.get("warehouseType") == FBS]
    if not fbs_all:
        return dict(payout_share=None, error="нет FBS-заказов",
                    updated_at=NOW.isoformat(timespec="seconds"))
    first = min(r["date"][:10] for r in fbs_all)
    lo = max((cov_d - datetime.timedelta(days=int(os.environ.get("COHORT_LO", "21")))).isoformat(),
             first)
    hi = (cov_d - datetime.timedelta(days=int(os.environ.get("COHORT_HI", "8")))).isoformat()
    if hi < lo:
        hi = max(lo, (cov_d - datetime.timedelta(days=1)).isoformat())

    sold = {r["srid"] for r in sales_all if str(r.get("saleID", "")).startswith("S")}
    label = ", ".join(sorted(BRANDS)).upper() if BRANDS else ""
    # артикулы, у которых в окне вообще были FBS-заказы: это и есть «кабинет»
    # для ставок — по ним и считаем, чтобы в знаменатель не попал чистый FBW
    cab_nms = {r.get("nmId") for r in orders_window(fbs_all, None, lo, hi)}

    n_cab = len(orders_window(fbs_all, cab_nms, lo, hi))
    n_brand = len(orders_window(fbs_all, nms, lo, hi)) if BRANDS else 0

    cost_brand = cost_block(fin, nms, lo, hi, n_brand, f"FBS бренда {label}") if n_brand else None
    cost_cab = cost_block(fin, cab_nms, lo, hi, n_cab, "FBS кабинета")
    buy_brand = buyout_block(fbs_all, sold, nms, lo, hi, f"FBS бренда {label}") if BRANDS else None
    buy_cab = buyout_block(fbs_all, sold, cab_nms, lo, hi, "FBS кабинета")

    MIN_BRAND = int(os.environ.get("MIN_COHORT_BRAND", "60"))
    MIN_LOGI = int(os.environ.get("MIN_LOGI_EVENTS", "20"))
    use_brand_cost = bool(cost_brand and cost_brand["deliveries"] >= MIN_LOGI)
    cost = cost_brand if use_brand_cost else cost_cab
    cost_src = cost["tag"] if cost else None
    if cost_brand and not use_brand_cost:
        cost_src = (f"FBS кабинета — по бренду {label} пока "
                    f"{cost_brand['deliveries']} оплаченных доставок из нужных {MIN_LOGI}")

    use_brand_buy = bool(buy_brand and buy_brand["orders_raw"] >= MIN_BRAND)
    buy = buy_brand if use_brand_buy else buy_cab
    source = (f"FBS бренда {label}" if use_brand_buy else
              (f"FBS кабинета — у бренда {label} когорта ещё не дозрела" if BRANDS
               else "FBS кабинета"))

    fbs_srids = {r["srid"] for r in fbs_all} | \
        {r["srid"] for r in sales_all if r.get("warehouseType") == FBS}
    brand_srids = ({r["srid"] for r in by_brand(fbs_all, nms)} |
                   {r["srid"] for r in by_brand(sales_all, nms)
                    if r.get("warehouseType") == FBS}) if BRANDS else set()
    pay_brand = payout_block(fin, brand_srids, f"FBS бренда {label}") if brand_srids else None
    pay_cab = payout_block(fin, fbs_srids, "FBS кабинета")
    MIN_PAY = int(os.environ.get("MIN_PAYOUT_QTY", "30"))
    pay = pay_brand if (pay_brand and pay_brand["qty"] >= MIN_PAY) else pay_cab
    use_pay = bool(pay and pay["qty"] >= MIN_PAY)

    # сколько дней проходит от заказа до возврата товара на склад при невыкупе
    lag = []
    for r in fin:
        if nms and r.get("nmId") not in nms:
            continue
        if fnum(r.get("returnAmount")) > 0 and r.get("orderDt") and r.get("rrDate"):
            try:
                dd = (datetime.date.fromisoformat(str(r["rrDate"])[:10])
                      - datetime.date.fromisoformat(str(r["orderDt"])[:10])).days
            except Exception:
                continue
            if 0 < dd < 60:
                lag.append(dd)
    lag.sort()
    return_lag = dict(qty=len(lag), median=lag[len(lag) // 2] if lag else None,
                      p25=lag[int(len(lag) * .25)] if lag else None,
                      p75=lag[int(len(lag) * .75)] if lag else None) if lag else None

    return dict(
        return_lag=return_lag,
        payout_share=pay["payout_share"] if use_pay else None,
        payout_source=(f"{pay['tag']}, {pay['qty']} шт продаж" if use_pay
                       else "продаж в финотчёте пока мало"),
        payout_fbs=pay, payout_brand=pay_brand, payout_whole=pay_cab,
        payout_fbs_qty=(pay or {}).get("qty", 0),
        spp_share=(pay or {}).get("spp_share"),
        logistics_per_order=cost["logistics_per_order"] if cost else None,
        logistics_fwd_per_order=None, logistics_back_per_order=None,
        logistics_events_per_order=cost["events_per_order"] if cost else None,
        logistics_source=cost_src,
        handling_per_order=cost["handling_per_order"] if cost else None,
        penalty_per_order=cost["penalty_per_order"] if cost else None,
        storage_per_order=cost["storage_per_order"] if cost else None,
        buyout_of_raw=buy["buyout_of_raw"] if buy else None,
        buyout_source=buy["tag"] if buy else None,
        source=source,
        cohort_whole=buy_cab, cohort_fbs=buy_cab, cohort_brand=buy_brand,
        cost_brand=cost_brand, cost_cabinet=cost_cab,
        covered_to=covered_to,
        updated_at=NOW.isoformat(timespec="seconds"),
    )


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

    need_fin = any(stale(k) for k, _, _ in CABS)
    # Ставки логистики считаются по дозревшей когорте: заказ должен успеть
    # доехать и попасть в закрытую неделю финотчёта. От 1 сентября такой когорты
    # ещё нет, поэтому в тот прогон, когда пересобирается финотчёт, заказы тянем
    # глубже — только ради знаменателя ставок. На странице всё равно остаётся
    # период с start_date: лишнее отрезается перед сохранением.
    global START
    disp_start = START
    if need_fin:
        deep = int(os.environ.get("RATES_WINDOW_DAYS", "45"))
        START = min(START, TODAY - datetime.timedelta(days=deep - 1))
    log(f"окно заказов: {START} — {TODAY} (МСК {NOW:%H:%M})"
        + (f", на странице с {disp_start}" if START != disp_start else ""))

    if BRANDS:
        log("  бренд: " + ", ".join(sorted(BRANDS)).upper())

    NMS = {}
    for key, title, tok in CABS:
        if not BRANDS:
            NMS[key] = None
            continue
        log(f"  [{title}] карточки бренда")
        try:
            nms, art = brand_nmids(tok)
        except Exception as e:
            log(f"    список карточек недоступен: {e}")
            nms, art = set(), {}
        NMS[key] = nms
        save(f"nm_{key}", dict(nms=sorted(nms),
                               articles={str(k): v for k, v in art.items()},
                               cards={str(k): v for k, v in CARDS.items()}))

    # Выгружаем кабинет целиком, а на диск кладём только бренд: ставки
    # экономики считаются по кабинету, а весь дашборд — по бренду.
    orders, sales = {}, {}
    if WAREHOUSE:
        # Заказы и продажи нужны разделу «Сроки и путь заказа»: он считает,
        # за сколько дней заказ доезжает до ПВЗ и до выкупа. Денег в них не
        # берём — цена обнуляется в своде. Финотчёт и реклама не выгружаются
        # вовсе: без них прогон занимает пару минут вместо двадцати.
        log("  складской режим: финотчёт и реклама не выгружаются")
        for key, title, tok in CABS:
            log(f"  [{title}] заказы")
            o = pull_stat(tok, "/api/v1/supplier/orders", "заказы", START)
            b = [r for r in by_brand(o, NMS[key])
                 if r.get("date", "")[:10] >= START.isoformat()]
            log(f"    бренд: {len(b)} заказов из {len(o)}")
            save(f"orders_{key}", b)
        time.sleep(62)
        for key, title, tok in CABS:
            log(f"  [{title}] продажи")
            sl = pull_stat(tok, "/api/v1/supplier/sales", "продажи", START, key="saleID+srid")
            save(f"sales_{key}", by_brand(sl, NMS[key]))
        for key, title, tok in CABS:
            log(f"  [{title}] сборка и отгрузка")
            asm = int(os.environ.get("ASSEMBLY_DAYS", "30"))
            if START_DATE:
                asm = min(asm, (TODAY - START_DATE).days + 1)
            save(f"mp_{key}", pull_marketplace(tok, asm, NMS[key]))
            save(f"adv_{key}", [])
        log("готово")
        return

    for key, title, tok in CABS:
        log(f"  [{title}] заказы")
        orders[key] = pull_stat(tok, "/api/v1/supplier/orders", "заказы", START)
        b = [r for r in by_brand(orders[key], NMS[key])
             if r.get("date", "")[:10] >= disp_start.isoformat()]
        log(f"    бренд: {len(b)} заказов из {len(orders[key])}")
        save(f"orders_{key}", b)

    time.sleep(62)
    for key, title, tok in CABS:
        log(f"  [{title}] продажи")
        sales[key] = pull_stat(tok, "/api/v1/supplier/sales", "продажи", START, key="saleID+srid")
        b = [r for r in by_brand(sales[key], NMS[key])
             if r.get("date", "")[:10] >= disp_start.isoformat()]
        log(f"    бренд: {len(b)} строк из {len(sales[key])}")
        save(f"sales_{key}", b)

    for key, title, tok in CABS:
        log(f"  [{title}] реклама")
        try:
            upd, advnm = pull_adv(tok, NMS[key])
        except Exception as e:
            log(f"    реклама недоступна: {e}")
            upd, advnm = [], []
        save(f"adv_{key}", upd)
        if advnm:
            save(f"advnm_{key}", advnm)

    asm_days = int(os.environ.get("ASSEMBLY_DAYS", "30"))
    if START_DATE:
        asm_days = min(asm_days, (TODAY - START_DATE).days + 1)
    for key, title, tok in CABS:
        log(f"  [{title}] сборка и отгрузка")
        try:
            save(f"mp_{key}", pull_marketplace(tok, asm_days, NMS[key]))
        except Exception as e:
            log(f"    раздел сборки недоступен: {e}")

    for key, title, tok in CABS:
        log(f"  [{title}] возвраты продавцу")
        try:
            rd = int(os.environ.get("RETURNS_DAYS", "60"))
            if START_DATE:
                rd = min(rd, (TODAY - START_DATE).days + 1)
            save(f"ret_{key}", pull_returns(tok, days=rd, nms=NMS[key]))
        except Exception as e:
            log(f"    отчёт по возвратам недоступен: {e}")

    for key, title, tok in CABS:
        if not stale(key):
            log(f"  [{title}] финотчёт свежий ({state[key]['updated_at']}) — пропускаю")
            continue
        log(f"  [{title}] финотчёт (долго)")
        try:
            keep = {r["srid"] for r in orders[key] if r.get("warehouseType") == FBS} | \
                   {r["srid"] for r in sales[key] if r.get("warehouseType") == FBS}
            fin = pull_finance(tok, keep_srids=keep, keep_nms=NMS[key])
            save(f"fin_{key}", fin)
            state[key] = rates_from_finance(fin, orders[key], sales[key], NMS[key])
            log(f"    ставки: до продавца доходит {state[key]['payout_share']}, "
                f"логистика/заказ {state[key]['logistics_per_order']} "
                f"({state[key].get('logistics_source')}), "
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
