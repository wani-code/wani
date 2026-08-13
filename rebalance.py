#!/usr/bin/env python3
"""
계좌 잔고 조회 + 현재가 + 리밸런싱 분석 (읽기 전용, 매매 실행 없음)
여러 계좌를 동시에 등록해서 "계좌별 보유 현황" + "전체 계좌 합산 리밸런싱"을 함께 볼 수 있습니다.

기존 brief.py 와 함께 두고 실행하거나, brief.py 안에 함수만 옮겨 합칠 수 있습니다.
이 파일 단독으로는 "잔고 조회 -> 목표비중 대비 이탈 계산 -> 텍스트 생성"까지만 하고,
텔레그램 발송은 brief.py 의 telegram_send() 를 재사용하면 됩니다.

필요 환경변수 (기존 KIS_APP_KEY / KIS_APP_SECRET 에 추가):
  계좌 1개당 아래 2개를 번호를 붙여서 등록합니다. (1번부터 시작, 끊기지 않게 순서대로)
    KIS_ACCOUNT_NO_1       계좌번호 8자리 (예: 12345678)
    KIS_ACCOUNT_PRDT_CD_1  계좌상품코드 2자리 (예: 01) - 계좌번호 뒤 2자리
    KIS_ACCOUNT_LABEL_1    (선택) 계좌 표시 이름 (예: 본인, 배우자, 연금) - 안 넣으면 "계좌1"
  계좌가 더 있으면 _2, _3, _4 ... 번호를 이어서 등록하면 됩니다.
  번호가 하나라도 비어 있으면 그 지점에서 계좌 인식을 멈추므로, 번호를 건너뛰지 마세요.
  (1번 계좌는 번호 없는 예전 이름 KIS_ACCOUNT_NO / KIS_ACCOUNT_PRDT_CD / KIS_ACCOUNT_LABEL 도 인식합니다.)

주의:
  - 이 스크립트는 "국내주식 잔고조회" API 를 씁니다. KIS Developers에서
    실전투자 App Key 발급 시 기본 포함되는 조회성 TR이라 별도 자동매매 동의는
    필요 없습니다. (매수/매도 주문 API는 이 파일에 없습니다.)
  - 여러 계좌라도 App Key/App Secret 은 보통 1개(같은 한투 로그인)를 공유해서 씁니다.
  - 계좌번호는 Secrets 로만 관리하세요. 코드/로그에 절대 남기지 않습니다.
"""

import os
import sys
import requests

KIS_BASE = "https://openapi.koreainvestment.com:9443"

# ---------------------------------------------------------------- 목표 비중
# "그룹명": (목표비중 %, [종목코드...])
# 종목코드가 여러 개면 그룹 내 비중 합계로 목표비중을 관리합니다.
# 목표비중은 "전체 계좌를 합산한 포트폴리오" 기준입니다.
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


# ---------------------------------------------------------------- 계좌 목록 로드
def load_accounts():
    """
    KIS_ACCOUNT_NO_1 / KIS_ACCOUNT_PRDT_CD_1, _2, _3 ... 순서대로 환경변수를 읽어
    등록된 계좌 목록을 만듭니다. 번호가 끊기면(둘 중 하나라도 없으면) 그 지점에서 멈춥니다.

    1번 계좌는 번호가 붙지 않은 예전 이름(KIS_ACCOUNT_NO / KIS_ACCOUNT_PRDT_CD /
    KIS_ACCOUNT_LABEL)도 그대로 인식합니다. KIS_ACCOUNT_NO_1 이 따로 등록되어 있으면
    그쪽을 우선 사용합니다.
    """
    accounts = []

    no_1 = os.environ.get("KIS_ACCOUNT_NO_1") or os.environ.get("KIS_ACCOUNT_NO")
    prdt_1 = os.environ.get("KIS_ACCOUNT_PRDT_CD_1") or os.environ.get("KIS_ACCOUNT_PRDT_CD")
    if no_1 and prdt_1:
        label_1 = (
            os.environ.get("KIS_ACCOUNT_LABEL_1")
            or os.environ.get("KIS_ACCOUNT_LABEL")
            or "계좌1"
        )
        accounts.append({"cano": no_1.strip(), "prdt_cd": prdt_1.strip(), "label": label_1})

        i = 2
        while True:
            cano = os.environ.get(f"KIS_ACCOUNT_NO_{i}")
            prdt_cd = os.environ.get(f"KIS_ACCOUNT_PRDT_CD_{i}")
            if not cano or not prdt_cd:
                break
            label = os.environ.get(f"KIS_ACCOUNT_LABEL_{i}") or f"계좌{i}"
            accounts.append({"cano": cano.strip(), "prdt_cd": prdt_cd.strip(), "label": label})
            i += 1

    return accounts


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


