#!/usr/bin/env python3
"""
부가서비스 대시보드 데이터 생성기 (결정형).
bq CLI로 BigQuery를 집계하고 고정 공식으로 지표를 계산해 data.json을 출력한다.
사람이 손으로 숫자를 고치지 않으므로 정합성/누락/표현 변동이 구조적으로 발생하지 않는다.

usage: python3 gen_data.py [--cut YYYY-MM-DD] [--start YYYY-MM-DD] [--out data.json]
  --cut   집계 종료일(포함). 기본 = 어제 KST (이벤트 D+1 적재 때문).
  --start PoC 시작일. 기본 2026-06-05.
"""
import argparse, json, subprocess, sys, math
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
BQ_PROJECT = "socar-data"
ADDON = "('addon_carseat','addon_stroller')"


def bq(sql):
    """bq CLI로 쿼리 실행, 행을 dict 리스트로 반환 (값은 문자열)."""
    cmd = ["bq", "query", f"--project_id={BQ_PROJECT}", "--nouse_legacy_sql",
           "--format=json", "--max_rows=100000", sql]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(f"[bq error]\n{p.stderr}\n")
        raise SystemExit(2)
    return json.loads(p.stdout or "[]")


def i(x):
    return int(float(x)) if x not in (None, "") else 0


def f(x):
    return float(x) if x not in (None, "") else 0.0


def pct(num, den, nd=1):
    return round(100 * num / den, nd) if den else 0.0


def ci95(p_hat, n, nd=1):
    """비율 추정의 95% 신뢰구간(%p). p_hat: 0~1, n: 표본수."""
    if n <= 0:
        return (0.0, 0.0)
    se = math.sqrt(p_hat * (1 - p_hat) / n)
    lo = max(0.0, p_hat - 1.96 * se)
    hi = min(1.0, p_hat + 1.96 * se)
    return (round(lo * 100, nd), round(hi * 100, nd))


# region 매핑 CTE (옵션 viewer를 checkout zone으로 제주/내륙에 매핑)
def region_cte(cut, start):
    return f"""
mz AS (
  SELECT member_id, zone_id FROM (
    SELECT member_id, zone_id, ROW_NUMBER() OVER (PARTITION BY member_id ORDER BY COUNT(*) DESC) rn
    FROM `socar-data.app_web_log.socar_app_web_log`
    WHERE event_date_kst BETWEEN "{start}" AND "{cut}"
      AND page_name='checkout_reservation' AND zone_id IS NOT NULL
    GROUP BY member_id, zone_id ) WHERE rn=1 ),
zmap_z AS (
  SELECT SAFE_CAST(zone_id AS INT64) AS zid, ANY_VALUE(region1) AS region1
  FROM `socar-data.soda_store.reservation_v2` WHERE region1 IS NOT NULL GROUP BY zid ),
reg AS (
  SELECT mz.member_id,
    CASE WHEN z.region1='제주특별자치도' THEN '제주'
         WHEN z.region1 IN ('부산광역시','대구광역시') THEN '내륙' ELSE '기타' END AS region
  FROM mz JOIN zmap_z z ON z.zid = SAFE_CAST(mz.zone_id AS INT64) )
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cut", default=None)
    ap.add_argument("--start", default="2026-06-05")
    ap.add_argument("--out", default="data.js")
    a = ap.parse_args()
    # cut 미지정 시 = 이벤트가 적재된 마지막 날 (D+1, 주말 배치 지연 등 자동 대응)
    if a.cut:
        cut = a.cut
    else:
        r = bq("SELECT CAST(MAX(event_date_kst) AS STRING) AS m "
               "FROM `socar-data.app_web_log.socar_app_web_log` "
               "WHERE event_date_kst >= DATE_SUB(CURRENT_DATE('Asia/Seoul'), INTERVAL 7 DAY) "
               "AND (page_name='services_option' OR event_name='custom_option_add_button')")
        cut = (r[0]["m"] if r and r[0]["m"]
               else (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d"))
    start = a.start

    D = {"meta": {"start": start, "cut": cut,
                  "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")}}

    # 1) 지역별 일별 결제
    rows = bq(f"""
