import os
import re
import feedparser
import requests
import hashlib
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from supabase import create_client, Client
import pytz
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

# ── 접근 제어 (선택) ─────────────────────────────────────────
# Render 환경변수에 SITE_PASSWORD 설정 시 비밀번호 보호 활성화
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")

@app.before_request
def check_auth():
    """SITE_PASSWORD 설정 시 로그인 필요"""
    if not SITE_PASSWORD:
        return  # 비밀번호 미설정 시 누구나 접근 가능
    # 정적 파일, 로그인 엔드포인트는 제외
    if request.path.startswith("/static"):
        return
    if request.path == "/login":
        return
    from flask import session
    if session.get("authenticated"):
        return
    # API 요청은 헤더로 인증
    if request.path.startswith("/api"):
        token = request.headers.get("X-Site-Token", "")
        if token == SITE_PASSWORD:
            return
        return jsonify({"error": "unauthorized"}), 401
    # 페이지 요청은 로그인 페이지로 리디렉션
    from flask import redirect, url_for
    return redirect("/login")

# ── 환경변수 ──────────────────────────────────────────────────
SUPABASE_URL        = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY        = os.environ.get("SUPABASE_KEY", "")
NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
SLACK_WEBHOOK_URL   = os.environ.get("SLACK_WEBHOOK_URL", "")   # 1번: 슬랙 알림
NAVER_AD_API_KEY    = os.environ.get("NAVER_AD_API_KEY", "")    # 네이버 검색광고 API키
NAVER_AD_SECRET_KEY = os.environ.get("NAVER_AD_SECRET_KEY", "") # 네이버 검색광고 비밀키
NAVER_AD_CUSTOMER_ID= os.environ.get("NAVER_AD_CUSTOMER_ID","") # 네이버 검색광고 고객ID

KST = pytz.timezone("Asia/Seoul")

# ── 1. 신규 글 슬랙 알림 ─────────────────────────────────────
def send_slack_alert(company_name: str, source_type: str, title: str, url: str):
    """신규 글 감지 시 슬랙 채널에 알림 전송"""
    if not SLACK_WEBHOOK_URL:
        return
    try:
        icon  = "📝" if source_type == "blog" else "☕"
        src   = "블로그" if source_type == "blog" else "카페"
        now   = datetime.now(KST).strftime("%m/%d %H:%M")
        text  = (
            f"{icon} *[{company_name}] 새 {src} 글 감지* — {now}\n"
            f"> {title}\n"
            f"> <{url}|바로 보기>"
        )
        requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": text},
            timeout=8,
        )
    except Exception as e:
        logger.warning(f"slack alert failed: {e}")

# ── Supabase 클라이언트 ───────────────────────────────────────
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── 데모용 기본 데이터 (Supabase 미연결 시) ───────────────────
DEFAULT_COMPANIES = [
    {"id":1,"name":"경쟁사 A","blog_id":"competitor_a","cafe_author":"comp_a_cafe","cafe_url":"","active":True, "cafe_name":""},
    {"id":2,"name":"경쟁사 B","blog_id":"competitor_b","cafe_author":"comp_b_cafe","cafe_url":"","active":True, "cafe_name":""},
    {"id":3,"name":"경쟁사 C","blog_id":"competitor_c","cafe_author":"comp_c_cafe","cafe_url":"","active":True, "cafe_name":""},
    {"id":4,"name":"경쟁사 D","blog_id":"",            "cafe_author":"comp_d_cafe","active":True},
    {"id":5,"name":"경쟁사 E","blog_id":"competitor_e","cafe_author":"comp_e_cafe","active":True},
    {"id":6,"name":"경쟁사 F","blog_id":"competitor_f","cafe_author":"comp_f_cafe","active":True},
]

# ── DB 헬퍼 ──────────────────────────────────────────────────
def init_db():
    if not supabase:
        logger.warning("Supabase 미연결 — 데모 모드")
        return
    try:
        supabase.table("companies").select("id").limit(1).execute()
        logger.info("Supabase 연결 OK")
    except Exception as e:
        logger.error(f"Supabase init error: {e}")

def get_companies():
    if not supabase:
        return DEFAULT_COMPANIES
    try:
        r = supabase.table("companies").select("*").eq("active", True).order("id").execute()
        return r.data if r.data else DEFAULT_COMPANIES
    except Exception as e:
        logger.error(f"get_companies: {e}")
        return DEFAULT_COMPANIES

def scrape_cafe_member_posts(cafe_url: str, limit: int = 30) -> list:
    """카페 멤버 게시글 목록 스크래핑 (제목, 작성일, 조회수)
    cafe_url: https://cafe.naver.com/f-e/cafes/{cafe_id}/members/{member_id}
    """
    if not cafe_url or "members" not in cafe_url:
        return []
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://cafe.naver.com/",
            "Accept-Language": "ko-KR,ko;q=0.9",
        }
        # 페이지 파라미터
        from urllib.parse import urlparse, urlencode
        url = cafe_url if "?" not in cafe_url else cafe_url.split("?")[0]
        params = {"perPage": limit, "page": 1}
        full_url = url + "?" + urlencode(params)
        
        resp = requests.get(full_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"카페 스크래핑 실패: {resp.status_code}")
            return []
        
        import re as _re
        html = resp.text
        posts = []
        
        # 카페 게시글 테이블 파싱
        # article-board-list 또는 게시글 테이블
        rows = _re.findall(
            r'<tr[^>]*class="[^"]*(?:article|board)[^"]*"[^>]*>(.*?)</tr>',
            html, _re.DOTALL
        )
        for row in rows[:limit]:
            title_m = _re.search(r'<a[^>]*class="[^"]*article[^"]*"[^>]*>(.*?)</a>', row, _re.DOTALL)
            date_m  = _re.search(r'<td[^>]*class="[^"]*td_date[^"]*"[^>]*>(.*?)</td>', row, _re.DOTALL)
            view_m  = _re.search(r'<td[^>]*class="[^"]*td_view[^"]*"[^>]*>(.*?)</td>', row, _re.DOTALL)
            link_m  = _re.search(r'href="([^"]*articleid=[0-9]+[^"]*)"', row)
            
            if not title_m:
                continue
            title = _re.sub(r'<[^>]+>', '', title_m.group(1)).strip()
            date  = _re.sub(r'<[^>]+>', '', date_m.group(1)).strip() if date_m else ''
            views = _re.sub(r'[^0-9]', '', view_m.group(1)) if view_m else ''
            link  = 'https://cafe.naver.com' + link_m.group(1) if link_m and link_m.group(1).startswith('/') else (link_m.group(1) if link_m else '')
            
            if title:
                posts.append({
                    'title': title,
                    'url': link,
                    'published_at': date,
                    'views_count': int(views) if views else 0,
                })
        return posts
    except Exception as e:
        logger.warning(f"카페 스크래핑 오류: {e}")
        return []


def get_posts_by_company(company_id: int, source_type: str, limit: int = 10):
    if not supabase:
        return []
    try:
        r = (supabase.table("detected_posts")
             .select("*")
             .eq("company_id", company_id)
             .eq("source_type", source_type)
             .order("detected_at", desc=True)
             .limit(limit)
             .execute())
        posts = r.data or []
        # published_at을 datetime으로 파싱해서 정렬 (timezone 형식 무관하게 정확히)
        from dateutil import parser as _dp
        def _sort_key(p):
            raw = p.get("published_at") or p.get("detected_at") or ""
            if not raw:
                return __import__("datetime").datetime.min.replace(tzinfo=__import__("pytz").utc)
            try:
                dt = _dp.parse(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=__import__("pytz").utc)
                return dt
            except Exception:
                return __import__("datetime").datetime.min.replace(tzinfo=__import__("pytz").utc)
        posts.sort(key=_sort_key, reverse=True)
        return posts
    except Exception as e:
        logger.error(f"get_posts_by_company: {e}")
        return []
