#!/usr/bin/env python3
"""
K-뷰티 트래커 ETL — 라이브 데이터 적재 레이어
==================================================
두 축의 데이터를 권위 있는 공공 API에서 끌어와 대시보드가 읽는 JSON으로 변환한다.

  축1 (시장)   : 관세청 수출입무역통계 OpenAPI  ->  market_data.json
  축2 (경쟁사) : 금융감독원 DART OpenAPI         ->  competitor_data.json

API 키 발급 (무료):
  - DART   : https://opendart.fss.or.kr  ->  인증키 신청   (환경변수 DART_API_KEY)
  - 관세청 : https://www.data.go.kr/data/15101636/openapi.do  (환경변수 CUSTOMS_API_KEY)

사용:
  python etl.py --demo        # 네트워크 없이 시드 JSON 생성 (대시보드 시연용)
  python etl.py --market      # 관세청에서 화장품 수출 적재
  python etl.py --competitors # DART에서 경쟁사 재무 적재
  python etl.py --all         # 둘 다

스케줄 권장 (cron):
  시장(월간, 매월 16일)        0 6 16 * *  python etl.py --market
  상장 경쟁사(분기)            0 7 15 2,5,8,11 * python etl.py --competitors
  비상장 경쟁사(연간, 4월)     0 7 1 4 *   python etl.py --competitors
"""

import os
import sys
import json
import argparse
from datetime import datetime

try:
    import requests  # 라이브 모드에서만 필요
except ImportError:
    requests = None

# ── 추적 대상 정의 ───────────────────────────────────────────────
# 화장품 HS 코드 (관세청 10단위, 6단위까지는 국제 공통)
HS_CODES = {
    "3304": "기초·색조 화장품 전체",
    "330499": "기초화장품 (스킨케어 핵심)",
    "330410": "입술화장품",
    "330420": "눈화장품",
    "3401": "비누·클렌징",
    "3303": "향수",
}

# 추적 20개 브랜드 -> 운영 법인 매핑 (브랜드별 매출 적재용).
# corp_code "" 는 resolve_corp_codes()로 1회 채운다. stock_code 있으면 상장(분기 가능).
ENTITIES = [
    {"brand": "Skin1004",         "entity": "크레이버코퍼레이션", "corp_code": "", "stock_code": None, "home": True, "note": "자사 · 구다이 계열"},
    {"brand": "Medicube",         "entity": "에이피알",        "corp_code": "01133217", "stock_code": "278470", "note": "화장품부문 분리"},
    {"brand": "VT Cosmetics",     "entity": "브이티지엠피",     "corp_code": "",         "stock_code": "018290", "note": "화장품부문 분리"},
    {"brand": "COSRX",            "entity": "코스알엑스",       "corp_code": "",         "stock_code": None,     "note": "아모레 자회사·별도 감사보고서"},
    {"brand": "Beauty of Joseon", "entity": "구다이글로벌",     "corp_code": "",         "stock_code": None},
    {"brand": "Tirtir",           "entity": "티르티르",        "corp_code": "",         "stock_code": None},
    {"brand": "Round Lab",        "entity": "서린컴퍼니",       "corp_code": "",         "stock_code": None},
    {"brand": "Skinfood",         "entity": "스킨푸드",        "corp_code": "",         "stock_code": None},
    {"brand": "Numbuzin",         "entity": "비나우",          "corp_code": "",         "stock_code": None, "note": "넘버즈인+퓌"},
    {"brand": "Torriden",         "entity": "토리든",          "corp_code": "",         "stock_code": None},
    {"brand": "Some By Mi",       "entity": "아이아이컴바인드",  "corp_code": "",         "stock_code": None, "note": "탬버린즈 포함"},
    {"brand": "Abib",             "entity": "포컴퍼니",        "corp_code": "",         "stock_code": None},
    {"brand": "Mixsoon",          "entity": "파켓",            "corp_code": "",         "stock_code": None},
    {"brand": "Isntree",          "entity": "이즈앤트리",       "corp_code": "",         "stock_code": None},
    # 보류 — 매출 미확인(운영 법인/외감 여부 확정 후 합류):
    #   I'm From, Purito Seoul, Haruharu Wonder, Axis-Y, Mary & May, iUnik, P.Calm
]

