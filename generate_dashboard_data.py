#!/usr/bin/env python3
"""生成增强版 dashboard data.json — 从 Notion KOL By Day 拉全量, 算加权 sector 评分 + 5项增强。
加权方法(Chao 确认: 全历史指数衰减, 评分只在 Sector Summary):
  每条观点情绪分 = direction(+1看多/0中性/-1看空) * recency * novelty * confidence * style_adj
  - recency: 指数衰减 0.5^(days_ago/30) (30天半衰期)
  - novelty: 方向反转2.0 / 新催化剂1.5 / 维持0.6 (按该KOL前一条观点比较)
  - confidence: 有target price+catalyst 1.3 / 有标的方向1.0 / 泛泛0.5
  - style_adj: 交易派方向1.0 / 持仓派方向0.6
  sector_score = sum(情绪分) 归一化到 -100~+100
输出: dashboard/kol-dashboard/data.json
"""
import json, os, re, math, urllib.request
from datetime import datetime, date, timezone, timedelta
from collections import defaultdict, Counter

BASE=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 项目根(dashboard/kol-dashboard/ 往上3级)
def lk(n):
    p=n+"="
    for l in open(os.path.join(BASE,"config",".env")):
        if l.startswith(p): return l[len(p):].strip()
TOK=lk("NOTION_"+"TOKEN")
H={"Authorization":f"Bearer {TOK}","Notion-Version":"2022-06-28","Content-Type":"application/json"}
DB="32347eb5fd3c8087b9c0f409f95f664e"
TODAY=datetime.now(timezone(timedelta(hours=9))).date()  # JST 今天(动态, recency衰减基准)

# 持仓派(方向几乎不变, 降权; 看催化剂) vs 其余默认交易派
HOLDERS={"Peter Schiff","Robert Kiyosaki","Rick Rule","Luke Gromen","Keith Neumeyer",
         "Willem Middelkoop","James Rickards","Michael Saylor","Ray Dalio","Jim Rogers",
         "Stephen Leeb","Alasdair Macleod","Egon von Greyerz","Matthew Piepenburg"}
# 机构(不算 KOL 信号)和非标准 sector 过滤
SKIP_KOL={"Goldman Sachs","Morgan Stanley","Citi / UBS","Citi","UBS"}
STD_SECTORS={"Precious Metals","Macro","Energy & Commodities","Crypto","Equities","Government Debt","Alternative"}

def txt(p):
    if not p: return ""
    t=p.get("type")
    if t in("title","rich_text"): return "".join(x.get("plain_text","") for x in p[t])
    if t=="select": return p["select"]["name"] if p["select"] else ""
    if t=="date": return p["date"]["start"] if p["date"] else ""
    return ""

def fetch_all():
    rows=[];cursor=None
    while True:
        body={"page_size":100}
        if cursor:body["start_cursor"]=cursor
        r=json.load(urllib.request.urlopen(urllib.request.Request(
            f"https://api.notion.com/v1/databases/{DB}/query",
            data=json.dumps(body).encode(),headers=H,method="POST"),timeout=45))
        for row in r["results"]:
            P=row["properties"]
            rows.append({"id":row["id"],"name":txt(P.get("Name")),"kol_name":txt(P.get("Name of KOL")),
                "kol_or_ib":txt(P.get("KOL or IB View")),"date":txt(P.get("Date")),
                "sector":txt(P.get("Sector")),"detail_sector":txt(P.get("Detail Sector")),
                "comments":txt(P.get("Comments")),"suggestion":txt(P.get("Suggestion")),
                "bull_bear":txt(P.get("多空标的")),
                "direction_detail":txt(P.get("方向明细")),"dominant":txt(P.get("主导方向"))})
        if not r.get("has_more"):break
        cursor=r["next_cursor"]
    return rows

# 方向取值 -> 数值分(强度). 用于三元组摊平统计真实多空.
DIR_SCORE={"强烈看多":2,"看多":1,"中性":0,"看空":-1,"强烈看空":-2,"分歧":0}
def parse_legs(e):
    """解析方向明细JSON -> [{标的,板块,方向}]. 解析失败/为空返回 None(表示该条未结构化回填)."""
    md=(e.get("direction_detail") or "").strip()
    if not md: return None
    try:
        legs=json.loads(md)
        if isinstance(legs,list) and legs: return legs
    except Exception:
        return None
    return None