WITH addon AS (
  SELECT reservation_id, charge_type, SUM(amount) AS net,
    DATE(MIN(IF(amount>0, created_at, NULL)),"Asia/Seoul") AS fc_dt
  FROM `socar-data.tianjin_replica.charged_info`
  WHERE DATE(created_at,"Asia/Seoul") >= "{start}" AND charge_type IN {ADDON}
  GROUP BY reservation_id, charge_type HAVING net > 0 ),
zmap AS ( SELECT zone_id, ANY_VALUE(region1) AS region1
  FROM `socar-data.soda_store.reservation_v2` WHERE region1 IS NOT NULL GROUP BY zone_id )
SELECT CASE WHEN z.region1='제주특별자치도' THEN '제주' WHEN z.region1='부산광역시' THEN '부산'
            WHEN z.region1='대구광역시' THEN '대구' END AS region,
  a.fc_dt AS dt,
  COUNTIF(charge_type="addon_carseat") AS carseat_cnt,
  SUM(IF(charge_type="addon_carseat", net,0)) AS carseat_rev,
  COUNTIF(charge_type="addon_stroller") AS stroller_cnt,
  SUM(IF(charge_type="addon_stroller", net,0)) AS stroller_rev,
  COUNT(DISTINCT a.reservation_id) AS rsv_cnt, SUM(net) AS total_rev
FROM addon a JOIN `socar-data.tianjin_replica.reservation_info` ri ON ri.id = a.reservation_id
LEFT JOIN zmap z ON z.zone_id = ri.zone_id
WHERE DATE(ri.created_at,"Asia/Seoul") >= "{start}" AND a.fc_dt <= "{cut}"
  AND z.region1 IN ('제주특별자치도','부산광역시','대구광역시')
GROUP BY region, dt ORDER BY region, dt""")

    # 일자 축
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    dN = datetime.strptime(cut, "%Y-%m-%d").date()
    days = [(d0 + timedelta(days=k)).strftime("%Y-%m-%d")
            for k in range((dN - d0).days + 1)]
    inland_open = "2026-06-08"
    idays = [d for d in days if d >= inland_open]

    jeju = {d: {"cs": 0, "cs_rev": 0, "st": 0, "st_rev": 0, "rsv": 0, "rev": 0} for d in days}
    busan = {d: {"cnt": 0, "rev": 0} for d in idays}
    daegu = {d: {"cnt": 0, "rev": 0} for d in idays}
    for r in rows:
        dt = r["dt"]
        if r["region"] == "제주":
            j = jeju[dt]
            j["cs"], j["cs_rev"] = i(r["carseat_cnt"]), i(r["carseat_rev"])
            j["st"], j["st_rev"] = i(r["stroller_cnt"]), i(r["stroller_rev"])
            j["rsv"], j["rev"] = i(r["rsv_cnt"]), i(r["total_rev"])
        elif r["region"] == "부산":
            busan[dt] = {"cnt": i(r["rsv_cnt"]), "rev": i(r["total_rev"])}
        elif r["region"] == "대구":
            daegu[dt] = {"cnt": i(r["rsv_cnt"]), "rev": i(r["total_rev"])}

    jeju_cs = sum(jeju[d]["cs"] for d in days)
    jeju_cs_rev = sum(jeju[d]["cs_rev"] for d in days)
    jeju_st = sum(jeju[d]["st"] for d in days)
    jeju_st_rev = sum(jeju[d]["st_rev"] for d in days)
    jeju_rsv = sum(jeju[d]["rsv"] for d in days)
    jeju_rev = sum(jeju[d]["rev"] for d in days)
    busan_cnt = sum(busan[d]["cnt"] for d in idays)
    busan_rev = sum(busan[d]["rev"] for d in idays)
    daegu_cnt = sum(daegu[d]["cnt"] for d in idays)
    daegu_rev = sum(daegu[d]["rev"] for d in idays)
    inland_rsv = busan_cnt + daegu_cnt
    inland_rev = busan_rev + daegu_rev

    D["jeju"] = {
        "rsv": jeju_rsv, "carseat": jeju_cs, "carseat_rev": jeju_cs_rev,
        "stroller": jeju_st, "stroller_rev": jeju_st_rev, "rev": jeju_rev,
        "products": jeju_cs + jeju_st, "both": jeju_cs + jeju_st - jeju_rsv,
        "daily": [{"dt": d, **jeju[d]} for d in days],
    }
    D["inland"] = {
        "rsv": inland_rsv, "busan": busan_cnt, "busan_rev": busan_rev,
        "daegu": daegu_cnt, "daegu_rev": daegu_rev, "rev": inland_rev,
        "daily": [{"dt": d, "busan": busan[d]["cnt"], "busan_rev": busan[d]["rev"],
                   "daegu": daegu[d]["cnt"], "daegu_rev": daegu[d]["rev"],
                   "rsv": busan[d]["cnt"] + daegu[d]["cnt"],
                   "rev": busan[d]["rev"] + daegu[d]["rev"]} for d in idays],
    }
    D["total"] = {"rsv": jeju_rsv + inland_rsv, "rev": jeju_rev + inland_rev}

    # 2) 전체 퍼널 (PoC 판단용)
    rows = bq(f"""
