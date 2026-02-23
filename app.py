from flask import Flask, render_template, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import os
import json

app = Flask(__name__)

KST = timezone(timedelta(hours=9))
WARN_HOURS = 10
ALERT_INTERVAL_HOURS = 3
DAILY_GOAL = 10  # 하루 목표 발행 수
alert_last_sent = {}
goal_alert_sent = {}  # blog_id: date (10개 달성 알림 발송 날짜)

# ========== 카카오 토큰 자동 갱신 시스템 ==========
kakao_tokens = {
    "access_token": os.environ.get("KAKAO_ACCESS_TOKEN", ""),
    "refresh_token": os.environ.get("KAKAO_REFRESH_TOKEN", ""),
    "token_updated_at": None
}


def refresh_kakao_token():
    """리프레시 토큰으로 액세스 토큰 자동 갱신"""
    refresh_token = kakao_tokens["refresh_token"]
    if not refresh_token:
        print("[KAKAO] 리프레시 토큰 없음")
        return False

    client_id = os.environ.get("KAKAO_CLIENT_ID", "")
    client_secret = os.environ.get("KAKAO_CLIENT_SECRET", "")

    params = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if client_secret:
        params["client_secret"] = client_secret

    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        "https://kauth.kakao.com/oauth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())

        if "access_token" in result:
            kakao_tokens["access_token"] = result["access_token"]
            kakao_tokens["token_updated_at"] = datetime.now(KST).isoformat()
            print(f"[KAKAO] ✅ 액세스 토큰 갱신 성공!")

            # 리프레시 토큰도 새로 발급되면 업데이트 (만료 1개월 전에 새로 줌)
            if "refresh_token" in result:
                kakao_tokens["refresh_token"] = result["refresh_token"]
                print(f"[KAKAO] ✅ 리프레시 토큰도 갱신됨!")

            # Supabase에 토큰 백업 저장 (서버 재시작 대비)
            save_tokens_to_supabase()
            return True
        else:
            print(f"[KAKAO] ❌ 갱신 실패: {result}")
            return False
    except Exception as e:
        print(f"[KAKAO] ❌ 갱신 오류: {e}")
        return False


def save_tokens_to_supabase():
    """토큰을 Supabase에 백업 저장"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    data = {
        "id": "kakao_tokens",
        "access_token": kakao_tokens["access_token"],
        "refresh_token": kakao_tokens["refresh_token"],
        "updated_at": datetime.now(KST).isoformat()
    }
    supabase_request("POST", "kakao_tokens?on_conflict=id", data)


def load_tokens_from_supabase():
    """서버 시작 시 Supabase에서 최신 토큰 불러오기"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    result = supabase_request("GET", "kakao_tokens?id=eq.kakao_tokens")
    if result and len(result) > 0:
        row = result[0]
        # Supabase 토큰이 환경변수보다 최신이면 사용
        if row.get("access_token"):
            kakao_tokens["access_token"] = row["access_token"]
        if row.get("refresh_token"):
            kakao_tokens["refresh_token"] = row["refresh_token"]
        kakao_tokens["token_updated_at"] = row.get("updated_at")
        print(f"[KAKAO] Supabase에서 토큰 로드 완료")


def get_valid_kakao_token():
    """유효한 카카오 액세스 토큰 반환 (필요시 자동 갱신)"""
    return kakao_tokens.get("access_token", "")