DART_BASE = "https://opendart.fss.or.kr/api"
CUSTOMS_BASE = "https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList"


# ── DART: corp_code 매핑 ────────────────────────────────────────
def resolve_corp_codes(api_key):
    """corpCode.zip(전체 고유번호 파일)을 받아 회사명->corp_code 매핑.
    최초 1회만 실행하면 됨. 여기서는 흐름만 제시."""
    import io, zipfile, xml.etree.ElementTree as ET
    r = requests.get(f"{DART_BASE}/corpCode.xml", params={"crtfc_key": api_key}, timeout=30)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    name_to_code = {}
    for item in root.iter("list"):
        nm = (item.findtext("corp_name") or "").strip()
        name_to_code[nm] = item.findtext("corp_code")
    return name_to_code


# ── DART: 단일회사 주요 재무지표 ────────────────────────────────
def fetch_dart_financials(api_key, corp_code, year, reprt_code="11011"):
    """reprt_code: 11011=사업보고서(연간), 11013=1Q, 11012=반기, 11014=3Q.
    상장사는 분기 보고서, 비상장 외감법인은 연간(11011)만 존재."""
    params = {
        "crtfc_key": api_key, "corp_code": corp_code,
        "bsns_year": str(year), "reprt_code": reprt_code,
        "fs_div": "CFS",  # 연결. 없으면 OFS(별도)로 폴백
    }
    r = requests.get(f"{DART_BASE}/fnlttSinglAcntAll.json", params=params, timeout=20)
    data = r.json()
    if data.get("status") != "000":
        params["fs_div"] = "OFS"
        r = requests.get(f"{DART_BASE}/fnlttSinglAcntAll.json", params=params, timeout=20)
        data = r.json()
    out = {}
    for row in data.get("list", []):
        nm = row.get("account_nm", "")
        amt = row.get("thstrm_amount", "").replace(",", "")
        if nm in ("매출액", "수익(매출액)") and "revenue" not in out:
            out["revenue"] = _to_won(amt)
        elif nm == "영업이익":
            out["operating_profit"] = _to_won(amt)
        elif nm in ("당기순이익", "당기순이익(손실)"):
            out["net_income"] = _to_won(amt)
    return out


def _to_won(s):
    try:
        return round(int(float(s)) / 1e8, 1)  # 억원 단위
    except (ValueError, TypeError):
        return None


# ── 지역별/해외 매출 ─────────────────────────────────────────────
# 공시 실측 해외 비중(검증된 값). 라이브 파싱 실패 시 폴백으로도 사용.
DISCLOSED_OVERSEAS = {
    "Skin1004":  {"ov_pct": 98, "year": 2024, "src": "크레이버 발표(감사보고서)"},
    "COSRX":     {"ov_pct": 80, "year": 2024, "src": "회사 공시 '해외 80%+'"},
    "Medicube":  {"ov_pct": 55, "year": 2024, "src": "APR 사업보고서(전사, 4Q 64%)"},
}

# DART 단일회사 지역별(매출) 데이터는 재무 API가 아닌 '사업보고서 본문'에 있다.
# 상장사: 사업보고서 첨부문서(document.xml)의 '매출 - 지역별/부문별' 표를 파싱.
# 비상장 외감: 감사보고서 주석의 '수출 매출' 항목을 파싱.
# 아래는 사업보고서 원문을 받아 지역 키워드로 금액을 추출하는 실동작 파서.
REGION_KEYS = ["국내", "내수", "수출", "해외", "미국", "북미", "일본", "중국", "유럽", "아시아", "동남아"]