SELECT CASE
    WHEN page_name='checkout_reservation' AND component_name='custom_option_add_button' THEN 'add'
    WHEN page_name='services_option' AND event_name='view' THEN 'opt'
    WHEN page_name='services_option' AND component_name='cta_button' THEN 'sel'
    WHEN page_name='services_option' AND component_name='product_detail_help' THEN 'help'
    WHEN page_name='checkout_reservation' AND component_name LIKE '%time_alert%' THEN 'alert' END AS step,
  COUNT(DISTINCT member_id) AS uu
FROM `socar-data.app_web_log.socar_app_web_log`
WHERE event_date_kst BETWEEN "{start}" AND "{cut}"
  AND ((page_name='checkout_reservation' AND (component_name='custom_option_add_button' OR component_name LIKE '%time_alert%'))
    OR (page_name='services_option' AND (event_name='view' OR component_name IN ('cta_button','product_detail_help'))))
GROUP BY step""")
    fn = {r["step"]: i(r["uu"]) for r in rows if r["step"]}

    # 전체 구매(옵션 본 중 결제) + 취소율
    buy = bq(f"""
WITH paid AS (
  SELECT ri.member_id FROM `socar-data.tianjin_replica.charged_info` ci
  JOIN `socar-data.tianjin_replica.reservation_info` ri ON ri.id=ci.reservation_id
  WHERE ci.charge_type IN {ADDON} AND DATE(ci.created_at,"Asia/Seoul")>="{start}"
  GROUP BY ri.member_id HAVING SUM(ci.amount)>0 ),
opt AS (
  SELECT DISTINCT member_id FROM `socar-data.app_web_log.socar_app_web_log`
  WHERE event_date_kst BETWEEN "{start}" AND "{cut}"
    AND page_name='services_option' AND event_name='view' )
SELECT COUNT(*) AS opt_n, COUNTIF(p.member_id IS NOT NULL) AS buyers
FROM opt o LEFT JOIN paid p ON p.member_id=o.member_id""")[0]
    opt_n, buyers = i(buy["opt_n"]), i(buy["buyers"])
    lo, hi = ci95(buyers / opt_n if opt_n else 0, opt_n)

    cancel = bq(f"""