def get_cafe_name(cafe_url: str) -> str:
    """카페 URL에서 카페명 추출 (네이버 카페 페이지 파싱)"""
    if not cafe_url:
        return ""
    try:
        import re as _re
        # URL에서 카페 슬러그 추출
        # 패턴1: cafe.naver.com/{slug}/  (일반형)
        # 패턴2: cafe.naver.com/f-e/cafes/{cafeId}/members/{memberId}
        m1 = _re.search(r"cafe\.naver\.com/(?!f-e/)([^/?#]+)", cafe_url)
        if m1:
            slug = m1.group(1)
            cafe_page = requests.get(
                f"https://cafe.naver.com/{slug}",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=8,
                allow_redirects=True,
            )
            title_m = _re.search(r"<title>([^<]+)</title>", cafe_page.text)
            if title_m:
                raw = title_m.group(1).strip()
                # "카페명 : 네이버 카페" 또는 "카페명" 형태
                name = raw.split(":")[0].strip().split("|")[0].strip()
                if name and name.lower() not in ("naver", "네이버"):
                    return name
        # 패턴2: cafeId 방식 → cafeId로 카페 홈 시도
        m2 = _re.search(r"cafes/([0-9]+)", cafe_url)
        if m2:
            cafe_id = m2.group(1)
            cafe_page = requests.get(
                f"https://cafe.naver.com/ca-fe/cafes/{cafe_id}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
                allow_redirects=True,
            )
            title_m = _re.search(r"<title>([^<]+)</title>", cafe_page.text)
            if title_m:
                raw = title_m.group(1).strip()
                name = raw.split(":")[0].strip().split("|")[0].strip()
                if name and name.lower() not in ("naver", "네이버", ""):
                    return name
    except Exception as e:
        logger.warning(f"get_cafe_name error: {e}")
    return ""


def get_daily_counts(company_id: int) -> list:
    """최근 7일 일별 블로그+카페 감지 건수 [{date, label, blog, cafe, total}] """
    from datetime import timedelta
    today = datetime.now(KST).date()
    days  = [(today - timedelta(days=i)) for i in range(6, -1, -1)]  # 6일전 ~ 오늘
    DAY_KO = ["일","월","화","수","목","금","토"]
    result = []
    for d in days:
        month_day = str(d.month) + "/" + str(d.day)
        label = month_day + chr(10) + DAY_KO[d.weekday()]
        blog_c  = 0
        cafe_c  = 0
        if supabase:
            try:
                date_str = d.strftime("%Y-%m-%d")
                # published_at 기준 (실제 발행일) - detected_at은 스캔 시점이라 부정확
                from datetime import timedelta as _td
                next_d = (d + _td(days=1)).strftime("%Y-%m-%d")
                rb = (supabase.table("detected_posts")
                      .select("id", count="exact")
                      .eq("company_id", company_id)
                      .eq("source_type", "blog")
                      .gte("published_at", date_str)
                      .lt("published_at",  next_d)
                      .execute())
                rc = (supabase.table("detected_posts")
                      .select("id", count="exact")
                      .eq("company_id", company_id)
                      .eq("source_type", "cafe")
                      .gte("published_at", date_str)
                      .lt("published_at",  next_d)
                      .execute())
                blog_c = rb.count or 0
                cafe_c = rc.count or 0
            except Exception as e:
                logger.error(f"get_daily_counts: {e}")
        result.append({
            "date":  str(d),
            "label": label,
            "blog":  blog_c,
            "cafe":  cafe_c,
            "total": blog_c + cafe_c,
            "is_today": d == today,
        })
    return result

def get_week_counts(company_id):
    if not supabase:
        return {"blog":0,"cafe":0}
    try:
        from datetime import timedelta
        today=datetime.now(KST)
        week_start=(today-timedelta(days=today.weekday())).strftime("%Y-%m-%d")
        # published_at 기준 (실제 발행일)
        rb=supabase.table("detected_posts").select("id",count="exact").eq("company_id",company_id).eq("source_type","blog").gte("published_at",week_start).execute()
        rc=supabase.table("detected_posts").select("id",count="exact").eq("company_id",company_id).eq("source_type","cafe").gte("published_at",week_start).execute()
        return {"blog":rb.count or 0,"cafe":rc.count or 0}
    except Exception as e:
        logger.error(f"get_week_counts: {e}")
        return {"blog":0,"cafe":0}
def get_today_counts(company_id: int):
    """업체별 오늘 블로그/카페 감지 건수"""
    if not supabase:
        return {"blog": 0, "cafe": 0}
    try:
        today = datetime.now(KST).strftime("%Y-%m-%d")
        rb = (supabase.table("detected_posts").select("id", count="exact")
              .eq("company_id", company_id).eq("source_type", "blog")
              .gte("detected_at", today).execute())
        rc = (supabase.table("detected_posts").select("id", count="exact")
              .eq("company_id", company_id).eq("source_type", "cafe")
              .gte("detected_at", today).execute())
        return {"blog": rb.count or 0, "cafe": rc.count or 0}
    except Exception as e:
        logger.error(f"get_today_counts: {e}")
        return {"blog": 0, "cafe": 0}

def get_stats():
    if not supabase:
        return {"today":0,"total":0,"company_count":len(DEFAULT_COMPANIES)}
    try:
        today   = datetime.now(KST).strftime("%Y-%m-%d")
        r_today = supabase.table("detected_posts").select("id",count="exact").gte("detected_at",today).execute()
        r_total = supabase.table("detected_posts").select("id",count="exact").execute()
        r_comp  = supabase.table("companies").select("id",count="exact").eq("active",True).execute()
        return {
            "today":         r_today.count or 0,
            "total":         r_total.count or 0,
            "company_count": r_comp.count  or 0,
        }
    except Exception as e:
        logger.error(f"get_stats: {e}")
        return {"today":0,"total":0,"company_count":0}

def is_duplicate(post_id: str) -> bool:
    if not supabase:
        return False
    try:
        r = supabase.table("detected_posts").select("id").eq("post_id", post_id).execute()
        return bool(r.data)
    except Exception as e:
        logger.error(f"is_duplicate: {e}")
        return False

def save_post(data: dict):
    if not supabase:
        return
    try:
        supabase.table("detected_posts").insert(data).execute()
    except Exception as e:
        logger.error(f"save_post: {e}")

# ── 네이버 블로그 RSS 체크 ────────────────────────────────────
def parse_rss_date(entry) -> str:
    """feedparser entry에서 KST ISO 날짜 추출"""
    tp = entry.get("published_parsed") or entry.get("updated_parsed")
    if tp:
        try:
            import calendar, time as _time
            ts  = calendar.timegm(tp)
            dt  = datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.utc).astimezone(KST)
            return dt.isoformat()
        except Exception:
            pass
    raw = entry.get("published") or entry.get("updated", "")
    if raw:
        # RFC 2822 ("Mon, 02 Mar 2026 00:00:00 +0900") → ISO 변환
        try:
            from email.utils import parsedate_to_datetime as _ptd
            dt = _ptd(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=pytz.utc)
            return dt.astimezone(KST).isoformat()
        except Exception:
            pass
        # dateutil fallback
        try:
            from dateutil import parser as _dp
            dt = _dp.parse(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=pytz.utc)
            return dt.astimezone(KST).isoformat()
        except Exception:
            pass
        return raw  # 최후 fallback
    return datetime.now(KST).isoformat()

