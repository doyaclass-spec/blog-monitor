from flask import Flask, render_template, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import os
import json
import pytz

app = Flask(__name__)

# ─── 상수 ───────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
KST_TZ = pytz.timezone("Asia/Seoul")

WARN_HOURS = int(os.environ.get("WARN_HOURS", "10"))       # 10시간 무발행 시 이상감지
ALERT_INTERVAL_HOURS = 3                                     # 같은 블로그 이상감지 최소 간격
DAILY_GOAL = int(os.environ.get("DAILY_GOAL", "10"))        # 하루 발행 초과 기준

BLOG_IDS = [os.environ.get(f"BLOG{i}", "") for i in range(1, 7)]
BLOG_LABELS = [os.environ.get(f"LABEL{i}", f"{i}호기") for i in range(1, 7)]

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
RENDER_URL = "https://blog-monitor-p4nn.onrender.com"

# ─── 메모리 상태 ─────────────────────────────────────────
alert_last_sent = {}   # {blog_id: datetime(utc)} 이상감지 마지막 발송
goal_alert_sent = {}   # {blog_id: date} 초과발행 오늘 발송 여부

kakao_tokens = {
    "access_token": os.environ.get("KAKAO_ACCESS_TOKEN", ""),
    "refresh_token": os.environ.get("KAKAO_REFRESH_TOKEN", ""),
    "updated_at": None,
}


# ─── Supabase ────────────────────────────────────────────
def supabase_request(method, path, data=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation,resolution=merge-duplicates",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[Supabase] 오류: {e}")
        return None


def save_tokens_to_supabase():
    supabase_request("POST", "kakao_tokens?on_conflict=id", {
        "id": "kakao_tokens",
        "access_token": kakao_tokens["access_token"],
        "refresh_token": kakao_tokens["refresh_token"],
        "updated_at": datetime.now(KST).isoformat(),
    })


def load_tokens_from_supabase():
    result = supabase_request("GET", "kakao_tokens?id=eq.kakao_tokens")
    if result and len(result) > 0:
        row = result[0]
        if row.get("access_token"):
            kakao_tokens["access_token"] = row["access_token"]
        if row.get("refresh_token"):
            kakao_tokens["refresh_token"] = row["refresh_token"]
        kakao_tokens["updated_at"] = row.get("updated_at")
        print("[KAKAO] Supabase에서 토큰 로드 완료")


def get_history(blog_id):
    """최근 7일 발행 기록 (Supabase)"""
    today = datetime.now(KST).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    result = supabase_request("GET", f"blog_stats?blog_id=eq.{blog_id}&date=gte.{dates[0]}&order=date.asc")
    history = {d: 0 for d in dates}
    if result:
        for row in result:
            if row["date"] in history:
                history[row["date"]] = row["count"]
    return [{"date": d, "count": history[d]} for d in dates]