WITH r AS (
  SELECT reservation_id, SUM(amount) AS net,
    TIMESTAMP_DIFF(MAX(IF(amount<0,created_at,NULL)), MIN(IF(amount>0,created_at,NULL)), MINUTE) AS diff_min
  FROM `socar-data.tianjin_replica.charged_info`
  WHERE DATE(created_at,"Asia/Seoul") >= "{start}" AND charge_type IN {ADDON}
  GROUP BY reservation_id )
SELECT COUNTIF(net>0) AS kept, COUNTIF(net=0) AS cancelled,
  COUNTIF(net=0 AND diff_min<=3) AS rollback,
  COUNTIF(net=0 AND diff_min>3) AS real_cancel
FROM r""")[0]
    kept, real_cancel = i(cancel["kept"]), i(cancel["real_cancel"])
    cancel_pct = pct(real_cancel, kept + real_cancel)

    # 월 환산 순매출 (제주+내륙, 각 오픈일 기준 일평균 × 30)
    jdays = (dN - d0).days + 1
    idays_n = (dN - datetime.strptime(inland_open, "%Y-%m-%d").date()).days + 1
    monthly = round((jeju_rev / jdays + inland_rev / max(idays_n, 1)) * 30 / 10000)

    D["poc"] = {
        "buy_rate": pct(buyers, opt_n), "buy_ci": [lo, hi],
        "opt_n": opt_n, "buyers": buyers,
        "block_rate": pct(fn.get("alert", 0), fn.get("add", 1)),
        "cancel_pct": cancel_pct, "kept": kept, "real_cancel": real_cancel,
        "rollback": i(cancel["rollback"]), "cancelled": i(cancel["cancelled"]),
        "monthly_rev_manwon": monthly,
    }

    # 3) 지역별 퍼널 + 전환 + 도움말
    rows = bq(f"""
WITH {region_cte(cut, start)},
ev AS (
  SELECT member_id,
    MAX(IF(page_name='checkout_reservation' AND component_name='custom_option_add_button',1,0)) AS add_c,
    MAX(IF(page_name='services_option' AND event_name='view',1,0)) AS opt,
    MAX(IF(page_name='services_option' AND event_name='click' AND component_name='cta_button',1,0)) AS sel,
    MAX(IF(page_name='services_option' AND event_name='click' AND component_name='product_detail_help',1,0)) AS help,
    MAX(IF(page_name='checkout_reservation' AND component_name LIKE '%time_alert%',1,0)) AS alert
  FROM `socar-data.app_web_log.socar_app_web_log`
  WHERE event_date_kst BETWEEN "{start}" AND "{cut}" AND page_name IN ('checkout_reservation','services_option')
  GROUP BY member_id ),
paid AS (
  SELECT ri.member_id FROM `socar-data.tianjin_replica.charged_info` ci
  JOIN `socar-data.tianjin_replica.reservation_info` ri ON ri.id=ci.reservation_id
  WHERE ci.charge_type IN {ADDON} AND DATE(ci.created_at,"Asia/Seoul")>="{start}"
  GROUP BY ri.member_id HAVING SUM(ci.amount)>0 )
SELECT r.region, SUM(e.add_c) add_uu, SUM(e.opt) opt_uu, SUM(e.sel) sel_uu, SUM(e.help) help_uu, SUM(e.alert) alert_uu,
  COUNTIF(e.opt=1 AND p.member_id IS NOT NULL) buyers,
  COUNTIF(e.sel=1 AND p.member_id IS NOT NULL) sel_paid,
  COUNTIF(e.help=1 AND e.opt=1) help_enter,
  COUNTIF(e.help=1 AND e.opt=1 AND e.sel=1) help_sel,
  COUNTIF(e.help=0 AND e.opt=1) nohelp_enter,
  COUNTIF(e.help=0 AND e.opt=1 AND e.sel=1) nohelp_sel