def fetch_report_doc(api_key, corp_code, year, reprt_code="11011"):
    """사업보고서 접수번호(rcept_no)를 찾아 원문(document.xml)을 텍스트로 반환."""
    import re
    lst = requests.get(f"{DART_BASE}/list.json", params={
        "crtfc_key": api_key, "corp_code": corp_code,
        "bgn_de": f"{year+1}0101", "end_de": f"{year+1}1231",
        "pblntf_ty": "A", "page_count": 100,
    }, timeout=20).json()
    rcept = next((it["rcept_no"] for it in lst.get("list", [])
                  if "사업보고서" in it.get("report_nm", "") or "감사보고서" in it.get("report_nm", "")), None)
    if not rcept:
        return None
    doc = requests.get(f"{DART_BASE}/document.xml",
                       params={"crtfc_key": api_key, "rcept_no": rcept}, timeout=30)
    # 첨부 zip 내 xml -> 텍스트만 추출
    import io, zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(doc.content))
        raw = b" ".join(z.read(n) for n in z.namelist()).decode("utf-8", "ignore")
    except zipfile.BadZipFile:
        raw = doc.text
    return re.sub(r"<[^>]+>", " ", raw)


def parse_regional_sales(text):
    """원문 텍스트에서 '지역/수출' 키워드 주변 금액(백만/억원)을 추출.
    표 구조가 회사마다 달라 완전 자동화는 어려우므로, 키워드 매칭 후
    가장 가까운 숫자를 후보로 수집한다(사람 검수 전 1차 자동수집용)."""
    import re
    out = {}
    if not text:
        return out
    for key in REGION_KEYS:
        m = re.search(rf"{key}[^0-9\-]{{0,20}}([0-9,]{{4,}})", text)
        if m:
            try:
                out[key] = int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return out


def overseas_for(api_key, brand, corp_code, year):
    """브랜드의 해외 비중: 실측(파싱) 우선, 실패 시 검증 폴백."""
    parsed = {}
    if corp_code:
        try:
            parsed = parse_regional_sales(fetch_report_doc(api_key, corp_code, year))
        except Exception:
            parsed = {}
    if parsed:
        return {"regional_raw": parsed, "src": "DART 사업보고서 파싱(검수 필요)"}
    if brand in DISCLOSED_OVERSEAS:
        return {**DISCLOSED_OVERSEAS[brand], "src": DISCLOSED_OVERSEAS[brand]["src"] + " (폴백)"}
    return {"ov_pct": None, "src": "미확보"}


def load_competitors(api_key):
    year = datetime.now().year - 1
    results = []
    for c in ENTITIES:
        if not c["corp_code"]:
            results.append({"brand": c["brand"], "entity": c.get("entity") or "확인 필요",
                            "status": "pending", "src": "DART"})
            continue
        fin = fetch_dart_financials(api_key, c["corp_code"], year, "11011")
        ov = overseas_for(api_key, c["brand"], c["corp_code"], year)
        results.append({
            "brand": c["brand"], "entity": c["entity"], "year": year,
            "listed": bool(c["stock_code"]), "note": c.get("note", ""),
            "status": "part" if c.get("note") else "ok",
            "overseas": ov,
            **fin, "src": "DART",
        })
    return results


# ── 관세청: 품목별 국가별 수출입 ────────────────────────────────
def fetch_customs(api_key, hs_code, year):
    """화장품 HS코드의 연간 국가별 수출입 실적."""
    params = {
        "serviceKey": api_key, "strtYymm": f"{year}01", "endYymm": f"{year}12",
        "hsSgn": hs_code, "type": "json",
    }
    r = requests.get(CUSTOMS_BASE, params=params, timeout=20)
    items = r.json().get("response", {}).get("body", {}).get("items", {}).get("item", [])
    by_country = []
    for it in items:
        by_country.append({
            "country": it.get("statKor"),
            "export_usd": int(it.get("expDlr", 0)),
            "import_usd": int(it.get("impDlr", 0)),
        })
    by_country.sort(key=lambda x: x["export_usd"], reverse=True)
    return by_country


def load_market(api_key):
    year = datetime.now().year - 1
    market = {"year": year, "by_hs": {}}
    for hs, label in HS_CODES.items():
        market["by_hs"][hs] = {"label": label, "countries": fetch_customs(api_key, hs, year)}
    return market