def send_kakao_message(msg):
    """카카오톡 메시지 전송 (401 시 자동 갱신 후 재시도)"""
    token = get_valid_kakao_token()
    if not token:
        return {"status": "skip", "reason": "토큰 없음"}

    for attempt in range(2):  # 최대 2번 시도
        data = urllib.parse.urlencode({"template_object": json.dumps({
            "object_type": "text",
            "text": msg,
            "link": {"web_url": "https://blog-monitor-p4nn.onrender.com"}
        })}).encode()
        req = urllib.request.Request(
            "https://kapi.kakao.com/v2/api/talk/memo/default/send",
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                return {"status": "ok", "result": result}
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                print(f"[KAKAO] 토큰 만료! 자동 갱신 시도...")
                if refresh_kakao_token():
                    token = kakao_tokens["access_token"]
                    continue  # 갱신 후 재시도
                else:
                    return {"status": "error", "reason": "토큰 갱신 실패"}
            return {"status": "error", "code": e.code, "reason": e.read().decode()}
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    return {"status": "error", "reason": "전송 실패"}
# ========== 카카오 토큰 자동 갱신 시스템 끝 ==========



BLOG_IDS = [
    os.environ.get("BLOG1", ""),
    os.environ.get("BLOG2", ""),
    os.environ.get("BLOG3", ""),
    os.environ.get("BLOG4", ""),
    os.environ.get("BLOG5", ""),
    os.environ.get("BLOG6", ""),
]

BLOG_LABELS = [
    os.environ.get("LABEL1", ""),
    os.environ.get("LABEL2", ""),
    os.environ.get("LABEL3", ""),
    os.environ.get("LABEL4", ""),
    os.environ.get("LABEL5", ""),
    os.environ.get("LABEL6", ""),
]

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


def supabase_request(method, path, data=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation,resolution=merge-duplicates"
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"Supabase error: {e}")
        return None


# 서버 시작 시 Supabase에서 최신 토큰 불러오기 (Gunicorn 포함)
load_tokens_from_supabase()


def get_history(blog_id):
    today = datetime.now(KST).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    result = supabase_request("GET", f"blog_stats?blog_id=eq.{blog_id}&date=gte.{dates[0]}&order=date.asc")
    history = {d: 0 for d in dates}
    if result:
        for row in result:
            if row["date"] in history:
                history[row["date"]] = row["count"]
    return [{"date": d, "count": history[d]} for d in dates]


def fetch_blog_posts(blog_id):
    url = f"https://rss.blog.naver.com/{blog_id}.xml"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()
    except Exception as e:
        return {"ok": False, "error": str(e)[:80], "posts": [], "today_count": 0}

    try:
        root = ET.fromstring(xml_data)
        items = root.findall(".//item")
    except:
        return {"ok": False, "error": "XML 파싱 오류", "posts": [], "today_count": 0}

    if not items:
        return {"ok": False, "error": "글 없음", "posts": [], "today_count": 0}

    now = datetime.now(KST)
    today_str = now.date().isoformat()
    posts = []
    today_count = 0

    for item in items[:15]:  # 최대 15개
        title = item.findtext("title") or ""
        pub_date_str = item.findtext("pubDate") or ""
        link = item.findtext("link") or ""
        try:
            dt = parsedate_to_datetime(pub_date_str).astimezone(KST)
            elapsed = (now - dt).total_seconds() / 3600
            if dt.date().isoformat() == today_str:
                today_count += 1
            h = int(elapsed)
            m = int((elapsed % 1) * 60)
            if elapsed < 1:
                lbl = f"{m}분 전"
            elif elapsed < 24:
                lbl = f"{h}시간 {m}분 전"
            else:
                lbl = f"{int(elapsed//24)}일 전"
            posts.append({
                "title": title,
                "hoursAgo": round(elapsed, 1),
                "timeLabel": lbl,
                "link": link
            })
        except:
            continue

    return {"ok": True, "posts": posts, "today_count": today_count}


@app.route("/")
def index():
    blogs = []
    for i, (bid, blabel) in enumerate(zip(BLOG_IDS, BLOG_LABELS)):
        if bid:
            blogs.append({"id": bid, "label": blabel, "num": i + 1})
    return render_template("index.html", blogs=blogs, warn_hours=WARN_HOURS)


@app.route("/api/check")
def check_all():
    results = []
    for bid, blabel in zip(BLOG_IDS, BLOG_LABELS):
        if not bid:
            continue
        result = fetch_blog_posts(bid)
        history = get_history(bid)
        results.append({"blog_id": bid, "label": blabel, "history": history, **result})
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    return jsonify({"results": results, "checked_at": now_str, "warn_hours": WARN_HOURS})


@app.route("/api/record", methods=["GET", "POST"])
def record_daily():
    today_str = datetime.now(KST).date().isoformat()
    saved = []
    for bid in BLOG_IDS:
        if not bid:
            continue
        result = fetch_blog_posts(bid)
        count = result.get("today_count", 0)
        data = {"blog_id": bid, "date": today_str, "count": count}
        supabase_request("POST", "blog_stats?on_conflict=blog_id,date", data)
        saved.append({"blog_id": bid, "date": today_str, "count": count})
    return jsonify({"status": "ok", "recorded": saved})


@app.route("/oauth")
def oauth_callback():
    """카카오 코드만 표시 - 토큰 교환 안 함"""
    code = request.args.get("code", "")
    if not code:
        return "<h2>코드가 없어요</h2>"
    return f"""
    <html><head><meta charset="UTF-8">
    <style>body{{font-family:sans-serif;max-width:600px;margin:50px auto;padding:20px}}
    .box{{background:#e8f5e9;padding:20px;border-radius:10px;word-break:break-all;font-family:monospace;font-size:12px}}
    .btn{{padding:12px 24px;background:#4CAF50;color:white;border:none;border-radius:8px;font-size:14px;cursor:pointer;margin-top:10px}}
    </style></head><body>
    <h2>✅ 코드 발급 성공!</h2>
    <p>아래 코드를 복사해서 HTML 파일 2단계에 붙여넣으세요:</p>
    <div class="box" id="code">{code}</div>
    <br>
    <button class="btn" onclick="navigator.clipboard.writeText('{code}');alert('복사됐어요!')">📋 복사</button>
    </body></html>
    """


@app.route("/kakao-auth")
def kakao_auth():
    """카카오 토큰 발급 페이지"""
    code = request.args.get("code")
    if not code:
        client_id = os.environ.get("KAKAO_CLIENT_ID", "")
        redirect_uri = os.environ.get("KAKAO_REDIRECT_URI", "")
        auth_url = f"https://kauth.kakao.com/oauth/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code"
        return f'''<a href="{auth_url}">카카오 로그인</a>'''

    # 코드로 토큰 발급
    client_id = os.environ.get("KAKAO_CLIENT_ID", "")
    redirect_uri = os.environ.get("KAKAO_REDIRECT_URI", "")
    token_url = "https://kauth.kakao.com/oauth/token"
    client_secret = os.environ.get("KAKAO_CLIENT_SECRET", "")
    data = f"grant_type=authorization_code&client_id={client_id}&redirect_uri={redirect_uri}&code={code}&client_secret={client_secret}"
    req = urllib.request.Request(token_url, data=data.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            access_token = result.get("access_token", "")
            refresh_token = result.get("refresh_token", "")
            return f"""
            <h2>✅ 토큰 발급 성공!</h2>
            <p><b>Access Token:</b><br><code>{access_token}</code></p>
            <p><b>Refresh Token:</b><br><code>{refresh_token}</code></p>
            <p>위 두 토큰을 Render 환경변수에 저장하세요!</p>
            """
    except Exception as e:
        return f"<h2>❌ 오류: {e}</h2>"


@app.route("/api/send-kakao", methods=["GET", "POST"])
def send_kakao_alert(blog_id=None, hours=None, label=None):
    """카카오톡 나에게 보내기 (자동 토큰 갱신)"""
    if blog_id is None:
        blog_id = request.args.get("blog_id", "test")
        hours = request.args.get("hours", "테스트")
        label = request.args.get("label", "")

    # 3시간 이내 같은 블로그 알림 중복 방지
    now = datetime.utcnow()
    last = alert_last_sent.get(blog_id)
    if last and (now - last).total_seconds() < ALERT_INTERVAL_HOURS * 3600:
        return jsonify({"status": "skip", "reason": f"3시간 이내 이미 발송됨"})
    alert_last_sent[blog_id] = now

    msg = f"🚨 블로그 모니터 이상 감지!\n\n대표님!!\n{label}가 {hours}시간째 글을 안 쓰고 있어요.\n확인해주세요!\n\n👉 https://blog-monitor-p4nn.onrender.com"
    result = send_kakao_message(msg)
    return jsonify(result)


@app.route("/api/daily-report")
def daily_report():
    """매일 아침 일일 리포트 카카오톡 전송 (자동 토큰 갱신)"""
    today = datetime.now(KST).date()
    yesterday = today - timedelta(days=1)
    yesterday_str = yesterday.isoformat()

    lines = []
    total = 0
    for bid, blabel in zip(BLOG_IDS, BLOG_LABELS):
        if not bid:
            continue
        result = fetch_blog_posts(bid)
        count = result.get("today_count", 0)
        total += count
        status = "⚠️" if count >= DAILY_GOAL else "✅"
        lines.append(f"{status} {blabel}: {count}개")

    msg = f"📊 블로그 모니터 일일 리포트\n{yesterday_str}\n\n" + "\n".join(lines) + f"\n\n총 발행: {total}개\n\n👉 https://blog-monitor-p4nn.onrender.com"
    result = send_kakao_message(msg)
    return jsonify(result)


@app.route("/api/check-goal")
def check_goal():
    """하루 10개 달성 감지 및 카카오 알림 (자동 토큰 갱신)"""
    today = datetime.now(KST).date()
    alerts_sent = []

    for bid, blabel in zip(BLOG_IDS, BLOG_LABELS):
        if not bid:
            continue
        result = fetch_blog_posts(bid)
        count = result.get("today_count", 0)

        # 오늘 이미 알림 보냈으면 스킵
        if goal_alert_sent.get(bid) == today:
            continue

        if count > DAILY_GOAL:
            goal_alert_sent[bid] = today
            msg = f"🚨 블로그 모니터 이상 감지!\n\n대표님!!\n{blabel}가 하루에 {count}개 작성했는데 프로그램 확인해보세요!\n\n👉 https://blog-monitor-p4nn.onrender.com"
            send_kakao_message(msg)
            alerts_sent.append(blabel)

    return jsonify({"status": "ok", "alerts_sent": alerts_sent})


@app.route("/api/token-status")
def token_status():
    """토큰 상태 확인 (디버깅용)"""
    has_access = bool(kakao_tokens.get("access_token"))
    has_refresh = bool(kakao_tokens.get("refresh_token"))
    updated = kakao_tokens.get("token_updated_at", "없음")
    return jsonify({
        "access_token_exists": has_access,
        "refresh_token_exists": has_refresh,
        "last_updated": updated,
        "access_token_preview": kakao_tokens["access_token"][:10] + "..." if has_access else "없음"
    })


@app.route("/api/refresh-token")
def manual_refresh():
    """수동 토큰 갱신 (테스트용)"""
    success = refresh_kakao_token()
    return jsonify({"refreshed": success, "token_preview": kakao_tokens["access_token"][:10] + "..." if success else "실패"})


# ========== 스케줄러 ==========

def _build_daily_lines():
    """오늘 발행 현황 라인 목록 반환"""
    lines = []
    total = 0
    for bid, blabel in zip(BLOG_IDS, BLOG_LABELS):
        if not bid:
            continue
        result = fetch_blog_posts(bid)
        count = result.get("today_count", 0)
        total += count
        status = "⚠️" if count > DAILY_GOAL else "✅"
        lines.append(f"{status} {blabel}: {count}개")
    return lines, total


def _build_weekly_lines():
    """이번 주(7일) 발행 현황 라인 목록 반환 (Supabase 기록 기준)"""
    today = datetime.now(KST).date()
    lines = []
    grand_total = 0
    for bid, blabel in zip(BLOG_IDS, BLOG_LABELS):
        if not bid:
            continue
        history = get_history(bid)   # 최근 7일
        week_total = sum(h["count"] for h in history)
        grand_total += week_total
        lines.append(f"📌 {blabel}: {week_total}개")
    return lines, grand_total


def scheduled_weekly_check():
    """3시간마다 주간 발행 현황 체크 & 카톡 발송"""
    with app.app_context():
        now = datetime.now(KST)
        today = now.date()
        daily_lines, daily_total = _build_daily_lines()
        weekly_lines, weekly_total = _build_weekly_lines()
        msg = (
            f"📡 블로그 주간 발행 현황 체크\n"
            f"{now.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"[ 오늘 발행 ]\n"
            + "\n".join(daily_lines)
            + f"\n오늘 합계: {daily_total}개\n\n"
            f"[ 주간 누적 (7일) ]\n"
            + "\n".join(weekly_lines)
            + f"\n주간 합계: {weekly_total}개\n\n"
            f"👉 https://blog-monitor-p4nn.onrender.com"
        )
        send_kakao_message(msg)
        print(f"[SCHEDULER] 주간 현황 발송 완료 ({now.strftime('%H:%M')})")


def scheduled_final_report():
    """매일 23:56 최종 일일 + 주간 리포트 발송"""
    with app.app_context():
        today = datetime.now(KST).date()
        daily_lines, daily_total = _build_daily_lines()
        weekly_lines, weekly_total = _build_weekly_lines()
        msg = (
            f"📊 블로그 모니터 최종 리포트\n"
            f"{today}\n\n"
            f"[ 오늘 최종 발행 ]\n"
            + "\n".join(daily_lines)
            + f"\n오늘 합계: {daily_total}개\n\n"
            f"[ 주간 누적 (7일) ]\n"
            + "\n".join(weekly_lines)
            + f"\n주간 합계: {weekly_total}개\n\n"
            f"👉 https://blog-monitor-p4nn.onrender.com"
        )
        send_kakao_message(msg)
        print(f"[SCHEDULER] 최종 리포트 발송 완료 ({today})")


KST_TZ = pytz.timezone("Asia/Seoul")
scheduler = BackgroundScheduler(timezone=KST_TZ)
# 3시간마다 주간 발행 현황 체크 (0시, 3시, 6시, 9시, 12시, 15시, 18시, 21시)
scheduler.add_job(scheduled_weekly_check, "cron", hour="0,3,6,9,12,15,18,21", minute=0)
# 매일 23:56 최종 일일+주간 리포트
scheduler.add_job(scheduled_final_report, "cron", hour=23, minute=56)
scheduler.start()
# ==============================


if __name__ == "__main__":
    # 서버 시작 시 Supabase에서 최신 토큰 불러오기
    load_tokens_from_supabase()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