FROM ev e JOIN r r ON r.member_id=e.member_id LEFT JOIN paid p ON p.member_id=e.member_id
GROUP BY r.region""".replace("JOIN r r", "JOIN reg r"))
    rf = {r["region"]: r for r in rows}

    # 4) 카시트/유모차 선택완료율 (지역별)
    rows = bq(f"""
WITH {region_cte(cut, start)},
ev AS (
  SELECT member_id,
    MAX(IF(event_name='view' AND REGEXP_EXTRACT(form_data,r'pk[\\\\":]+([a-z])')='c',1,0)) AS vc,
    MAX(IF(event_name='click' AND component_name='cta_button' AND REGEXP_EXTRACT(form_data,r'pk[\\\\":]+([a-z])')='c',1,0)) AS sc,
    MAX(IF(event_name='view' AND REGEXP_EXTRACT(form_data,r'pk[\\\\":]+([a-z])')='s',1,0)) AS vs,
    MAX(IF(event_name='click' AND component_name='cta_button' AND REGEXP_EXTRACT(form_data,r'pk[\\\\":]+([a-z])')='s',1,0)) AS ss
  FROM `socar-data.app_web_log.socar_app_web_log`
  WHERE event_date_kst BETWEEN "{start}" AND "{cut}" AND page_name='services_option'
  GROUP BY member_id )
SELECT r.region, COUNTIF(e.vc=1) ce, COUNTIF(e.vc=1 AND e.sc=1) cs2,
  COUNTIF(e.vs=1) se, COUNTIF(e.vs=1 AND e.ss=1) ss2
FROM ev e JOIN reg r ON r.member_id=e.member_id GROUP BY r.region""")
    pk = {r["region"]: r for r in rows}

    def region_block(name):
        g = rf.get(name, {})
        add, opt, sel = i(g.get("add_uu")), i(g.get("opt_uu")), i(g.get("sel_uu"))
        bys, selp = i(g.get("buyers")), i(g.get("sel_paid"))
        he, hs = i(g.get("help_enter")), i(g.get("help_sel"))
        ne, ns = i(g.get("nohelp_enter")), i(g.get("nohelp_sel"))
        p = pk.get(name, {})
        ce, cs2 = i(p.get("ce")), i(p.get("cs2"))
        se, ss2 = i(p.get("se")), i(p.get("ss2"))
        return {
            "add": add, "opt": opt, "sel": sel, "help": i(g.get("help_uu")),
            "alert": i(g.get("alert_uu")), "buyers": bys,
            "buy_rate": pct(bys, opt), "sel_rate": pct(sel, opt),
            "pay_conv": pct(selp, sel), "block_rate": pct(i(g.get("alert_uu")), add),
            "carseat_sel": pct(cs2, ce), "carseat_enter": ce, "carseat_submit": cs2,
            "stroller_sel": pct(ss2, se), "stroller_enter": se, "stroller_submit": ss2,
            "help_rate": pct(hs, he), "nohelp_rate": pct(ns, ne),
        }

    D["jeju"].update({"funnel": region_block("제주")})
    D["inland"].update({"funnel": region_block("내륙")})

    # 5) 프로파일 (전체)
    pr = bq(f"""
WITH addon AS (
  SELECT reservation_id, DATE(MIN(IF(amount>0,created_at,NULL)),"Asia/Seoul") AS fc_dt
  FROM `socar-data.tianjin_replica.charged_info`
  WHERE DATE(created_at,"Asia/Seoul") >= "{start}" AND charge_type IN {ADDON}
  GROUP BY reservation_id HAVING SUM(amount) > 0 ),
base AS (
  SELECT TIMESTAMP_DIFF(ri.start_at, ri.created_at, HOUR)/24.0 AS lead_days,
    DATE_DIFF(DATE(ri.end_at,"Asia/Seoul"), DATE(ri.start_at,"Asia/Seoul"), DAY) AS nights, ri.age
  FROM addon a JOIN `socar-data.tianjin_replica.reservation_info` ri ON ri.id = a.reservation_id
  WHERE a.fc_dt <= "{cut}" AND DATE(ri.created_at,"Asia/Seoul") >= "{start}" )
