#!/usr/bin/env python3
"""
포트폴리오 통합 브리핑 -> 텔레그램 발송
실행 시각: 매일 14:00 KST (평일)

계좌 잔고 + 리밸런싱 분석(rebalance.py) 결과를 하나의 메시지로 정리해서 보냅니다.
평소에는 "요약 + 보유 종목 + 리밸런싱" 정도만 오고, 당일 급등락(±5%)이나
목표비중 이탈이 큰 항목(±5%p)이 있을 때만 <주의 필요> 섹션이 위쪽에 추가로 붙습니다.
(예전 버전에 있던 "보유 여부와 상관없는 고정 관심종목 시세", "ETF NAV 괴리율",
"반도체TOP10 내부 비중 규칙"은 정보량을 줄이기 위해 기본 구성에서 뺐습니다.
필요하시면 다시 추가해드릴 수 있습니다.)

필요 환경변수 (GitHub Secrets):
  KIS_APP_KEY / KIS_APP_SECRET                 한국투자증권 오픈API
  KIS_ACCOUNT_NO_1 / KIS_ACCOUNT_PRDT_CD_1 ...  rebalance.py 와 동일한 계좌 등록 방식
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID         텔레그램 발송
"""

import os
import sys
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

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_TEXT_LIMIT = 3500  # 텔레그램 상한 4096자, 여유 두고 3500

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


# ---------------------------------------------------------------- 텔레그램
def chunk(text, limit=TELEGRAM_TEXT_LIMIT):
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


def telegram_send(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = TELEGRAM_API_URL.format(token=token)

    parts = chunk(text)
    for i, part in enumerate(parts, 1):
        tag = f"({i}/{len(parts)})\n" if len(parts) > 1 else ""
        r = requests.post(
            url,
            data={"chat_id": chat_id, "text": tag + part},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[error] telegram send: {r.status_code} {r.text}", file=sys.stderr)
            r.raise_for_status()
        print(f"[ok] sent {i}/{len(parts)}")


# ---------------------------------------------------------------- main
def main():
    msg = build_message()
    print(msg)
    if os.environ.get("DRY_RUN") == "1":
        print("[dry-run] 발송 생략")
        return
    telegram_send(msg)


if __name__ == "__main__":
    main()