def direction(e):
    """从标题/多空标的判断方向 +1/0/-1"""
    s=(e["name"]+" "+e["bull_bear"]+" "+e["comments"][:50])
    bull="🟢" in s or "看多" in e["name"] or "看涨" in e["name"]
    bear="🔴" in s or "看空" in e["name"] or "看跌" in e["name"] or "崩盘" in e["name"]
    if bull and not bear: return 1
    if bear and not bull: return -1
    if bull and bear: return 0  # 矛盾=中性
    if "🟡" in s or "中性" in e["name"] or "震荡" in e["name"]: return 0
    return 0

def recency(e):
    try: d=datetime.strptime(e["date"],"%Y-%m-%d").date()
    except: return 0.1
    days=(TODAY-d).days
    if days<0: days=0
    return 0.5**(days/30.0)  # 30天半衰期

def confidence(e):
    c=e["comments"]+e["bull_bear"]
    has_price=bool(re.search(r'\$?\d{3,5}|目标价|target',c))
    has_catalyst=bool(re.search(r'催化|因为|→.*→|Fed|央行|地缘|霍尔木兹|关税|赤字',c))
    if has_price and has_catalyst: return 1.3
    if e["bull_bear"].strip(): return 1.0
    return 0.5

def novelty(e,prev_dir):
    """与该KOL前一条观点比较: 反转2.0/新观点1.5/维持0.6"""
    d=direction(e)
    if prev_dir is None: return 1.5  # 首条算新
    if d!=0 and prev_dir!=0 and d!=prev_dir: return 2.0  # 反转
    if d==prev_dir: return 0.6  # 维持
    return 1.5

