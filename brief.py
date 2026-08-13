#!/usr/bin/env python3
"""
포트폴리오 통합 브리핑 -> 카카오톡 '나에게 보내기'
실행 시각: 매일 14:00 KST (평일)

계좌 잔고 + 리밸런싱 분석(rebalance.py) 결과를 하나의 메시지로 정리해서 보냅니다.
평소에는 "요약 + 보유 종목 + 리밸런싱" 정도만 오고, 당일 급등락(±5%)이나
목표비중 이탈이 큰 항목(±5%p)이 있을 때만 <주의 필요> 섹션이 위쪽에 추가로 붙습니다.

필요 환경변수 (GitHub Secrets):
  KIS_APP_KEY / KIS_APP_SECRET                 한국투자증권 오픈API
  KIS_ACCOUNT_NO_1 / KIS_ACCOUNT_PRDT_CD_1 ...  rebalance.py 와 동일한 계좌 등록 방식
  KAKAO_REST_API_KEY / KAKAO_REFRESH_TOKEN      카카오 '나에게 보내기'

주의:
  - KAKAO_REFRESH_TOKEN 유효기간은 약 2개월입니다. 만료되면 발송이 조용히 멈추므로,
    GitHub → Settings → Notifications → Actions 실패 알림을 켜두시는 걸 권합니다.
"""

import os
import sys
import json
import datetime
import requests

from rebalance import (
    kis_token,
    load_accounts,
    kis_balance,
    merge_holdings,
    compute_rebalance,
    kis_quote,
    DRIFT_ALERT_PP,
)

KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
KAKAO_TEXT_LIMIT = 190  # 카카오 text 템플릿 상한 200자, 여유 두고 190

SURGE_PCT = 5.0  # 당일 등락률 경고 임계치


# ---------------------------------------------------------------- 메시지 생성
def build_message():
    token = kis_token()
    accounts = load_accounts()

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    lines = [f"[포트폴리오 브리핑] {now:%m/%d %H:%M}"]

    if not accounts:
        lines.append("")
        lines.append("등록된 계좌가 없습니다. KIS_ACCOUNT_NO_1 / KIS_ACCOUNT_PRDT_CD_1 을 확인해주세요.")
        return "\n".join(lines)

    all_holdings = []
    grand_total = 0.0
    for acc in accounts:
        holdings, total = kis_balance(token, acc["cano"], acc["prdt_cd"])
        all_holdings.extend(holdings)
        grand_total += total

    if not all_holdings or grand_total <= 0:
        lines.append("")
        lines.append("잔고 조회 실패 또는 보유 없음")
        return "\n".join(lines)

    combined = merge_holdings(all_holdings)

    # 당일 등락률 (보유 종목만 조회)
    today_rate = {}
    surge_alerts = []
    for h in combined:
        q = kis_quote(token, h["code"])
        if not q:
            continue
        today_rate[h["code"]] = q["rate"]
        if abs(q["rate"]) >= SURGE_PCT:
            sign = "+" if q["rate"] > 0 else ""
            surge_alerts.append(f"{h['name']} {sign}{q['rate']:.1f}%")

    rebal = compute_rebalance(combined, grand_total)
    drift_alerts = []
    for r in rebal:
        if abs(r["gap_pp"]) >= DRIFT_ALERT_PP:
            verb = "매수검토" if r["action_krw"] > 0 else "매도검토"
            drift_alerts.append(f"{r['group']} {verb} 약 {abs(r['action_krw']):,.0f}원")

    # <요약>
    lines.append("")
    lines.append("<요약>")
    lines.append(f"계좌 {len(accounts)}개 총평가금액 {grand_total:,.0f}원")
    lines.append("리밸런싱 상태: " + ("조정 필요" if drift_alerts else "양호"))

    # <주의 필요> (알림 있을 때만)
    if surge_alerts or drift_alerts:
        lines.append("")
        lines.append("<주의 필요>")
        for a in surge_alerts:
            lines.append(f"급등락 {a}")
        for a in drift_alerts:
            lines.append(f"비중 이탈 {a}")

    # <보유 종목>
    lines.append("")
    lines.append("<보유 종목>")
    for h in sorted(combined, key=lambda x: -x["eval_amt"]):
        rate = today_rate.get(h["code"])
        if rate is None:
            rate_str = ""
        else:
            sign = "+" if rate > 0 else ""
            rate_str = f", 오늘{sign}{rate:.2f}%"
        lines.append(f"{h['name']} {h['eval_amt']:,.0f}원 (누적{h['pnl_pct']:+.1f}%{rate_str})")

    # <리밸런싱>
    lines.append("")
    lines.append("<리밸런싱 (목표비중 대비)>")
    for r in rebal:
        mark = " !" if abs(r["gap_pp"]) >= DRIFT_ALERT_PP else ""
        lines.append(
            f"{r['group']} {r['actual_pct']:.1f}% / 목표{r['target_pct']:.0f}% "
            f"({r['gap_pp']:+.1f}p){mark}"
        )

    lines.append("")
    lines.append("※ 참고용. 투자자문 아님")
    return "\n".join(lines)


# ---------------------------------------------------------------- 카카오
def kakao_access_token():
    r = requests.post(
        KAKAO_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": os.environ["KAKAO_REST_API_KEY"],
            "refresh_token": os.environ["KAKAO_REFRESH_TOKEN"],
        },
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    if "refresh_token" in body:
        # 리프레시 토큰이 갱신된 경우 로그로 남김 (Secrets 수동 갱신 필요)
        print("::warning::KAKAO_REFRESH_TOKEN 이 갱신되었습니다. Secrets 업데이트 필요")
    return body["access_token"]


def chunk(text, limit=KAKAO_TEXT_LIMIT):
    """줄 단위로 limit 이하 조각으로 분할."""
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            if buf:
                out.append(buf)
            buf = line[:limit]
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


def kakao_send(access_token, text):
    parts = chunk(text)
    for i, part in enumerate(parts, 1):
        tag = f"({i}/{len(parts)})\n" if len(parts) > 1 else ""
        payload = {
            "object_type": "text",
            "text": tag + part,
            "link": {"web_url": "https://m.stock.naver.com"},
        }
        r = requests.post(
            KAKAO_MEMO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            data={"template_object": json.dumps(payload, ensure_ascii=False)},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[error] kakao send: {r.status_code} {r.text}", file=sys.stderr)
            r.raise_for_status()
        print(f"[ok] sent {i}/{len(parts)}")


# ---------------------------------------------------------------- main
def main():
    msg = build_message()
    print(msg)
    if os.environ.get("DRY_RUN") == "1":
        print("[dry-run] 발송 생략")
        return
    kakao_send(kakao_access_token(), msg)


if __name__ == "__main__":
    main()