# ── 시드(데모) 데이터: 네트워크 없이 대시보드와 동일 값 생성 ──────
# 자사(스킨1004) 발표 기반 — 단일 API로 안 잡히므로 큐레이션 유지
HOME_TREND = [
    {"year": "2022", "rev": 331}, {"year": "2023", "rev": 669},
    {"year": "2024", "rev": 2800}, {"year": "2025E", "rev": 5600, "est": True},
]
HOME_2025 = {"rev": 2820, "op": 820, "west": 1220, "note": "상반기에 2024 연간 매출 조기 달성"}


def demo_market():
    # 대시보드 MARKET 스키마와 1:1 (asOf/kpis/byCountry/fast/byCategory/yoy/production)
    return {
        "asOf": "2025 연간 (MFDS 잠정치)",
        "kpis": [
            {"label": "총 수출액", "value": "$11.43B", "sub": "전년比 +12.3%", "up": True, "src": "MFDS"},
            {"label": "세계 수출 순위", "value": "2위", "sub": "佛 $24.3B · 美 $10.8B", "src": "MFDS"},
            {"label": "수출 대상국", "value": "202개국", "sub": "전년 172개국", "up": True, "src": "MFDS"},
            {"label": "기초화장품 비중", "value": "74.7%", "sub": "$8.54B", "src": "KCII"},
        ],
        "byCountry": [
            {"name": "미국", "value": 2.19, "yoy": 15.1, "tag": "1위"},
            {"name": "중국", "value": 2.01, "yoy": -19.2, "tag": "2위"},
            {"name": "일본", "value": 1.10, "yoy": 5.0, "tag": "3위"},
            {"name": "4–10위 합계", "value": 2.78, "est": True},
            {"name": "그 외 192개국", "value": 3.35, "est": True},
        ],
        "fast": [
            {"name": "폴란드", "detail": "$282M · 9위", "yoy": 115},
            {"name": "UAE", "detail": "8위", "yoy": 70.6},
        ],
        "byCategory": [
            {"name": "기초(스킨케어)", "value": 8.54, "share": "74.7%"},
            {"name": "색조(메이크업)", "value": 1.51, "share": "13.2%"},
            {"name": "클렌징", "value": 0.59, "share": "5.2%"},
            {"name": "기타", "value": 0.73, "share": "6.4%", "est": True},
            {"name": "향수", "value": 0.06, "share": "0.5% · +46.2%"},
        ],
        "yoy": [{"year": "2024", "value": 10.2}, {"year": "2025", "value": 11.43}],
        "production": {
            "brand": [
                {"name": "LG생활건강", "value": 3.92}, {"name": "아모레퍼시픽", "value": 3.03},
                {"name": "애경산업", "value": 0.30},
            ],
            "odm": [
                {"name": "코스맥스", "value": 1.61}, {"name": "콜마", "value": 1.30},
                {"name": "코스메카", "value": 0.35},
            ],
        },
    }


