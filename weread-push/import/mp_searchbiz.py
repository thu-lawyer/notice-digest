# -*- coding: utf-8 -*-
"""通过 CDP 在已登录的 mp.weixin.qq.com 页面里调 searchbiz 接口查公众号 biz。
用法: python3 mp_searchbiz.py --probe <名称>   探针一个
      python3 mp_searchbiz.py --full <名单json>  全量查询(名单=biz-map miss 列表或原始132名单)
"""
import json, sys, time, urllib.parse, urllib.request, os

CDP = "http://127.0.0.1:9222"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mp-searchbiz-108.json")
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mp-searchbiz.log")

def get_tab():
    tabs = json.load(urllib.request.urlopen(CDP + "/json"))
    for t in tabs:
        if t.get("type") == "page" and "mp.weixin.qq.com" in t.get("url", "") and "token=" in t.get("url", ""):
            return t
    raise SystemExit("no logged-in mp.weixin tab")

def evaluate(ws_url, expr, timeout=30):
    import websocket
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expr, "returnByValue": True, "awaitPromise": True}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                r = msg.get("result", {}).get("result", {})
                if r.get("subtype") == "error":
                    raise RuntimeError(r.get("description", "js error"))
                return r.get("value")
    finally:
        ws.close()

def search_one(tab, name, token):
    q = urllib.parse.quote(name)
    url = ("https://mp.weixin.qq.com/cgi-bin/searchbiz?action=search_biz&begin=0&count=5"
           f"&query={q}&token={token}&lang=zh_CN&f=json&ajax=1")
    expr = f"fetch({json.dumps(url)}).then(r=>r.text())"
    raw = evaluate(tab["webSocketDebuggerUrl"], expr)
    try:
        d = json.loads(raw)
    except Exception:
        return {"ret": -1, "err": "non-json: " + str(raw)[:120]}
    ret = d.get("base_resp", {}).get("ret", -99)
    out = {"ret": ret, "cands": []}
    for it in (d.get("list") or [])[:5]:
        out["cands"].append({"fakeid": it.get("fakeid"), "nickname": it.get("nickname"),
                             "alias": it.get("alias"), "round_head_img": it.get("round_head_img", "")[:80]})
    return out

def norm(s):
    return (s or "").replace(" ", "").replace("\u3000", "").strip()

def main():
    tab = get_tab()
    token = tab["url"].split("token=")[1].split("&")[0]
    args = sys.argv[1:]
    mode = args[0]
    if mode == "--probe":
        r = search_one(tab, args[1], token)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    # full
    src = json.load(open(args[1]))
    if isinstance(src, dict):           # biz-map {hit:{name:biz}, miss:[names]}
        names = src.get("miss", [])
    else:                               # raw list of {mp_name / mp_id}
        names = [x.get("mp_name") for x in src if not x.get("mp_id")]
    result = {"hits": {}, "multi": {}, "miss": [], "fail": []}
    for i, name in enumerate(names, 1):
        try:
            r = search_one(tab, name, token)
        except Exception as e:
            result["fail"].append({"name": name, "err": str(e)[:150]}); r = None
        if r is None:
            pass
        elif r.get("ret") != 0:
            result["fail"].append({"name": name, "ret": r.get("ret"), "err": r.get("err", "")})
        else:
            exact = [c for c in r["cands"] if norm(c["nickname"]) == norm(name)]
            if len(exact) == 1:
                c = exact[0]; result["hits"][name] = {"fakeid": c["fakeid"], "nickname": c["nickname"]}
            elif len(exact) > 1:
                result["multi"][name] = exact
            elif r["cands"]:
                result["multi"][name] = r["cands"][:3]
            else:
                result["miss"].append(name)
        print(f"[{i}/{len(names)}] {name} ret={r['ret'] if r else 'EXC'}", flush=True)
        if i % 10 == 0:
            json.dump(result, open(OUT, "w"), ensure_ascii=False, indent=1)
        time.sleep(2.8)
    json.dump(result, open(OUT, "w"), ensure_ascii=False, indent=1)
    print(f"DONE hit={len(result['hits'])} multi={len(result['multi'])} miss={len(result['miss'])} fail={len(result['fail'])}", flush=True)

if __name__ == "__main__":
    main()
