#!/usr/bin/env python3
"""
포트폴리오 장중 브리핑 -> 텔레그램 발송
실행 시각: 매일 14:30 KST (평일)

필요 환경변수 (GitHub Secrets):
  KIS_APP_KEY          한국투자증권 오픈API App Key
  KIS_APP_SECRET       한국투자증권 오픈API App Secret
  TELEGRAM_BOT_TOKEN   텔레그램 봇 토큰 (BotFather 에서 발급)
  TELEGRAM_CHAT_ID     메시지를 받을 내 Chat ID
"""

import os
import sys
import datetime
import requests

KIS_BASE = "https://openapi.koreainvestment.com:9443"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

TELEGRAM_TEXT_LIMIT = 3500  # 텔레그램 상한 4096자, 여유 두고 3500

# ---------------------------------------------------------------- 보유 종목
STOCKS = [
    ("005930", "삼성전자"),
    ("055550", "신한지주"),
    ("103590", "일진전기"),
    ("001440", "대한전선"),
]

ETFS = [
    ("133690", "TIGER 미국나스닥100"),
    ("379800", "KODEX 미국S&P500"),
    ("441640", "KODEX 미국배당커버드콜"),
    ("396500", "TIGER 반도체TOP10"),
    ("237350", "KODEX 코스피100"),
    ("498400", "KODEX 200타겟위클리CC"),
    ("472150", "TIGER 배당커버드콜"),
    ("0183J0", "TIGER 미국우주테크"),
]

# 반도체TOP10 지수 규칙: 상위 2종목 각 25% 고정
DRIFT_WATCH_ETF = "396500"
DRIFT_CAP = 25.0
DRIFT_ALERT_PP = 5.0   # 캡 대비 이만큼 벗어나면 경고

SURGE_PCT = 5.0        # 등락률 경고 임계치
PREMIUM_ALERT = 1.0    # 괴리율 경고 임계치 (%)


# ---------------------------------------------------------------- KIS
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


def _kis_headers(token, tr_id):
    return {
        "authorization": f"Bearer {token}",
        "appkey": os.environ["KIS_APP_KEY"],
        "appsecret": os.environ["KIS_APP_SECRET"],
        "tr_id": tr_id,
        "content-type": "application/json; charset=utf-8",
    }


def kis_quote(token, code):
    """주식/ETF 공통 현재가. 실패 시 None."""
    try:
        r = requests.get(
            f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=_kis_headers(token, "FHKST01010100"),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json().get("output") or {}
        if not d.get("stck_prpr"):
            return None
        return {
            "price": int(d["stck_prpr"]),
            "rate": float(d.get("prdy_ctrt", 0)),
        }
    except Exception as e:
        print(f"[warn] quote {code}: {e}", file=sys.stderr)
        return None


def kis_etf_nav(token, code):
    """ETF NAV/괴리율. 엔드포인트 미지원 시 None으로 조용히 넘어감."""
    try:
        r = requests.get(
            f"{KIS_BASE}/uapi/etfetn/v1/quotations/inquire-price",
            headers=_kis_headers(token, "FHPST02400000"),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json().get("output") or {}
        nav = float(d.get("nav") or 0)
        prem = d.get("dprt")
        return {
            "nav": nav,
            "premium": float(prem) if prem not in (None, "") else None,
        }
    except Exception as e:
        print(f"[warn] nav {code}: {e}", file=sys.stderr)
        return None


def kis_etf_holdings(token, code, top=3):
    """ETF 구성종목 비중 상위 N개."""
    try:
        r = requests.get(
            f"{KIS_BASE}/uapi/etfetn/v1/quotations/inquire-component-stock-price",
            headers=_kis_headers(token, "FHKST121600C0"),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json().get("output2") or []
        out = []
        for row in rows[:top]:
            out.append({
                "name": row.get("hts_kor_isnm", "?"),
                "weight": float(row.get("etf_cnfg_issu_rlim") or 0),
            })
        return out
    except Exception as e:
        print(f"[warn] holdings {code}: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------- 메시지
def sign(rate):
    return "+" if rate > 0 else ""


def build_message(token):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    lines = [f"[포트폴리오 장중] {now:%m/%d %H:%M}"]

    alerts = []

    lines.append("")
    lines.append("<개별주>")
    for code, name in STOCKS:
        q = kis_quote(token, code)
        if not q:
            lines.append(f"{name} 조회실패")
            continue
        mark = " !" if abs(q["rate"]) >= SURGE_PCT else ""
        lines.append(f"{name} {q['price']:,} ({sign(q['rate'])}{q['rate']:.2f}%){mark}")
        if abs(q["rate"]) >= SURGE_PCT:
            alerts.append(f"{name} {sign(q['rate'])}{q['rate']:.1f}%")

    lines.append("")
    lines.append("<ETF>")
    for code, name in ETFS:
        q = kis_quote(token, code)
        if not q:
            lines.append(f"{name} 조회실패")
            continue
        line = f"{name} {q['price']:,} ({sign(q['rate'])}{q['rate']:.2f}%)"
        nav = kis_etf_nav(token, code)
        if nav and nav.get("premium") is not None:
            if abs(nav["premium"]) >= PREMIUM_ALERT:
                line += f" 괴리{nav['premium']:+.2f}% !"
                alerts.append(f"{name} 괴리율 {nav['premium']:+.2f}%")
        lines.append(line)

    # 반도체TOP10 비중 드리프트
    hold = kis_etf_holdings(token, DRIFT_WATCH_ETF, top=3)
    if hold:
        lines.append("")
        lines.append("<반도체TOP10 비중/규칙25%>")
        for h in hold:
            gap = h["weight"] - DRIFT_CAP
            lines.append(f"{h['name']} {h['weight']:.1f}% ({gap:+.1f}p)")
        top2 = hold[:2]
        if len(hold) >= 3:
            margin = top2[1]["weight"] - hold[2]["weight"]
            lines.append(f"2위-3위 격차 {margin:.1f}p")
            if margin < 2.0:
                alerts.append(f"상위2종목 순위 역전 임박 (격차 {margin:.1f}p)")

    if alerts:
        lines.append("")
        lines.append("[체크] " + " / ".join(alerts))

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
    token = kis_token()
    msg = build_message(token)
    print(msg)
    if os.environ.get("DRY_RUN") == "1":
        print("[dry-run] 발송 생략")
        return
    telegram_send(msg)


if __name__ == "__main__":
    main()
