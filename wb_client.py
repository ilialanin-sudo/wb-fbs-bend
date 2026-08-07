#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Минимальный HTTP-клиент WB: Authorization без Bearer, честная отработка 429."""
import json, time, urllib.request, urllib.error
from urllib.parse import urlencode

UA = "wb-fbs-dashboard/1.0"


class WBError(RuntimeError):
    pass


def log(*a):
    print(*a, flush=True)


def call(token, host, path, method="GET", query=None, body=None,
         timeout=900, tries=6, max_wait=400):
    """Один запрос к WB. На 429 ждём ровно X-Ratelimit-Retry и не раньше."""
    url = f"https://{host}{path}" + (("?" + urlencode(query)) if query else "")
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Authorization": token.strip(), "User-Agent": UA}
    if data is not None:
        hdr["Content-Type"] = "application/json"
    last = None
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, data=data, headers=hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.status == 204 or not raw.strip():
                    return None
                return json.loads(raw.decode())
        except urllib.error.HTTPError as e:
            body_txt = ""
            try:
                body_txt = e.read().decode()[:300]
            except Exception:
                pass
            if e.code == 429:
                wait = int(e.headers.get("X-Ratelimit-Retry")
                           or e.headers.get("Retry-After") or 62)
                if wait > max_wait:
                    raise WBError(f"429 {path}: WB просит ждать {wait} с — прерываю")
                log(f"    429 {path}: жду {wait} с (попытка {attempt}/{tries})")
                time.sleep(wait + 2)
                last = e
                continue
            if e.code in (500, 502, 503, 504):
                log(f"    HTTP {e.code} {path}: повтор через 15 с ({attempt}/{tries})")
                time.sleep(15)
                last = e
                continue
            raise WBError(f"HTTP {e.code} {path}: {body_txt}")
        except (urllib.error.URLError, TimeoutError) as e:
            log(f"    сеть {path}: {e}; повтор через 10 с ({attempt}/{tries})")
            time.sleep(10)
            last = e
            continue
    raise WBError(f"не достучались до {path}: {last}")