def demo_brands():
    # 매출 확인된 브랜드만(2024, 억원) + 해외비중. ovSrc actual/est. home=자사.
    return [
        {"brand": "COSRX",            "rev": 5898, "revYear": 2024, "yoy": 21.3,  "ov": 80, "ovSrc": "actual", "entity": "코스알엑스 (아모레 자회사)", "listed": "상장(모회사)", "status": "ok"},
        {"brand": "VT Cosmetics",     "rev": 4317, "revYear": 2024, "yoy": 46.1,  "ov": 55, "ovSrc": "est",    "entity": "브이티지엠피",            "listed": "상장 018290", "status": "ok"},
        {"brand": "Medicube",         "rev": 3385, "revYear": 2024, "yoy": 58.0,  "ov": 55, "ovSrc": "actual", "entity": "에이피알",               "listed": "상장 278470", "status": "part"},
        {"brand": "Beauty of Joseon", "rev": 3309, "revYear": 2024, "yoy": 137.1, "ov": 90, "ovSrc": "est",    "entity": "구다이글로벌",            "listed": "비상장", "status": "ok"},
        {"brand": "Skin1004",         "rev": 2800, "revYear": 2024, "yoy": 321.0, "ov": 98, "ovSrc": "actual", "entity": "크레이버코퍼레이션 (구다이)", "listed": "비상장", "status": "ok", "home": True, "op": 770},
        {"brand": "Tirtir",           "rev": 2736, "revYear": 2024, "yoy": 63.4,  "ov": 80, "ovSrc": "est",    "entity": "티르티르 (구다이)",        "listed": "비상장", "status": "ok"},
        {"brand": "Numbuzin",         "rev": 2664, "revYear": 2024, "yoy": 133.6, "ov": 40, "ovSrc": "est",    "entity": "비나우",                "listed": "비상장", "status": "part"},
        {"brand": "Torriden",         "rev": 1860, "revYear": 2024, "yoy": 176.3, "ov": 45, "ovSrc": "est",    "entity": "토리든",                "listed": "비상장", "status": "ok"},
        {"brand": "Some By Mi",       "rev": 1646, "revYear": 2024, "yoy": 40.2,  "ov": 70, "ovSrc": "est",    "entity": "아이아이컴바인드",         "listed": "비상장", "status": "part"},
        {"brand": "Round Lab",        "rev": 1000, "revYear": 2024, "yoy": None,  "ov": 35, "ovSrc": "est",    "entity": "서린컴퍼니 (구다이)",      "listed": "비상장", "status": "part"},
        {"brand": "Abib",             "rev": 846,  "revYear": 2024, "yoy": None,  "ov": 75, "ovSrc": "est",    "entity": "포컴퍼니",               "listed": "비상장", "status": "ok"},
        {"brand": "Skinfood",         "rev": 589,  "revYear": 2023, "yoy": 57.0,  "ov": 40, "ovSrc": "est",    "entity": "스킨푸드 (구다이)",        "listed": "비상장", "status": "part"},
        {"brand": "Mixsoon",          "rev": 152,  "revYear": 2023, "yoy": None,  "ov": 50, "ovSrc": "est",    "entity": "파켓",                  "listed": "비상장", "status": "part"},
    ]


def demo_competitors():
    return {"brands": demo_brands(), "homeTrend": HOME_TREND, "home2025": HOME_2025}


def write(path, obj):
    obj = {**obj, "generatedAt": datetime.now().strftime("%Y-%m-%d")}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print(f"  ✓ {path}  ({len(json.dumps(obj))} bytes)")


def main():
    ap = argparse.ArgumentParser(description="K-뷰티 트래커 데이터 적재 (DART/관세청). 키 없으면 시드로 폴백.")
    ap.add_argument("--demo", action="store_true", help="강제 시드(네트워크 미사용)")
    ap.add_argument("--outdir", default="data", help="JSON 출력 폴더 (기본: data)")
    args = ap.parse_args()

    dart_key = os.environ.get("DART_API_KEY")
    customs_key = os.environ.get("CUSTOMS_API_KEY")
    out = args.outdir

    # ── 시장 ──
    market = demo_market()
    if not args.demo and customs_key and requests is not None:
        try:
            print("[market] 관세청 적재…")
            market = load_market(customs_key)
        except Exception as e:
            print(f"  ! 관세청 적재 실패 → 시드 사용: {e}")
    else:
        print("[market] 시드 사용 (CUSTOMS_API_KEY 없음)" if not args.demo else "[market] 시드(강제)")
    write(os.path.join(out, "market.json"), market)

    # ── 경쟁사 ──
    comp = demo_competitors()
    if not args.demo and dart_key and requests is not None:
        try:
            print("[competitors] DART 적재…")
            brands = load_competitors(dart_key)            # 라이브: 브랜드 배열
            comp = {"brands": brands, "homeTrend": HOME_TREND, "home2025": HOME_2025}
        except Exception as e:
            print(f"  ! DART 적재 실패 → 시드 사용: {e}")
    else:
        print("[competitors] 시드 사용 (DART_API_KEY 없음)" if not args.demo else "[competitors] 시드(강제)")
    write(os.path.join(out, "competitors.json"), comp)
    print("완료.")


if __name__ == "__main__":
    main()