def check_blog(company: dict) -> int:
    blog_id = company.get("blog_id","").strip()
    if not blog_id:
        return 0

    rss_url   = f"https://rss.blog.naver.com/{blog_id}.xml"
    new_count = 0
    try:
        # User-Agent 설정으로 네이버 차단 우회
        resp = requests.get(
            rss_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FeedFetcher/1.0)"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"RSS HTTP {resp.status_code} ({company['name']})")
            return 0
        feed = feedparser.parse(resp.content)
        if not feed.entries:
            logger.warning(f"RSS 글 없음 ({company['name']}): status={getattr(feed,'status','?')}")
        for entry in feed.entries[:10]:
            raw_link = entry.get("link") or entry.get("id", "")
            if not raw_link:
                continue
            post_id  = hashlib.md5(raw_link.encode()).hexdigest()
            if is_duplicate(post_id):
                continue

            title = re.sub(r"<.*?>", "", entry.get("title", "제목 없음")).strip()
            pub   = parse_rss_date(entry)

            save_post({
                "post_id":      post_id,
                "company_id":   company["id"],
                "company_name": company["name"],
                "source_type":  "blog",
                "author_id":    blog_id,
                "title":        title,
                "url":          raw_link,
                "published_at": pub,
                "detected_at":  datetime.now(KST).isoformat(),
            })
            new_count += 1
            logger.info(f"[BLOG 신규] {company['name']}: {title}")
            send_slack_alert(company["name"], "blog", title, raw_link)

    except Exception as e:
        logger.error(f"Blog RSS error ({company['name']}): {e}")

    return new_count