# ─── 카카오 토큰 ──────────────────────────────────────────
def refresh_kakao_token():
    """Refresh Token으로 Access Token 갱신"""
    refresh_token = kakao_tokens.get("refresh_token", "")
    client_id = os.environ.get("KAKAO_CLIENT_ID", "")
    if not refresh_token or not client_id:
        print("[KAKAO] 갱신 불가: refresh_token 또는 client_id 없음")
        return False

    params = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token}
    client_secret = os.environ.get("KAKAO_CLIENT_SECRET", "")
    if client_secret:
        params["client_secret"] = client_secret

    req = urllib.request.Request(
        "https://kauth.kakao.com/oauth/token",
        data=urllib.parse.urlencode(params).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        if "access_token" in result:
            kakao_tokens["access_token"] = result["access_token"]
            kakao_tokens["updated_at"] = datetime.now(KST).isoformat()
            if "refresh_token" in result:
                kakao_tokens["refresh_token"] = result["refresh_token"]
            save_tokens_to_supabase()
            print("[KAKAO] ✅ 토큰 갱신 성공")
            return True
        print(f"[KAKAO] ❌ 갱신 실패: {result}")
        return False
    except Exception as e:
        print(f"[KAKAO] ❌ 갱신 오류: {e}")
        return False


def send_kakao_message(msg):
    """카카오 나에게 보내기 (401 시 자동 갱신 후 재시도)"""
    for attempt in range(2):
        token = kakao_tokens.get("access_token", "")
        if not token:
            print("[KAKAO] Access Token 없음, 갱신 시도")
            if not refresh_kakao_token():
                return {"status": "error", "reason": "토큰 없음"}
            token = kakao_tokens.get("access_token", "")

        data = urllib.parse.urlencode({
            "template_object": json.dumps({
                "object_type": "text",
                "text": msg,
                "link": {"web_url": RENDER_URL},
            })
        }).encode()
        req = urllib.request.Request(
            "https://kapi.kakao.com/v2/api/talk/memo/default/send",
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                return {"status": "ok", "result": result}
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                print("[KAKAO] 401 토큰 만료 → 자동 갱신 후 재시도")
                kakao_tokens["access_token"] = ""
                refresh_kakao_token()
                continue
            return {"status": "error", "code": e.code, "reason": e.read().decode()[:200]}
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    return {"status": "error", "reason": "재시도 실패"}


# ─── RSS 파싱 ──────────────────────────────────────────────
def fetch_blog_posts(blog_id):
    url = f"https://rss.blog.naver.com/{blog_id}.xml"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()
    except Exception as e:
        return {"ok": False, "error": str(e)[:80], "posts": [], "today_count": 0}

    try:
        root = ET.fromstring(xml_data)
        items = root.findall(".//item")
    except Exception:
        return {"ok": False, "error": "XML 파싱 오류", "posts": [], "today_count": 0}

    if not items:
        return {"ok": False, "error": "글 없음", "posts": [], "today_count": 0}

    now = datetime.now(KST)
    today_str = now.date().isoformat()
    posts = []
    today_count = 0

    for item in items[:15]:
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub_date_str = item.findtext("pubDate") or ""
        try:
            dt = parsedate_to_datetime(pub_date_str).astimezone(KST)
            elapsed = (now - dt).total_seconds() / 3600
            if dt.date().isoformat() == today_str:
                today_count += 1
            h, m = int(elapsed), int((elapsed % 1) * 60)
            if elapsed < 1:
                lbl = f"{m}분 전"
            elif elapsed < 24:
                lbl = f"{h}시간 {m}분 전"
            else:
                lbl = f"{int(elapsed // 24)}일 전"
            posts.append({"title": title, "link": link, "hoursAgo": round(elapsed, 1), "timeLabel": lbl})
        except Exception:
            continue

    return {"ok": True, "posts": posts, "today_count": today_count}


# ─── 알림 핵심 로직 ────────────────────────────────────────
def _run_auto_check():
    """이상 감지(10시간 무발행) + 초과발행(10개 초과) 통합 체크"""
    now_utc = datetime.utcnow()
    today = datetime.now(KST).date()
    sent, skipped = [], []

    for bid, blabel in zip(BLOG_IDS, BLOG_LABELS):
        if not bid:
            continue
        result = fetch_blog_posts(bid)
        if not result.get("ok"):
            continue

        # ① 이상 감지: 10시간 이상 무발행
        posts = result.get("posts", [])
        hours_ago = posts[0].get("hoursAgo", 9999) if posts else 9999
        if hours_ago >= WARN_HOURS:
            last = alert_last_sent.get(bid)
            if last and (now_utc - last).total_seconds() < ALERT_INTERVAL_HOURS * 3600:
                skipped.append({"blog": blabel, "reason": "3시간 중복방지"})
            else:
                alert_last_sent[bid] = now_utc
                hours_int = int(hours_ago)
                send_kakao_message(
                    f"🚨 블로그 모니터 이상 감지!\n\n"
                    f"대표님!!\n"
                    f"{blabel}가 {hours_int}시간째 글을 안 쓰고 있어요.\n"
                    f"확인해주세요!\n\n"
                    f"👉 {RENDER_URL}"
                )
                sent.append({"blog": blabel, "type": "이상감지", "hours": hours_int})

        # ② 초과발행 감지: 하루 10개 초과 시 즉시 알림 (오늘 1회)
        count = result.get("today_count", 0)
        if count > DAILY_GOAL and goal_alert_sent.get(bid) != today:
            goal_alert_sent[bid] = today
            send_kakao_message(
                f"🚨 블로그 모니터 이상 감지!\n\n"
                f"대표님!!\n"
                f"{blabel}가 하루에 {count}개 작성했는데\n"
                f"프로그램 확인해주세요!\n\n"
                f"👉 {RENDER_URL}"
            )
            sent.append({"blog": blabel, "type": "초과발행", "count": count})

    return sent, skipped


def _build_report_lines():
    """오늘 발행 현황 + 주간 누적 라인 반환"""
    daily_lines, daily_total = [], 0
    weekly_lines, weekly_total = [], 0

    for bid, blabel in zip(BLOG_IDS, BLOG_LABELS):
        if not bid:
            continue
        result = fetch_blog_posts(bid)
        count = result.get("today_count", 0)
        daily_total += count
        status = "⚠️" if count > DAILY_GOAL else "✅"
        daily_lines.append(f"{status} {blabel}: {count}개")

        history = get_history(bid)
        w = sum(h["count"] for h in history)
        weekly_total += w
        # Supabase 없으면 주간 데이터가 0으로 표시됨 (정상)
        weekly_lines.append(f"📌 {blabel}: {w}개")

    return daily_lines, daily_total, weekly_lines, weekly_total


# ─── 스케줄러 작업 ─────────────────────────────────────────
def job_auto_check():
    """30분마다: 이상감지 + 초과발행 체크"""
    with app.app_context():
        sent, skipped = _run_auto_check()
        print(f"[SCHEDULER] auto_check — 발송:{len(sent)} 스킵:{len(skipped)}")


def job_weekly_report():
    """3시간마다 1분: 주간 발행 현황 카톡"""
    with app.app_context():
        now = datetime.now(KST)
        daily_lines, daily_total, weekly_lines, weekly_total = _build_report_lines()

        if not daily_lines:
            print("[SCHEDULER] weekly_report — 블로그 없음, 스킵")
            return

        msg = (
            f"📡 블로그 주간 발행 현황\n"
            f"{now.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"[ 오늘 발행 ]\n"
            + "\n".join(daily_lines)
            + f"\n오늘 합계: {daily_total}개\n\n"
            f"[ 주간 누적 (7일) ]\n"
            + "\n".join(weekly_lines)
            + f"\n주간 합계: {weekly_total}개\n\n"
            f"👉 {RENDER_URL}"
        )
        send_kakao_message(msg)
        print(f"[SCHEDULER] weekly_report 발송 완료")


def job_final_report():
    """매일 23:56: 최종 일일+주간 리포트"""
    with app.app_context():
        today = datetime.now(KST).date()
        daily_lines, daily_total, weekly_lines, weekly_total = _build_report_lines()

        if not daily_lines:
            print("[SCHEDULER] final_report — 블로그 없음, 스킵")
            return

        msg = (
            f"📊 블로그 최종 리포트\n"
            f"{today}\n\n"
            f"[ 오늘 최종 발행 ]\n"
            + "\n".join(daily_lines)
            + f"\n오늘 합계: {daily_total}개\n\n"
            f"[ 주간 누적 (7일) ]\n"
            + "\n".join(weekly_lines)
            + f"\n주간 합계: {weekly_total}개\n\n"
            f"👉 {RENDER_URL}"
        )
        send_kakao_message(msg)
        print(f"[SCHEDULER] final_report 발송 완료 ({today})")


# ─── Flask 라우트 ─────────────────────────────────────────
@app.route("/")
def index():
    blogs = [{"id": bid, "label": blabel, "num": i + 1}
             for i, (bid, blabel) in enumerate(zip(BLOG_IDS, BLOG_LABELS)) if bid]
    return render_template("index.html", blogs=blogs, warn_hours=WARN_HOURS)


@app.route("/api/check")
def api_check():
    """화면용 블로그 데이터"""
    results = []
    for bid, blabel in zip(BLOG_IDS, BLOG_LABELS):
        if not bid:
            continue
        result = fetch_blog_posts(bid)
        history = get_history(bid)
        results.append({"blog_id": bid, "label": blabel, "history": history, **result})
    return jsonify({"results": results, "checked_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"), "warn_hours": WARN_HOURS})


@app.route("/api/record", methods=["GET", "POST"])
def api_record():
    """Supabase에 오늘 발행수 기록 (cron-job에서 자정에 호출)"""
    today_str = datetime.now(KST).date().isoformat()
    saved = []
    for bid in BLOG_IDS:
        if not bid:
            continue
        result = fetch_blog_posts(bid)
        count = result.get("today_count", 0)
        supabase_request("POST", "blog_stats?on_conflict=blog_id,date",
                         {"blog_id": bid, "date": today_str, "count": count})
        saved.append({"blog_id": bid, "date": today_str, "count": count})
    return jsonify({"status": "ok", "recorded": saved})


@app.route("/api/auto-check")
def api_auto_check():
    """이상감지+초과발행 통합 체크 (cron-job에서 30분마다 호출)"""
    sent, skipped = _run_auto_check()
    return jsonify({"status": "ok", "sent": sent, "skipped": skipped})


@app.route("/api/send-kakao")
def api_send_kakao_test():
    """카카오 연결 테스트"""
    result = send_kakao_message(f"✅ 블로그 모니터 카카오 알림 테스트!\n\n연결이 정상입니다 👍\n\n👉 {RENDER_URL}")
    return jsonify(result)


@app.route("/api/token-status")
def api_token_status():
    """토큰 상태 확인"""
    has_access = bool(kakao_tokens.get("access_token"))
    has_refresh = bool(kakao_tokens.get("refresh_token"))
    return jsonify({
        "access_token": "✅ 있음" if has_access else "❌ 없음",
        "refresh_token": "✅ 있음" if has_refresh else "❌ 없음",
        "updated_at": kakao_tokens.get("updated_at", "없음"),
        "preview": kakao_tokens["access_token"][:10] + "..." if has_access else "없음",
    })


@app.route("/api/refresh-token")
def api_refresh_token():
    """수동 토큰 갱신"""
    success = refresh_kakao_token()
    return jsonify({"refreshed": success})


@app.route("/oauth")
def oauth_callback():
    """카카오 OAuth 콜백 — 코드 화면에 표시"""
    code = request.args.get("code", "")
    if not code:
        return "<h2>코드가 없어요.</h2>"
    return f"""
    <html><head><meta charset="UTF-8">
    <style>body{{font-family:sans-serif;max-width:600px;margin:50px auto;padding:20px}}
    .box{{background:#e8f5e9;padding:20px;border-radius:10px;word-break:break-all;font-size:13px}}
    .btn{{padding:10px 20px;background:#4CAF50;color:white;border:none;border-radius:8px;cursor:pointer;margin-top:10px}}
    </style></head><body>
    <h2>✅ 코드 발급 성공!</h2>
    <p>아래 코드를 복사해서 HTML 파일 2단계에 붙여넣으세요:</p>
    <div class="box">{code}</div><br>
    <button class="btn" onclick="navigator.clipboard.writeText('{code}');alert('복사됐어요!')">📋 복사</button>
    </body></html>
    """


@app.route("/kakao-auth")
def kakao_auth():
    """카카오 토큰 발급 페이지"""
    code = request.args.get("code")
    client_id = os.environ.get("KAKAO_CLIENT_ID", "")
    redirect_uri = os.environ.get("KAKAO_REDIRECT_URI", "")
    client_secret = os.environ.get("KAKAO_CLIENT_SECRET", "")

    if not code:
        auth_url = f"https://kauth.kakao.com/oauth/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code"
        return f'<a href="{auth_url}">카카오 로그인</a>'

    data = f"grant_type=authorization_code&client_id={client_id}&redirect_uri={redirect_uri}&code={code}&client_secret={client_secret}"
    req = urllib.request.Request(
        "https://kauth.kakao.com/oauth/token",
        data=data.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
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


# ─── 시작 ──────────────────────────────────────────────────
# Supabase에서 최신 토큰 로드 (Gunicorn 포함)
load_tokens_from_supabase()

# 스케줄러 등록
scheduler = BackgroundScheduler(timezone=KST_TZ)
scheduler.add_job(job_auto_check,    "cron", minute="0,30")                              # 매 30분 이상감지
scheduler.add_job(job_weekly_report, "cron", hour="0,3,6,9,12,15,18,21", minute=1)      # 3시간마다 주간현황 (1분에 실행, 정각 auto_check와 겹침 방지)
scheduler.add_job(job_final_report,  "cron", hour=23, minute=56)                         # 매일 23:56 최종리포트
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