def main():
    rows=fetch_all()
    # 按 KOL+日期排序, 计算每条 novelty(需前一条方向)
    by_kol=defaultdict(list)
    for e in rows: by_kol[e["kol_name"]].append(e)
    for k in by_kol: by_kol[k].sort(key=lambda x:x["date"] or "")

    sector_pts=defaultdict(list)   # sector -> [情绪分] (加权评分用)
    sector_raw=defaultdict(lambda:{"bull":0,"bear":0,"neutral":0,"total":0})
    # 三元组摊平统计(真实多空,核心修复): 按方向明细的每个标的腿计入其板块
    sector_legs=defaultdict(lambda:{"强烈看多":0,"看多":0,"中性":0,"看空":0,"强烈看空":0,"分歧":0,"legs_total":0})
    ticker_dir=defaultdict(lambda:{"bull":0,"bear":0,"kols":set()})  # 标的 -> 多空(来自结构化腿)
    legged_rows=0  # 有结构化方向明细的记录数
    stance_changes=[]
    for kol,items in by_kol.items():
        if not kol: continue
        prev_dir=None
        is_holder=kol in HOLDERS
        for e in items:
            legs=parse_legs(e)
            if legs is not None:
                legged_rows+=1
                # 三元组摊平: 每条腿计入其板块的方向分布 + 标的多空
                for leg in legs:
                    sec=leg.get("板块",""); dr=leg.get("方向",""); tk=leg.get("标的","")
                    if sec in STD_SECTORS and dr in DIR_SCORE:
                        sl=sector_legs[sec]; sl[dr]+=1; sl["legs_total"]+=1
                        # 加权评分: 腿方向分 * recency * style
                        style=0.6 if (is_holder and DIR_SCORE[dr]!=0) else 1.0
                        sector_pts[sec].append(DIR_SCORE[dr]*recency(e)*style)
                        if tk:
                            td=ticker_dir[tk]
                            if DIR_SCORE[dr]>0: td["bull"]+=1
                            elif DIR_SCORE[dr]<0: td["bear"]+=1
                            td["kols"].add(kol)
                # dominant 方向用于反转检测
                d=DIR_SCORE.get(e.get("dominant",""),0)
                d=1 if d>0 else (-1 if d<0 else 0)
            else:
                # 未回填: 回退旧文本法(标注), 仅计入 raw 不污染结构化统计
                d=direction(e)
                r=recency(e); nov=novelty(e,prev_dir); conf=confidence(e)
                style=0.6 if (is_holder and d!=0) else 1.0
                pts=d*r*nov*conf*style
                if e["sector"] in STD_SECTORS:
                    sector_pts[e["sector"]].append(pts)
            # raw 多空人头(用 dominant 或文本法)
            sec_for_raw=None
            if legs is not None:
                # 用主导板块=第一条腿的板块, 或原sector标准化
                sec_for_raw=legs[0].get("板块") if legs[0].get("板块") in STD_SECTORS else None
            if not sec_for_raw and e["sector"] in STD_SECTORS:
                sec_for_raw=e["sector"]
            if sec_for_raw:
                sr=sector_raw[sec_for_raw]; sr["total"]+=1
                sr["bull" if d>0 else ("bear" if d<0 else "neutral")]+=1
            # 反转检测
            if prev_dir is not None and d!=0 and prev_dir!=0 and d!=prev_dir and recency(e)>0.4:
                stance_changes.append({"kol_name":kol,"sector":(legs[0].get("板块") if legs else e["sector"]),"date":e["date"],
                    "from":"看多" if prev_dir>0 else "看空","to":"看多" if d>0 else "看空",
                    "title":e["name"][:50],"signal":"高" if not is_holder else "中"})
            prev_dir=d if d!=0 else prev_dir

    # sector 评分 = 共识度驱动的净多空占比(Chao要求: 有反对意见就不该满分±100)
    # score = 100 × (加权看多 − 加权看空) / (加权看多 + 加权看空 + 分歧)
    # 只有【零反对】才可能到±100; 有1个相反立场就<100; 多空各半=0
    sector_summary=[]
    all_secs=set(sector_pts)|set(sector_legs)|set(sector_raw)
    # 各方向的强度权重(绝对值): 强烈=2, 普通=1; 分歧计入分母但不计净值
    for sec in all_secs:
        sl=sector_legs[sec]
        # 加权多空量(强度加权)
        w_bull=sl["强烈看多"]*2 + sl["看多"]*1
        w_bear=sl["强烈看空"]*2 + sl["看空"]*1
        w_split=sl["分歧"]*1   # 分歧=两边都有,稀释共识,计入分母
        denom=w_bull+w_bear+w_split
        if denom>0:
            score=round(100*(w_bull-w_bear)/denom,0)
        else:
            score=0
        sr=sector_raw[sec]
        legs_bull=sl["强烈看多"]+sl["看多"]
        legs_bear=sl["看空"]+sl["强烈看空"]
        legs_neutral=sl["中性"]; legs_split=sl["分歧"]
        # 共识度: 净占比的绝对值越高越一致; |score|=100 表示零反对
        consensus=abs(score)
        sector_summary.append({"sector":sec,"score":int(score),
            "consensus":int(consensus),
            "weighted_sum":round(sum(sector_pts.get(sec,[])),1),
            # 行级人头(主导方向)
            "bull":sr["bull"],"bear":sr["bear"],"neutral":sr["neutral"],"total":sr["total"],
            # 腿级真实多空(三元组摊平 — 雷达图/多空指标用这个)
            "legs_bull":legs_bull,"legs_bear":legs_bear,"legs_neutral":legs_neutral,
            "legs_split":legs_split,"legs_total":sl["legs_total"],
            "strong_bull":sl["强烈看多"],"strong_bear":sl["强烈看空"],
            "w_bull":w_bull,"w_bear":w_bear,
            "avg":round(sum(sector_pts.get(sec,[]))/len(sector_pts[sec]),2) if sector_pts.get(sec) else 0})
    sector_summary.sort(key=lambda x:-x["score"])

    # ticker_heatmap: 优先用结构化 ticker_dir(方向明细的标的腿), 回退旧文本法
    if ticker_dir:
        bull_list=sorted([(t,v) for t,v in ticker_dir.items() if v["bull"]>v["bear"]],key=lambda x:-x[1]["bull"])[:15]
        bear_list=sorted([(t,v) for t,v in ticker_dir.items() if v["bear"]>v["bull"]],key=lambda x:-x[1]["bear"])[:15]
        ticker_heatmap={"bull":[{"ticker":t,"count":v["bull"],"kols":len(v["kols"])} for t,v in bull_list],
                        "bear":[{"ticker":t,"count":v["bear"],"kols":len(v["kols"])} for t,v in bear_list]}
    else:
        bull_t=Counter();bear_t=Counter();ticker_kols=defaultdict(set)
        for e in rows:
            bb=e["bull_bear"]
            for m in re.finditer(r'([A-Z]{2,5})',bb):
                tk=m.group(1)
                if "🔴" in bb[:m.start()][-20:]: bear_t[tk]+=1
                else: bull_t[tk]+=1
                ticker_kols[tk].add(e["kol_name"])
        ticker_heatmap={"bull":[{"ticker":t,"count":c,"kols":len(ticker_kols[t])} for t,c in bull_t.most_common(15)],
                        "bear":[{"ticker":t,"count":c,"kols":len(ticker_kols[t])} for t,c in bear_t.most_common(15)]}

    # kol_cards (含派别 + 最近vs3月前基线)
    kol_cards=[]
    for kol,items in by_kol.items():
        if not kol: continue
        latest=items[-1]
        recent=[e for e in items if recency(e)>0.5]  # 近~30天
        old=[e for e in items if 0.05<recency(e)<0.2]  # ~3月前
        rdir=sum(direction(e) for e in recent)
        odir=sum(direction(e) for e in old)
        trend="→"
        if rdir>odir: trend="↗ 趋多"
        elif rdir<odir: trend="↘ 趋空"
        kol_cards.append({"kol_name":kol,"sector":latest["sector"],
            "detail_sector":latest["detail_sector"],"kol_or_ib":latest["kol_or_ib"],
            "style":"持仓派" if kol in HOLDERS else "交易派",
            "latest_date":latest["date"],"latest_comments":latest["comments"][:200],
            "latest_title":latest["name"],"bull_bear":latest["bull_bear"],
            "total_entries":len(items),"trend":trend})
    kol_cards.sort(key=lambda x:x["latest_date"] or "",reverse=True)

    # 今日信号(置顶): 近期高价值=反转 + 高置信新观点(过滤机构)
    today_signals=[]
    for sc in sorted(stance_changes,key=lambda x:x["date"],reverse=True):
        if sc["kol_name"] in SKIP_KOL: continue
        today_signals.append({"type":"反转","kol_name":sc["kol_name"],"sector":sc["sector"],
            "date":sc["date"],"desc":f"{sc['from']}→{sc['to']}",
            "signal":sc["signal"]})
        if len(today_signals)>=10: break

    out={"generated_at":datetime.now().strftime("%Y-%m-%d %H:%M JST"),
        "date_range":{"start":min(e["date"] for e in rows if e["date"]),
                      "end":max(e["date"] for e in rows if e["date"])},
        "scoring_method":"结构化方向明细(按标的拆分多空)三元组摊平 × 强度(强烈±2/普通±1) × 指数衰减(30天半衰期) × 派别(持仓派0.6)",
        "direction_coverage":{"legged_rows":legged_rows,"total_rows":len(rows),
            "pct":round(100*legged_rows/max(1,len(rows)),1)},
        "today_signals":today_signals,
        "sector_summary":sector_summary,
        "stance_changes":sorted(stance_changes,key=lambda x:x["date"],reverse=True)[:30],
        "ticker_heatmap":ticker_heatmap,
        "kol_cards":kol_cards,
        "weekly_reports":[],  # 保留原有(下方从旧文件继承)
        "raw_entries":[{k:e[k] for k in ("id","name","kol_name","kol_or_ib","date","sector","detail_sector","comments","suggestion","bull_bear")} for e in rows]}
    # 继承旧 weekly_reports
    try:
        old=json.load(open(os.path.join(os.path.dirname(__file__),"data.json")))
        out["weekly_reports"]=old.get("weekly_reports",[])
    except: pass

    outpath=os.path.join(os.path.dirname(__file__),"data.json")
    json.dump(out,open(outpath,"w"),ensure_ascii=False,indent=2)
    print(f"生成 data.json: {len(rows)} entries | 已结构化方向: {legged_rows}/{len(rows)} ({round(100*legged_rows/max(1,len(rows)),1)}%)")
    print(f"Sector 共识度评分(净多空占比, 仅零反对才±100):")
    for s in sector_summary:
        print(f"  {s['sector']:22} score={s['score']:+4d}(共识{s['consensus']}) | 加权多{s['w_bull']}/空{s['w_bear']} | 腿:多{s['legs_bull']}/空{s['legs_bear']}(强{s['strong_bear']})/中{s['legs_neutral']}/分歧{s['legs_split']}")
    print(f"反转信号: {len(stance_changes)} | 今日信号: {len(today_signals)}")

if __name__=="__main__":
    main()
