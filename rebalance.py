#!/usr/bin/env python3
"""
계좌 잔고 조회 + 현재가 + 리밸런싱 분석 (읽기 전용, 매매 실행 없음)

기존 brief.py 와 함께 두고 실행하거나, brief.py 안에 함수만 옮겨 합칠 수 있습니다.
이 파일 단독으로는 "잔고 조회 -> 목표비중 대비 이탈 계산 -> 텍스트 생성"까지만 하고,
카카오 발송은 brief.py 의 kakao_send() 를 재사용하면 됩니다.

필요 환경변수 (기존 KIS_APP_KEY / KIS_APP_SECRET 에 추가):
  KIS_ACCOUNT_NO      계좌번호 8자리 (예: 12345678)
  KIS_ACCOUNT_PRDT_CD 계좌상품코드 2자리 (예: 01) - 계좌번호 뒤 2자리

주의:
  - 이 스크립트는 "국내주식 잔고조회" API 를 씁니다. KIS Developers에서
    실전투자 App Key 발급 시 기본 포함되는 조회성 TR이라 별도 자동매매 동의는
    필요 없습니다. (매수/매도 주문 API는 이 파일에 없습니다.)
  - 계좌번호는 Secrets 로만 관리하세요. 코드/로그에 절대 남기지 않습니다.
"""

import os
import sys
import requests

KIS_BASE = "https://openapi.koreainvestment.com:9443"

# ---------------------------------------------------------------- 목표 비중
# "그룹명": (목표비중 %, [종목코드...])
# 종목코드가 여러 개면 그룹 내 비중 합계로 목표비중을 관리합니다.
TARGET_WEIGHTS = {
    "국내개별주": {
        "target_pct": 30.0,
        "codes": ["005930", "055550", "103590", "001440"],  # 삼성전자/신한지주/일진전기/대한전선
    },
    "해외ETF": {
        "target_pct": 45.0,
        "codes": ["133690", "379800", "441640", "0183J0"],  # 나스닥100/S&P500/배당커버드콜/우주테크
    },
    "국내ETF": {
        "target_pct": 25.0,
        "codes": ["396500", "237350", "498400", "472150"],  # 반도체TOP10/코스피100/200위클리CC/배당커버드콜
    },
}

DRIFT_ALERT_PP = 5.0  # 목표비중 대비 이 값(%p) 이상 벗어나면 경고