# ── 네이버 카페 글쓴이 추적 ───────────────────────────────────
def check_cafe(company: dict) -> int:
    """네이버 카페 검색 API로 지정 검색어 글 수집
    
    필터링 우선순위:
    1. cafe_home URL의 슬러그 (예: cafe.naver.com/howaboutyou → "howaboutyou")
    2. cafe_url의 숫자 카페 ID (예: cafes/12345678)
    3. 둘 다 없으면 검색어만으로 전체 카페 검색 (비정밀)
    """
    cafe_author = company.get("cafe_author","").strip()
    if not cafe_author or not NAVER_CLIENT_ID:
        return 0

    import re as _re

    # ── 카페 식별자 추출 ──────────────────────────────────────
    cafe_slug = ""
    cafe_id   = ""

    # 1순위: cafe_home에서 슬러그 추출 (가장 정확)
    cafe_home = company.get("cafe_home","").strip()
    if cafe_home:
        m = _re.search(r"cafe\.naver\.com/([^/?#\s]+)", cafe_home)
        if m and m.group(1) not in ("f-e", "ArticleRead.nhn"):
            cafe_slug = m.group(1).lower()

    # 2순위: cafe_url에서 숫자 ID 추출
    cafe_url_raw = company.get("cafe_url","").strip()
    if cafe_url_raw:
        m = _re.search(r"cafes/(\d+)", cafe_url_raw)
        if m:
            cafe_id = m.group(1)

    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    new_count = 0
    try:
        resp = requests.get(
            "https://openapi.naver.com/v1/search/cafearticle.json",
            headers=headers,
            params={"query": cafe_author, "display": 100, "sort": "date"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.error(f"Naver cafe API {resp.status_code} ({company['name']})")
            return 0

        for item in resp.json().get("items", []):
            link = item.get("link","")
            if not link:
                continue

            link_lower = link.lower()

            # ── 카페 필터링 ──────────────────────────────────
            # slug/id 둘 다 없으면 카페 특정 불가 → 저장 안 함
            if not cafe_slug and not cafe_id:
                continue
            if cafe_slug:
                if cafe_slug not in link_lower:
                    if not cafe_id or cafe_id not in link:
                        continue
            elif cafe_id:
                if cafe_id not in link:
                    continue

            post_id = hashlib.md5(link.encode()).hexdigest()
            if is_duplicate(post_id):
                continue

            title    = re.sub(r"<.*?>","", item.get("title","")).strip()
            pub_date = item.get("postdate","")
            if len(pub_date) == 8:
                pub_date = f"{pub_date[:4]}-{pub_date[4:6]}-{pub_date[6:]}T00:00:00+09:00"

            save_post({
                "post_id":      post_id,
                "company_id":   company["id"],
                "company_name": company["name"],
                "source_type":  "cafe",
                "author_id":    cafe_author,
                "title":        title,
                "url":          link,
                "published_at": pub_date,
                "detected_at":  datetime.now(KST).isoformat(),
            })
            new_count += 1
            logger.info(f"[CAFE 신규] {company['name']}: {title[:40]}")
            send_slack_alert(company["name"], "cafe", title, link)

    except Exception as e:
        logger.error(f"Cafe API error ({company['name']}): {e}")

    return new_count



# ── 키워드 DB 헬퍼 ──────────────────────────────────────────
def get_keywords(company_id):
    if not supabase: return []
    try:
        r = supabase.table("keywords").select("*").eq("company_id",company_id).eq("active",True).order("id").execute()
        return r.data or []
    except Exception as e:
        logger.error(f"get_keywords: {e}"); return []

def get_latest_ranking(keyword_id):
    if not supabase: return None
    try:
        r = supabase.table("rankings").select("*").eq("keyword_id",keyword_id).order("checked_at",desc=True).limit(1).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"get_latest_ranking: {e}"); return None


# ── 네이버 검색광고 API: 키워드 월 검색량 ─────────────────────
def get_naver_search_volumes(keywords: list[str]) -> dict:
    """키워드 리스트 → {keyword: monthly_pc+mobile_volume} 딕셔너리 반환"""
    if not NAVER_AD_API_KEY or not keywords:
        return {}
    import hmac as _hmac, hashlib as _hashlib, base64 as _b64, time as _time, json as _json
    try:
        ts      = str(int(_time.time() * 1000))
        method  = "GET"
        uri     = "/keywordstool"
        msg     = "\n".join([ts, method, uri])
        sig     = _b64.b64encode(
            _hmac.new(NAVER_AD_SECRET_KEY.encode(), msg.encode(), _hashlib.sha256).digest()
        ).decode()
        # 최대 5개씩 배치 조회
        result  = {}
        batch   = [keywords[i:i+5] for i in range(0, len(keywords), 5)]
        for chunk in batch[:4]:          # 최대 4배치 = 20개
            params = {"hintKeywords": ",".join(chunk), "showDetail": "1"}
            resp   = requests.get(
                "https://api.naver.com/keywordstool",
                headers={
                    "X-Timestamp":     ts,
                    "X-API-KEY":       NAVER_AD_API_KEY,
                    "X-Customer":      NAVER_AD_CUSTOMER_ID,
                    "X-Signature":     sig,
                    "Content-Type":    "application/json",
                },
                params=params,
                timeout=8,
            )
            if resp.status_code == 200:
                for item in resp.json().get("keywordList", []):
                    kw  = item.get("relKeyword", "")
                    pc  = int(item.get("monthlyPcQcCnt",  0) or 0)
                    mob = int(item.get("monthlyMobileQcCnt", 0) or 0)
                    total = pc + mob
                    # 네이버 표기: "<10" = 실제 미만 표시
                    if isinstance(item.get("monthlyPcQcCnt"), str):
                        pc = 5
                    if isinstance(item.get("monthlyMobileQcCnt"), str):
                        mob = 5
                    if kw:
                        result[kw] = pc + mob
        return result
    except Exception as e:
        logger.error(f"get_naver_search_volumes: {e}")
        return {}

def extract_ngrams(title: str) -> list[str]:
    """블로그 제목에서 1~3어절 조합 키워드 추출 (의미 있는 토큰만)"""
    import re as _re
    # 콜론/괄호 기준 분리 후 앞부분 사용
    title = _re.split(r'[:|ㅣ]', title)[0].strip()
    # 특수문자 제거, 토큰 분리
    tokens = _re.split(r"\s+", title)
    tokens = [t for t in tokens if len(t) >= 2]
    # 영문 단독 짧은 토큰 제거 (1BOX 등)
    tokens = [t for t in tokens if not _re.match(r'^[A-Za-z0-9]{1,3}$', t)]
    ngrams = []
    for n in (1, 2, 3):
        for i in range(len(tokens) - n + 1):
            kw = " ".join(tokens[i:i+n]).strip()
            if kw and len(kw) >= 2:
                ngrams.append(kw)
    # 중복 제거, 순서 유지
    seen = set()
    out  = []
    for k in ngrams:
        if k not in seen:
            seen.add(k); out.append(k)
    return out


# ── 게시물 키워드 노출 분석 ──────────────────────────────────
@app.route("/api/exposure", methods=["GET"])
def api_exposure_get():
    """GET /api/exposure?title=...&blog_id=... → JS 호환 래퍼"""
    try:
        title   = request.args.get("title", "").strip()
        blog_id = request.args.get("blog_id", "").strip()
        if not title:
            return jsonify({"error": "title 필요"}), 400
        candidates = extract_ngrams(title)[:20]
        if not candidates:
            return jsonify({"keywords": []})
        volumes = get_naver_search_volumes(candidates)
        sorted_kws = sorted(candidates, key=lambda k: volumes.get(k, 0), reverse=True)[:10]
        results = []
        for kw in sorted_kws:
            rank  = check_keyword_rank(kw, blog_id) if blog_id else 0
            vol   = volumes.get(kw, 0)
            if   rank == 0:   grade = "none"
            elif rank <= 10:  grade = "top"
            elif rank <= 30:  grade = "mid"
            else:             grade = "low"
            results.append({"keyword": kw, "monthly_pc": vol, "rank": rank if rank else None, "grade": grade})
        return jsonify({"keywords": results})
    except Exception as e:
        logger.error(f"api_exposure 오류: {e}")
        return jsonify({"error": f"분석 중 오류가 발생했습니다: {str(e)[:100]}"}), 200

@app.route("/api/post-exposure", methods=["POST"])
def api_post_exposure():
    """게시물 제목 기반 키워드 노출 현황 (검색량 높은 순 TOP10)"""
    data    = request.get_json() or {}
    title   = (data.get("title") or "").strip()
    blog_id = (data.get("blog_id") or "").strip()
    if not title:
        return jsonify({"error": "title 필요"}), 400

    # 1) 제목에서 ngram 키워드 추출
    candidates = extract_ngrams(title)[:20]
    if not candidates:
        return jsonify({"keywords": []})

    # 2) 네이버 검색량 조회
    volumes = get_naver_search_volumes(candidates)

    # 3) 검색량 순 정렬, TOP10
    sorted_kws = sorted(
        candidates,
        key=lambda k: volumes.get(k, 0),
        reverse=True
    )[:10]

    # 4) 각 키워드 블로그 검색 순위 확인
    results = []
    for kw in sorted_kws:
        rank  = check_keyword_rank(kw, blog_id) if blog_id else 0
        vol   = volumes.get(kw, 0)
        # 등급
        if   rank == 0:            grade = "미노출"
        elif rank <= 10:           grade = "상위노출"
        elif rank <= 30:           grade = "노출중"
        else:                      grade = "미노출"
        results.append({
            "keyword": kw,
            "volume":  vol,
            "rank":    rank,
            "grade":   grade,
        })

    return jsonify({"keywords": results, "total": len(results)})

def check_keyword_rank(keyword, blog_id):
    if not NAVER_CLIENT_ID or not blog_id: return 0
    headers = {"X-Naver-Client-Id":NAVER_CLIENT_ID,"X-Naver-Client-Secret":NAVER_CLIENT_SECRET}
    try:
        resp = requests.get("https://openapi.naver.com/v1/search/blog.json",
            headers=headers, params={"query":keyword,"display":100,"sort":"sim"}, timeout=10)
        if resp.status_code != 200: return 0
        pat = f"blog.naver.com/{blog_id}".lower()
        for i,item in enumerate(resp.json().get("items",[]),1):
            if pat in item.get("link","").lower(): return i
        return 0
    except Exception as e:
        logger.error(f"check_keyword_rank: {e}"); return 0

def save_ranking(keyword_id, company_id, keyword, rank):
    if not supabase: return
    try:
        supabase.table("rankings").insert({
            "keyword_id":keyword_id,"company_id":company_id,
            "keyword":keyword,"rank":rank,
            "checked_at":datetime.now(KST).isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"save_ranking: {e}")

def run_ranking_check():
    for c in get_companies():
        if not c.get("blog_id"): continue
        for kw in get_keywords(c["id"]):
            rank = check_keyword_rank(kw["keyword"],c["blog_id"])
            save_ranking(kw["id"],c["id"],kw["keyword"],rank)
            _name=c["name"]; _kw=kw["keyword"]
            logger.info(f"[순위] {_name} {_kw} -> {rank}위")

# ── 전체 스캔 ────────────────────────────────────────────────
def run_check(company_id: int = None):
    """전체 또는 특정 업체만 스캔"""
    companies = get_companies()
    if company_id:
        companies = [c for c in companies if c["id"] == company_id]
    blog_total = cafe_total = 0
    for c in companies:
        blog_total += check_blog(c)
        cafe_total += check_cafe(c)
    label = f"company_id={company_id}" if company_id else "전체"
    logger.info(f"✅ 스캔 완료({label}) — 블로그 {blog_total}건 / 카페 {cafe_total}건")
    return blog_total, cafe_total

# ── Claude AI 키워드 추천 ────────────────────────────────────
AI_KW_PROMPT = (
    "다음은 '{name}' 블로그의 최근 게시글 제목들입니다.\n"
    "이 블로그 글에 관심 있는 사람들이 네이버에서 실제로 검색할 키워드 10개를 추천해주세요.\n\n"
    "조건:\n"
    "- 소비자/구매자 관점에서 검색할 2~4단어 키워드 (업체명 제외)\n"
    "- 제품명, 서비스명, 지역+업종, 상황별 니즈 등 구체적인 키워드\n"
    "- 네이버 블로그 검색 상위 노출을 위한 실용적 키워드\n"
    "- 너무 포괄적인 단어(예: 추천, 방법, 정보) 단독 사용 금지\n"
    "- JSON 배열만 응답 (설명 없이)\n\n"
    "블로그 제목 목록:\n{titles}\n\n"
    "응답 형식: [\"키워드1\", \"키워드2\", ..., \"키워드10\"]"
)

def suggest_keywords_ai(company_name, posts):
    if not ANTHROPIC_API_KEY or not posts:
        return []
    try:
        import anthropic as _ant, json as _json, re as _re
        titles = "\n".join(
            "- " + p["title"] for p in posts[:20] if p.get("title")
        )
        if not titles:
            return []
        prompt = AI_KW_PROMPT.format(name=company_name, titles=titles)
        client = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg    = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        m    = _re.search(r"\[.*?\]", text, _re.DOTALL)
        if m:
            kws = _json.loads(m.group())
            return [k.strip() for k in kws if isinstance(k, str) and k.strip()][:10]
        return []
    except Exception as e:
        logger.error(f"suggest_keywords_ai: {e}")
        return []

def do_ai_suggest_and_rank(company):
    if not supabase:
        return {"saved": 0, "keywords": []}
    posts    = get_posts_by_company(company["id"], "blog", 20)
    existing = {kw["keyword"] for kw in get_keywords(company["id"])}
    kws      = suggest_keywords_ai(company["name"], posts)
    saved    = []
    for kw_text in kws:
        if kw_text in existing:
            continue
        try:
            r = supabase.table("keywords").insert({
                "company_id": company["id"],
                "keyword":    kw_text,
                "active":     True,
            }).execute()
            if r.data:
                kw_row = r.data[0]
                saved.append(kw_row)
                rank = check_keyword_rank(kw_text, company.get("blog_id", ""))
                save_ranking(kw_row["id"], company["id"], kw_text, rank)
                logger.info(f"[AI키워드] {company['name']} '{kw_text}' -> {rank}위")
        except Exception as e:
            logger.error(f"do_ai_suggest_and_rank: {e}")
    return {"saved": len(saved), "keywords": [k["keyword"] for k in saved]}


# ── Flask 라우트 ──────────────────────────────────────────────
@app.route("/")
def index():
    companies = get_companies()
    stats     = get_stats()
    now_kst   = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    for c in companies:
        c["blog_posts"] = get_posts_by_company(c["id"], "blog", 10)
        c["cafe_posts"] = get_posts_by_company(c["id"], "cafe", 10)
        # 카페 글목록 URL이 있으면 조회수/작성일 스크래핑으로 보완
        if c.get("cafe_url"):
            scraped = scrape_cafe_member_posts(c["cafe_url"], limit=20)
            if scraped:
                # DB 글과 스크래핑 글을 제목 기준으로 매핑
                scraped_by_title = {p["title"][:30]: p for p in scraped}
                for post in c["cafe_posts"]:
                    key = (post.get("title") or "")[:30]
                    if key in scraped_by_title:
                        post["views_count"] = scraped_by_title[key].get("views_count", 0)
                # DB에 없는 스크래핑 글도 추가 (최대 20개)
                db_titles = {(p.get("title") or "")[:30] for p in c["cafe_posts"]}
                for sp in scraped:
                    if sp["title"][:30] not in db_titles:
                        c["cafe_posts"].append({
                            "title": sp["title"],
                            "url": sp["url"],
                            "published_at": sp["published_at"],
                            "views_count": sp.get("views_count", 0),
                        })
                # 날짜 재정렬
                from dateutil import parser as _dp
                def _sk(p):
                    raw = p.get("published_at") or ""
                    try: return _dp.parse(raw) if raw else __import__("datetime").datetime.min
                    except: return __import__("datetime").datetime.min
                c["cafe_posts"].sort(key=_sk, reverse=True)
                c["cafe_posts"] = c["cafe_posts"][:20]
        week            = get_week_counts(c["id"])
        c["week_blog"]  = week["blog"]
        c["week_cafe"]  = week["cafe"]
        kws = get_keywords(c["id"])
        for kw in kws:
            kw["latest"] = get_latest_ranking(kw["id"])
        c["keywords"]   = kws
        c["daily"]      = get_daily_counts(c["id"])
        # 선택 필드 기본값 보장 (Supabase에 컬럼 없어도 안전)
        c.setdefault("cafe_name", "")
        c.setdefault("cafe_home", "")
        c.setdefault("cafe_url",  "")
        c.setdefault("cafe_author", "")
        c.setdefault("blog_id",   "")
    slack_connected = bool(SLACK_WEBHOOK_URL)
    slack_masked = ("https://hooks.slack.com/services/***" if SLACK_WEBHOOK_URL else "")
    return render_template("index.html",
        companies=companies, stats=stats, now_kst=now_kst,
        slack_connected=slack_connected,
        slack_webhook_masked=slack_masked)

@app.route("/api/scan", methods=["POST"])
def api_scan():
    blog_new, cafe_new = run_check()
    return jsonify({
        "status":     "ok",
        "blog_new":   blog_new,
        "cafe_new":   cafe_new,
        "checked_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
    })

@app.route("/api/companies", methods=["GET"])
def api_companies():
    return jsonify(get_companies())

@app.route("/api/companies", methods=["POST"])
def api_add_company():
    data = request.get_json()
    if not supabase:
        return jsonify({"error":"Supabase not configured"}), 500
    try:
        r = supabase.table("companies").insert({
            "name":        data.get("name","").strip(),
            "blog_id":     data.get("blog_id","").strip(),
            "cafe_author": data.get("cafe_author","").strip(),
            "cafe_name":   data.get("cafe_name","").strip(),
            "cafe_home":   data.get("cafe_home","").strip(),
            "cafe_url":    data.get("cafe_url","").strip(),
            "active":      True,
        }).execute()
        return jsonify({"status":"ok","data":r.data})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


@app.route("/api/companies/<int:company_id>", methods=["PUT"])
def api_update_company(company_id):
    data = request.get_json()
    if not supabase:
        return jsonify({"error": "Supabase not configured"}), 500
    try:
        # cafe_name은 폼에서 직접 입력받으므로 HTTP 호출 없이 바로 저장
        update_data = {
            "name":        data.get("name", "").strip(),
            "blog_id":     data.get("blog_id", "").strip(),
            "cafe_author": data.get("cafe_author", "").strip(),
            "cafe_name":   data.get("cafe_name", "").strip(),
            "cafe_home":   data.get("cafe_home", "").strip(),
            "cafe_url":    data.get("cafe_url", "").strip(),
        }
        r = supabase.table("companies").update(update_data).eq("id", company_id).execute()
        return jsonify({"status": "ok", "data": r.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/companies/<int:company_id>", methods=["DELETE"])
def api_delete_company(company_id):
    if not supabase:
        return jsonify({"error":"Supabase not configured"}), 500
    try:
        supabase.table("companies").update({"active":False}).eq("id",company_id).execute()
        return jsonify({"status":"ok"})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/posts/<int:company_id>/<source_type>")
def api_posts(company_id, source_type):
    if source_type not in ("blog","cafe"):
        return jsonify({"error":"invalid source_type"}), 400
    return jsonify(get_posts_by_company(company_id, source_type, 10))


# ── 키워드 API 라우트 ────────────────────────────────────────
@app.route("/api/keywords/suggest/<int:company_id>", methods=["POST"])
def api_suggest_keywords(company_id):
    companies = get_companies()
    company   = next((c for c in companies if c["id"] == company_id), None)
    if not company:
        return jsonify({"error": "company not found"}), 404
    result = do_ai_suggest_and_rank(company)
    return jsonify({"status": "ok", **result})

@app.route("/api/keywords", methods=["POST"])
def api_add_keyword():
    data = request.get_json()
    if not supabase:
        return jsonify({"error": "Supabase not configured"}), 500
    try:
        kw_text    = data.get("keyword", "").strip()
        company_id = data.get("company_id")
        if not kw_text:
            return jsonify({"error": "keyword required"}), 400
        r = supabase.table("keywords").insert({
            "company_id": company_id,
            "keyword":    kw_text,
            "active":     True,
        }).execute()
        if r.data:
            kw_row    = r.data[0]
            companies = get_companies()
            company   = next((c for c in companies if c["id"] == company_id), None)
            if company:
                rank = check_keyword_rank(kw_text, company.get("blog_id", ""))
                save_ranking(kw_row["id"], company_id, kw_text, rank)
        return jsonify({"status": "ok", "data": r.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/keywords/<int:kw_id>", methods=["DELETE"])
def api_delete_keyword(kw_id):
    if not supabase:
        return jsonify({"error": "Supabase not configured"}), 500
    try:
        supabase.table("keywords").update({"active": False}).eq("id", kw_id).execute()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/rank-check", methods=["POST"])
def api_rank_check():
    run_ranking_check()
    return jsonify({"status": "ok", "checked_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")})


# ── 카페 글 URL → 작성자 ID 자동 추출 ─────────────────────────
@app.route("/api/parse-cafe-author", methods=["POST"])
def api_parse_cafe_author():
    data = request.get_json()
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url 필요"}), 400
    try:
        result = extract_cafe_author(url)
        return jsonify(result)
    except Exception as e:
        logger.error(f"parse-cafe-author error: {e}")
        return jsonify({"error": str(e)}), 500


def extract_cafe_author(url: str) -> dict:
    """네이버 카페 글 URL에서 작성자 memberId와 글목록 URL 추출"""
    import re as _re
    from urllib.parse import unquote, urlparse, parse_qs

    # ── URL 정규화 ──────────────────────────────────────────
    # 형태1: cafe.naver.com/{slug}?iframe_url_utf8=%2FArticleRead...
    # 형태2: cafe.naver.com/ArticleRead.nhn?clubid=...&articleid=...
    # 형태3: cafe.naver.com/{slug}/{articleid}
    parsed   = urlparse(url)
    qs       = parse_qs(parsed.query)
    club_id  = None
    article_id = None
    cafe_slug  = None

    # 슬러그 추출
    path_parts = [p for p in parsed.path.split("/") if p]
    if path_parts:
        cafe_slug = path_parts[0]

    # iframe_url_utf8 방식
    iframe_raw = qs.get("iframe_url_utf8", [""])[0]
    if iframe_raw:
        decoded = unquote(unquote(iframe_raw))          # 이중 인코딩
        m_club    = _re.search(r"clubid[=:](\d+)",    decoded, _re.I)
        m_article = _re.search(r"articleid[=:](\d+)", decoded, _re.I)
        if m_club:    club_id    = m_club.group(1)
        if m_article: article_id = m_article.group(1)

    # 직접 파라미터 방식
    if not club_id:
        club_id    = (qs.get("clubid",    [""])[0] or
                      qs.get("clubId",    [""])[0])
    if not article_id:
        article_id = (qs.get("articleid", [""])[0] or
                      qs.get("articleId", [""])[0])

    # 경로형: /jihosoccer123/12345
    if not article_id and len(path_parts) >= 2 and path_parts[-1].isdigit():
        article_id = path_parts[-1]

    if not article_id:
        return {"ok": False, "error": "글 번호(articleId)를 찾을 수 없어요. 글 상세 페이지 URL을 붙여넣어 주세요."}

    # ── 글 페이지 fetch ──────────────────────────────────────
    fetch_url = (
        f"https://cafe.naver.com/ArticleRead.nhn?clubid={club_id}&articleid={article_id}"
        if club_id else
        f"https://cafe.naver.com/{cafe_slug}/{article_id}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
        "Referer":    "https://cafe.naver.com/",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    resp = requests.get(fetch_url, headers=headers, timeout=12, allow_redirects=True)
    html = resp.text

    # ── 작성자 ID 파싱 ───────────────────────────────────────
    author_id   = None
    author_nick = None
    WID  = r'writerId'
    AID  = r'authorId'
    MID  = r'memberId'
    WNN  = r'writerNickname'
    ANN  = r'authorName'
    NN   = r'nickname'

    patterns = [
        (_re.compile(r'"writerId"\s*:\s*"([^"]+)"',   _re.I), 1),
        (_re.compile(r'"authorId"\s*:\s*"([^"]+)"',   _re.I), 1),
        (_re.compile(r'"memberId"\s*:\s*"([^"]+)"',   _re.I), 1),
        (_re.compile(r'data-writer-member-id="([^"]+)"', _re.I), 1),
        (_re.compile(r'data-member-id="([^"]+)"',        _re.I), 1),
        (_re.compile(r'memberid=([A-Za-z0-9_]+)',           _re.I), 1),
        (_re.compile(r'writer_member_id[^=]+?=.?([A-Za-z0-9_]+)', _re.I), 1),
    ]
    nick_patterns = [
        (_re.compile(r'"writerNickname"\s*:\s*"([^"]+)"', _re.I), 1),
        (_re.compile(r'"authorName"\s*:\s*"([^"]+)"',     _re.I), 1),
        (_re.compile(r'"nickname"\s*:\s*"([^"]+)"',       _re.I), 1),
    ]
    for pat, grp in patterns:
        m = pat.search(html)
        if m:
            val = m.group(grp).strip()
            if val and val not in ("null","undefined",""):
                author_id = val
                break
    for pat, grp in nick_patterns:
        m = pat.search(html)
        if m:
            val = m.group(grp).strip()
            if val and val not in ("null","undefined",""):
                author_nick = val
                break

    # clubid가 없었으면 HTML에서 추출 시도
    if not club_id:
        mc = _re.search(r'"clubId"\s*:\s*"?(\d+)"?', html)
        if mc:
            club_id = mc.group(1)

    # ── 글목록 URL 구성 ──────────────────────────────────────
    member_list_url = None
    if cafe_slug and author_id and club_id:
        member_list_url = (
            f"https://cafe.naver.com/{cafe_slug}?"
            f"iframe_url_utf8=%2FMemberView.nhn"
            f"%3Fclubid%3D{club_id}"
            f"%26memberid%3D{author_id}"
        )
    elif cafe_slug and author_id:
        member_list_url = f"https://cafe.naver.com/{cafe_slug}/search/by-member?memberId={author_id}"

    if not author_id:
        return {
            "ok":       False,
            "error":    "작성자 ID를 자동으로 찾지 못했어요. 네이버 로그인이 필요한 비공개 카페일 수 있어요.",
            "html_len": len(html),
            "fetch_url": fetch_url,
        }

    return {
        "ok":              True,
        "author_id":       author_id,
        "author_nick":     author_nick,
        "cafe_slug":       cafe_slug,
        "club_id":         club_id,
        "article_id":      article_id,
        "member_list_url": member_list_url,
    }

@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


# ── 2. 키워드 빈도 분석 ──────────────────────────────────────
@app.route("/api/keyword-analysis/<int:company_id>")
def api_keyword_analysis(company_id):
    """경쟁사 블로그/카페 제목 키워드 TOP20 각각 반환"""
    if not supabase:
        return jsonify({"blog": [], "cafe": []})
    try:
        r = (supabase.table("detected_posts")
             .select("title,source_type")
             .eq("company_id", company_id)
             .order("detected_at", desc=True)
             .limit(300)
             .execute())
        posts = r.data or []
        # 실제 검색 가능성 없는 단어 + 장소/일반동사/조사 제거
        # 브랜드명, 제품명, 업종명, 기능명 위주로 남김
        STOPWORDS = {
            # ── 조사/어미/접속사 ──
            "이","그","저","것","수","등","및","또","더","를","을","은","는","의","가","에","도",
            "이다","있다","없다","하다","되다","않다","합니다","있습니다","해요","했어요",
            "에서","에게","으로","부터","까지","와","과","하고","이고","로","에는","에서는",
            "이며","이자","으로서","로서","한테","께서","만큼","처럼","보다","라도","마저",
            # ── 검색 가치 없는 일반 형용사/부사 ──
            "완전","진짜","정말","너무","좋은","좋아","많이","매우","꼭","바로","이제","다시",
            "어떻게","왜","언제","어디","무엇","어떤","얼마나","그냥","혹시","아직","이미",
            "드디어","결국","역시","항상","보통","그런데","그래서","하지만","그러나","따라서",
            "솔직히","사실","물론","만약","혹은","또는","아니면","즉","즉시","약간","조금",
            # ── 동작/과정 동사 (제목 동사라 검색가치 낮음) ──
            "해도","해서","하면","하는","하고","하여","하게","하기","하지","했다","했는",
            "보세요","봐요","보면","보니","알아보","살펴","따라","통해","위한","위해",
            "됩니다","됩니까","되나요","되는지","인지","것인지","건지","인가요","인데요",
            "드립니다","드려요","드릴","드린","겠습니다","겠어요","했습니다",
            # ── 일반 공간/장소 (너무 포괄적) ──
            "집","곳","곳에","매장","가게","업체","회사","우리","저희","고객","사람","분들",
            "주변","인근","근처","지역","지역의","전국","전체","전문","직접",
            # ── 구체성 없는 일반 단어 ──
            "방법","경우","이후","이전","현재","최근","지금","오늘","내일","어제","올해",
            "리뷰","후기","사례","소개","안내","정보","내용","이야기","얘기","이야기해",
            "구매","선택","비교","확인","신청","운반","방문","완료","준비","시작","마무리",
            "진행","설명","문의","연락","상담","견적","서비스","관리","처리","해결",
            # ── 수량/정도 ──
            "개","번","회","차","번째","가지","종류","명","분","시간","분간","일","주","달","년",
            # ── 특수문자/기호 ──
            "TOP","top","No","no","vs","VS","|","·","/","~","..","...",">>","<<",
            # ── 지역명 (단독 검색은 하지만 키워드 분석 노이즈) ──
            "서울","경기","인천","부산","대구","대전","광주","수원","성남","용인","고양",
            "파주","김밥집","식당","음식점","카페","카페는",
            # ── 단독으로 의미없는 짧은 단어 ──
            "것도","것을","것은","것이","게","거","건","걸","걔","쟤",
        }

        from collections import Counter
        def top_words(rows):
            """1단어 + 2단어 조합 빈도 분석 (실제 검색어 패턴 반영)"""
            cnt = Counter()
            for p in rows:
                words = re.findall(r"[가-힣]{2,}|[A-Za-z0-9]{2,}", p.get("title",""))
                cleaned = [w for w in words if w not in STOPWORDS and len(w) >= 2]
                # 1-gram
                for w in cleaned:
                    cnt[w] += 1
                # 2-gram (실제 검색 조합 키워드)
                for i in range(len(cleaned)-1):
                    bigram = cleaned[i] + " " + cleaned[i+1]
                    cnt[bigram] += 1
            # 1회 등장 단어 제거 (노이즈)
            cnt = Counter({k: v for k, v in cnt.items() if v >= 2})
            return [{"word": w, "count": c} for w, c in cnt.most_common(25)]
        blog_posts = [p for p in posts if p.get("source_type") == "blog"]
        cafe_posts = [p for p in posts if p.get("source_type") == "cafe"]
        return jsonify({"blog": top_words(blog_posts), "cafe": top_words(cafe_posts)})
    except Exception as e:
        logger.error(f"keyword_analysis: {e}")
        return jsonify({"blog": [], "cafe": [], "error": str(e)})


# ── 3. 발행 패턴 분석 ────────────────────────────────────────
@app.route("/api/publish-pattern/<int:company_id>")
def api_publish_pattern(company_id):
    """블로그/카페 요일별 / 시간대별 발행 패턴 각각 반환"""
    empty = {"by_day":[],"by_hour":[]}
    if not supabase:
        return jsonify({"blog": empty, "cafe": empty})
    try:
        from datetime import timedelta
        since = (datetime.now(KST) - timedelta(days=90)).isoformat()
        r = (supabase.table("detected_posts")
             .select("published_at,source_type")
             .eq("company_id", company_id)
             .gte("published_at", since)
             .execute())
        posts = r.data or []
        DAY_KO = ["월","화","수","목","금","토","일"]
        def calc(rows):
            bd=[0]*7; bh=[0]*24
            for p in rows:
                raw = p.get("published_at") or ""
                if not raw: continue
                try:
                    if raw.endswith("+09:00") or raw.endswith("Z"):
                        dt = datetime.fromisoformat(raw.replace("Z","+00:00")).astimezone(KST)
                    else:
                        dt = datetime.fromisoformat(raw).replace(tzinfo=KST)
                    bd[dt.weekday()]+=1; bh[dt.hour]+=1
                except: continue
            return {
                "by_day":  [{"day":DAY_KO[i],"count":bd[i]} for i in range(7)],
                "by_hour": [{"hour":i,"count":bh[i]} for i in range(24)],
            }
        return jsonify({
            "blog": calc([p for p in posts if p.get("source_type")=="blog"]),
            "cafe": calc([p for p in posts if p.get("source_type")=="cafe"]),
        })
    except Exception as e:
        logger.error(f"publish_pattern: {e}")
        return jsonify({"blog": empty, "cafe": empty, "error": str(e)})


# ── 슬랙 웹훅 설정 확인 ──────────────────────────────────────
@app.route("/api/alert-test", methods=["POST"])
def api_alert_test():
    """슬랙 웹훅 테스트 전송"""
    data = request.get_json() or {}
    webhook = data.get("webhook_url", "").strip() or SLACK_WEBHOOK_URL
    if not webhook:
        return jsonify({"ok": False, "error": "웹훅 URL이 없어요. Render 환경변수 SLACK_WEBHOOK_URL을 설정하거나 직접 입력해주세요."})
    try:
        now = datetime.now(KST).strftime("%m/%d %H:%M")
        resp = requests.post(webhook, json={
            "text": f"✅ *경쟁사 콘텐츠 추적 모니터 알림 연결 완료!* — {now}\n새 글이 감지되면 이 채널로 알림이 전송됩니다 📡"
        }, timeout=8)
        if resp.status_code == 200:
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": f"슬랙 응답: {resp.status_code} {resp.text}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── 로그인 페이지 ────────────────────────────────────────────
@app.route("/login", methods=["GET","POST"])
def login():
    from flask import session, redirect
    error = ""
    if request.method == "POST":
        pw = request.form.get("password","").strip()
        if pw == SITE_PASSWORD:
            session["authenticated"] = True
            return redirect("/")
        error = "비밀번호가 틀렸습니다"
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>경쟁사 콘텐츠 추적 모니터</title>
<link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{
  font-family:'Pretendard',sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:#0d1117;overflow:hidden;
}}
.orb{{position:fixed;border-radius:50%;filter:blur(90px);pointer-events:none;}}
.orb1{{width:500px;height:500px;background:rgba(59,91,219,.25);top:-150px;left:-150px;animation:drift 9s ease-in-out infinite alternate;}}
.orb2{{width:400px;height:400px;background:rgba(124,58,237,.2);bottom:-120px;right:-120px;animation:drift 11s ease-in-out infinite alternate-reverse;}}
.orb3{{width:250px;height:250px;background:rgba(14,165,233,.15);top:40%;left:45%;animation:drift 7s ease-in-out infinite alternate;}}
@keyframes drift{{0%{{transform:translate(0,0);}}100%{{transform:translate(40px,30px);}}}}
.box{{
  position:relative;z-index:10;
  background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.09);
  border-radius:28px;padding:48px 44px 40px;width:400px;
  backdrop-filter:blur(30px);
  box-shadow:0 32px 80px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.1);
  animation:up .55s cubic-bezier(.16,1,.3,1) both;
}}
@keyframes up{{from{{opacity:0;transform:translateY(28px);}}to{{opacity:1;transform:translateY(0);}}}}
.icon-wrap{{
  width:68px;height:68px;margin:0 auto 20px;
  background:linear-gradient(135deg,#3b5bdb,#7c3aed);
  border-radius:20px;display:flex;align-items:center;justify-content:center;
  font-size:30px;box-shadow:0 8px 30px rgba(59,91,219,.45);
  animation:pulse 3s ease-in-out infinite;
}}
@keyframes pulse{{0%,100%{{box-shadow:0 8px 30px rgba(59,91,219,.45);}}50%{{box-shadow:0 12px 40px rgba(124,58,237,.6);}}}}
h1{{font-size:20px;font-weight:800;color:#f0f4ff;letter-spacing:-.4px;margin-bottom:5px;}}
.sub{{font-size:12px;color:rgba(255,255,255,.38);letter-spacing:.4px;margin-bottom:34px;}}
.inp-wrap{{position:relative;margin-bottom:12px;}}
.inp-icon{{position:absolute;left:15px;top:50%;transform:translateY(-50%);font-size:16px;opacity:.45;pointer-events:none;}}
input{{
  width:100%;padding:13px 16px 13px 46px;
  background:rgba(255,255,255,.07);
  border:1.5px solid rgba(255,255,255,.11);
  border-radius:14px;color:#e8efff;
  font-family:'Pretendard',sans-serif;font-size:14px;
  letter-spacing:3px;outline:none;transition:border-color .2s,background .2s;
}}
input::placeholder{{color:rgba(255,255,255,.28);letter-spacing:0;}}
input:focus{{border-color:rgba(99,135,255,.75);background:rgba(255,255,255,.1);}}
input.shake{{animation:shake .4s ease;border-color:rgba(255,120,120,.7) !important;}}
@keyframes shake{{
  0%,100%{{transform:translateX(0);}}20%{{transform:translateX(-8px);}}
  40%{{transform:translateX(8px);}}60%{{transform:translateX(-5px);}}80%{{transform:translateX(5px);}}
}}
.btn{{
  width:100%;padding:15px;margin-top:4px;
  background:linear-gradient(135deg,#3b5bdb,#7c3aed);
  color:#fff;border:none;border-radius:14px;
  font-size:15px;font-weight:700;font-family:'Pretendard',sans-serif;
  cursor:pointer;transition:all .2s;position:relative;overflow:hidden;
  box-shadow:0 4px 20px rgba(59,91,219,.4);
  display:flex;align-items:center;justify-content:center;gap:8px;min-height:52px;
}}
.btn:hover:not(:disabled){{transform:translateY(-2px);box-shadow:0 10px 30px rgba(59,91,219,.55);}}
.btn:disabled{{cursor:not-allowed;}}
.spinner{{
  width:18px;height:18px;
  border:2.5px solid rgba(255,255,255,.25);
  border-top-color:#fff;border-radius:50%;
  animation:spin .65s linear infinite;display:none;
}}
@keyframes spin{{to{{transform:rotate(360deg);}}}}
.pbar{{
  position:absolute;bottom:0;left:0;height:3px;width:0;
  background:linear-gradient(90deg,rgba(255,255,255,.4),#fff);
  transition:width 2.5s ease;
}}
.err-msg{{
  color:#ff8080;font-size:12px;margin-top:10px;text-align:left;
  display:flex;align-items:center;gap:5px;
  animation:fin .25s ease;
}}
@keyframes fin{{from{{opacity:0;transform:translateY(-4px);}}to{{opacity:1;transform:translateY(0);}}}}
.status-row{{
  display:flex;align-items:center;justify-content:center;gap:6px;
  margin-top:20px;padding-top:20px;
  border-top:1px solid rgba(255,255,255,.07);
}}
.dot{{width:6px;height:6px;border-radius:50%;background:#10b981;animation:blink 2s infinite;flex-shrink:0;}}
@keyframes blink{{0%,100%{{opacity:1;}}50%{{opacity:.25;}}}}
.status-txt{{font-size:11px;color:rgba(255,255,255,.28);}}
</style></head>
<body>
<div class="orb orb1"></div><div class="orb orb2"></div><div class="orb orb3"></div>
<div class="box">
  <div class="icon-wrap">📡</div>
  <h1>경쟁사 콘텐츠 추적 모니터</h1>
  <div class="sub">AI마케팅전략연구소 &nbsp;·&nbsp; 직원 전용</div>
  <form id="lf" onsubmit="return go(event)">
    <div class="inp-wrap">
      <span class="inp-icon">🔑</span>
      <input type="password" id="pw" name="password" placeholder="접속 비밀번호" autofocus autocomplete="current-password">
    </div>
    <button type="submit" class="btn" id="btn">
      <span id="bicon">🔐</span>
      <span id="btxt">접속하기</span>
      <div class="spinner" id="sp"></div>
      <div class="pbar" id="pb"></div>
    </button>
  </form>
  {'<div class="err-msg">⚠️ ' + error + '</div>' if error else ''}
  <div class="status-row"><div class="dot"></div><div class="status-txt">시스템 정상 운영 중</div></div>
</div>
<script>
function go(e){{
  const v=document.getElementById('pw').value.trim();
  if(!v){{
    e.preventDefault();
    const el=document.getElementById('pw');
    el.classList.remove('shake'); void el.offsetWidth; el.classList.add('shake');
    el.focus();
    showErr('비밀번호를 입력해주세요');
    return false;
  }}
  const btn=document.getElementById('btn');
  document.getElementById('bicon').style.display='none';
  document.getElementById('btxt').textContent='접속 중...';
  document.getElementById('sp').style.display='block';
  btn.disabled=true;
  setTimeout(()=>{{document.getElementById('pb').style.width='80%';}},30);
  return true;
}}
function showErr(msg){{
  let el=document.getElementById('dynErr');
  if(!el){{el=document.createElement('div');el.id='dynErr';el.className='err-msg';document.getElementById('lf').after(el);}}
  el.innerHTML='⚠️ '+msg;
}}
document.getElementById('pw').addEventListener('input',()=>{{
  const e=document.getElementById('dynErr');if(e)e.remove();
  document.getElementById('pw').classList.remove('shake');
}});
</script>
</body></html>"""

@app.route("/health")
def health():
    return jsonify({"status":"ok","time":datetime.now(KST).isoformat()})

# ── APScheduler: 30분 자동스캔 + 5분 self-ping ──────────────
def self_ping():
    """Render 슬립 방지 — 5분마다 /health 호출"""
    try:
        app_url = os.environ.get("APP_URL", "")
        if app_url:
            import urllib.request
            urllib.request.urlopen(f"{app_url}/health", timeout=10)
            logger.info("self-ping OK")
    except Exception as e:
        logger.warning(f"self-ping failed: {e}")

scheduler = BackgroundScheduler(timezone=KST)
scheduler.add_job(run_check,  "interval", minutes=30, id="auto_scan")
scheduler.add_job(self_ping,  "interval", minutes=5,  id="keep_alive")
scheduler.start()

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
else:
    init_db()