SELECT COUNT(*) n,
  ROUND(APPROX_QUANTILES(lead_days,2)[OFFSET(1)],0) lead_med, ROUND(AVG(lead_days),1) lead_avg,
  COUNTIF(lead_days<1) l0, COUNTIF(lead_days>=1 AND lead_days<3) l1, COUNTIF(lead_days>=3 AND lead_days<7) l2,
  COUNTIF(lead_days>=7 AND lead_days<14) l3, COUNTIF(lead_days>=14) l4,
  ROUND(AVG(nights),1) navg, COUNTIF(nights<=1) n1, COUNTIF(nights=2) n2, COUNTIF(nights=3) n3, COUNTIF(nights>=4) n4,
  ROUND(AVG(age),1) aavg, COUNTIF(age>=20 AND age<30) a2, COUNTIF(age>=30 AND age<40) a3,
  COUNTIF(age>=40 AND age<50) a4, COUNTIF(age>=50) a5
FROM base""")[0]
    n = i(pr["n"])
    D["profile"] = {
        "n": n, "lead_med": i(pr["lead_med"]), "lead_avg": f(pr["lead_avg"]),
        "lead": [i(pr["l0"]), i(pr["l1"]), i(pr["l2"]), i(pr["l3"]), i(pr["l4"])],
        "lead_2wk_pct": pct(i(pr["l4"]), n), "nights_avg": f(pr["navg"]),
        "nights": [i(pr["n1"]), i(pr["n2"]), i(pr["n3"]), i(pr["n4"])],
        "age_avg": f(pr["aavg"]),
        "age": [i(pr["a2"]), i(pr["a3"]), i(pr["a4"]), i(pr["a5"])],
        "age_3040_pct": pct(i(pr["a3"]) + i(pr["a4"]), n),
    }

    # 6) 일별 옵션 진입 이벤트 UU
    rows = bq(f"""
SELECT event_date_kst AS dt,
  COUNT(DISTINCT IF(page_name='checkout_reservation' AND component_name='custom_option_add_button',member_id,NULL)) AS add_uu,
  COUNT(DISTINCT IF(page_name='services_option' AND event_name='view',member_id,NULL)) AS optview_uu,
  COUNT(DISTINCT IF(page_name='services_option' AND event_name='click' AND component_name IN ('cta_button','product_detail_help'),member_id,NULL)) AS optclick_uu
FROM `socar-data.app_web_log.socar_app_web_log`
WHERE event_date_kst BETWEEN "{start}" AND "{cut}" AND page_name IN ('checkout_reservation','services_option')
GROUP BY dt ORDER BY dt""")
    ev_by = {r["dt"]: r for r in rows}
    D["daily_events"] = [{"dt": d, "add": i(ev_by.get(d, {}).get("add_uu")),
                          "optview": i(ev_by.get(d, {}).get("optview_uu")),
                          "optclick": i(ev_by.get(d, {}).get("optclick_uu"))} for d in days]

    # 7) 마이그레이션 (시트 정답지 272 고정, 인앱 적재 3)
    D["migration"] = {"total": 272, "loaded": 3}

    with open(a.out, "w") as fp:
        if a.out.endswith(".js"):
            fp.write("window.DASH = " + json.dumps(D, ensure_ascii=False) + ";\n")
        else:
            json.dump(D, fp, ensure_ascii=False, indent=2)
    print(f"wrote {a.out}  (cut={cut})  제주 {jeju_rsv}예약/{jeju_rev:,}원  내륙 {inland_rsv}/{inland_rev:,}  전체구매율 {D['poc']['buy_rate']}%")


if __name__ == "__main__":
    main()