# ---------------------------------------------------------------- KIS 인증
def kis_token():
    r = requests.post(
        f"{KIS_BASE}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": os.environ["KIS_APP_KEY"],
            "appsecret": os.environ["KIS_APP_SECRET"],
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _headers(token, tr_id):
    return {
        "authorization": f"Bearer {token}",
        "appkey": os.environ["KIS_APP_KEY"],
        "appsecret": os.environ["KIS_APP_SECRET"],
        "tr_id": tr_id,
        "content-type": "application/json; charset=utf-8",
    }


# ---------------------------------------------------------------- 잔고 조회
def kis_balance(token):
    """
    국내주식 잔고조회 (실전투자 TR: TTTC8434R / 모의투자는 VTTC8434R)
    반환: (holdings, total_eval_amt)
      holdings: [{code, name, qty, avg_price, eval_amt, pnl_pct}, ...]
      total_eval_amt: 계좌 총평가금액
    """
    cano = os.environ["KIS_ACCOUNT_NO"]
    prdt_cd = os.environ["KIS_ACCOUNT_PRDT_CD"]

    holdings = []
    total_eval_amt = 0.0
    ctx_fk, ctx_nk = "", ""

    while True:
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": prdt_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": ctx_fk,
            "CTX_AREA_NK100": ctx_nk,
        }
        r = requests.get(
            f"{KIS_BASE}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=_headers(token, "TTTC8434R"),
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()

        for row in body.get("output1", []):
            qty = int(row.get("hldg_qty") or 0)
            if qty <= 0:
                continue
            holdings.append({
                "code": row.get("pdno"),
                "name": row.get("prdt_name"),
                "qty": qty,
                "avg_price": float(row.get("pchs_avg_pric") or 0),
                "eval_amt": float(row.get("evlu_amt") or 0),
                "pnl_pct": float(row.get("evlu_pfls_rt") or 0),
            })

        summary = body.get("output2")
        if summary:
            # output2 는 리스트 또는 dict 로 오는 응답이 있어 방어적으로 처리
            s = summary[0] if isinstance(summary, list) else summary
            total_eval_amt = float(s.get("tot_evlu_amt") or total_eval_amt)

        if body.get("tr_cont") not in ("F", "M"):  # 다음 페이지 없음
            break
        ctx_fk = body.get("ctx_area_fk100", "")
        ctx_nk = body.get("ctx_area_nk100", "")

    return holdings, total_eval_amt


# ---------------------------------------------------------------- 현재가
def kis_quote(token, code):
    try:
        r = requests.get(
            f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=_headers(token, "FHKST01010100"),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json().get("output") or {}
        if not d.get("stck_prpr"):
            return None
        return {"price": int(d["stck_prpr"]), "rate": float(d.get("prdy_ctrt", 0))}
    except Exception as e:
        print(f"[warn] quote {code}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- 리밸런싱 계산
def compute_rebalance(holdings, total_eval_amt):
    """
    그룹별 실제비중 vs 목표비중 비교.
    반환: [{group, target_pct, actual_pct, gap_pp, action_krw}, ...]
    action_krw 는 양수면 '더 사야 할 금액', 음수면 '팔아서 줄여야 할 금액' (참고용)
    """
    by_code = {h["code"]: h for h in holdings}
    results = []
    for group, cfg in TARGET_WEIGHTS.items():
        group_amt = sum(by_code[c]["eval_amt"] for c in cfg["codes"] if c in by_code)
        actual_pct = (group_amt / total_eval_amt * 100) if total_eval_amt else 0.0
        gap_pp = actual_pct - cfg["target_pct"]
        action_krw = -gap_pp / 100 * total_eval_amt  # 음수 gap(부족)이면 +매수금액
        results.append({
            "group": group,
            "target_pct": cfg["target_pct"],
            "actual_pct": actual_pct,
            "gap_pp": gap_pp,
            "action_krw": action_krw,
        })
    return results


# ---------------------------------------------------------------- 메시지 생성
def build_rebalance_section(token):
    holdings, total = kis_balance(token)
    if not holdings or total <= 0:
        return "<리밸런싱>\n잔고 조회 실패 또는 보유 없음"

    lines = ["<보유 현황>"]
    for h in sorted(holdings, key=lambda x: -x["eval_amt"]):
        lines.append(
            f"{h['name']} {h['qty']}주 평가{h['eval_amt']:,.0f} "
            f"({h['pnl_pct']:+.2f}%)"
        )
    lines.append(f"총평가금액 {total:,.0f}")

    lines.append("")
    lines.append("<리밸런싱 (목표비중 대비)>")
    alerts = []
    for r in compute_rebalance(holdings, total):
        mark = " !" if abs(r["gap_pp"]) >= DRIFT_ALERT_PP else ""
        verb = "매수검토" if r["action_krw"] > 0 else "매도검토"
        lines.append(
            f"{r['group']} {r['actual_pct']:.1f}% / 목표{r['target_pct']:.0f}% "
            f"({r['gap_pp']:+.1f}p){mark}"
        )
        if abs(r["gap_pp"]) >= DRIFT_ALERT_PP:
            alerts.append(f"{r['group']} {verb} 약 {abs(r['action_krw']):,.0f}원")

    if alerts:
        lines.append("")
        lines.append("[리밸런싱 체크] " + " / ".join(alerts))

    return "\n".join(lines)


if __name__ == "__main__":
    tok = kis_token()
    print(build_rebalance_section(tok))