# ---------------------------------------------------------------- 잔고 조회 (계좌 1개)
def kis_balance(token, cano, prdt_cd):
    """
    국내주식 잔고조회 (실전투자 TR: TTTC8434R / 모의투자는 VTTC8434R)
    반환: (holdings, total_eval_amt)
      holdings: [{code, name, qty, avg_price, eval_amt, pnl_pct}, ...]
      total_eval_amt: 계좌 총평가금액
    """
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


# ---------------------------------------------------------------- 여러 계좌 합산
def merge_holdings(all_holdings):
    """
    여러 계좌의 holdings 리스트를 종목코드 기준으로 합산합니다.
    평균단가는 수량 가중평균으로 다시 계산합니다.
    """
    merged = {}
    for h in all_holdings:
        m = merged.setdefault(h["code"], {
            "code": h["code"], "name": h["name"], "qty": 0,
            "cost_amt": 0.0, "eval_amt": 0.0,
        })
        m["qty"] += h["qty"]
        m["cost_amt"] += h["avg_price"] * h["qty"]
        m["eval_amt"] += h["eval_amt"]

    out = []
    for m in merged.values():
        avg_price = (m["cost_amt"] / m["qty"]) if m["qty"] else 0.0
        pnl_pct = ((m["eval_amt"] - m["cost_amt"]) / m["cost_amt"] * 100) if m["cost_amt"] else 0.0
        out.append({
            "code": m["code"],
            "name": m["name"],
            "qty": m["qty"],
            "avg_price": avg_price,
            "eval_amt": m["eval_amt"],
            "pnl_pct": pnl_pct,
        })
    return out


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
    accounts = load_accounts()
    if not accounts:
        return (
            "<리밸런싱>\n등록된 계좌가 없습니다. "
            "KIS_ACCOUNT_NO_1 / KIS_ACCOUNT_PRDT_CD_1 부터 Secrets에 등록해주세요."
        )

    lines = []
    all_holdings = []
    grand_total = 0.0
    any_success = False

    for acc in accounts:
        holdings, total = kis_balance(token, acc["cano"], acc["prdt_cd"])
        lines.append(f"<{acc['label']} 보유 현황>")
        if not holdings or total <= 0:
            lines.append("잔고 조회 실패 또는 보유 없음")
        else:
            for h in sorted(holdings, key=lambda x: -x["eval_amt"]):
                lines.append(
                    f"{h['name']} {h['qty']}주 평가{h['eval_amt']:,.0f} "
                    f"({h['pnl_pct']:+.2f}%)"
                )
            lines.append(f"{acc['label']} 총평가금액 {total:,.0f}")
            all_holdings.extend(holdings)
            grand_total += total
            any_success = True
        lines.append("")

    if not any_success or grand_total <= 0:
        lines.append("<리밸런싱>\n전체 계좌 잔고 조회 실패 또는 보유 없음")
        return "\n".join(lines).rstrip()

    combined = merge_holdings(all_holdings)

    lines.append(f"<전체 계좌 합산 총평가금액> {grand_total:,.0f}")
    lines.append("")
    lines.append(f"<리밸런싱 (목표비중 대비, 계좌 {len(accounts)}개 합산)>")
    alerts = []
    for r in compute_rebalance(combined, grand_total):
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
