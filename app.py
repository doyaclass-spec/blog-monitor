from flask import Flask, render_template, jsonify, request
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import os
import json

app = Flask(__name__)

KST = timezone(timedelta(hours=9))
WARN_HOURS = 6

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
    """카카오톡 나에게 보내기"""
    token = os.environ.get("KAKAO_ACCESS_TOKEN", "")
    if not token:
        return jsonify({"status": "skip", "reason": "토큰 없음"})

    if blog_id is None:
        blog_id = request.args.get("blog_id", "test")
        hours = request.args.get("hours", "테스트")
        label = request.args.get("label", "")

    msg = f"⚠ 블로그 이상 감지!\n\n{blog_id} ({label})\n마지막 글: {hours}시간 전\n기준 초과: {WARN_HOURS}시간\n\n확인: https://blog-monitor-p4nn.onrender.com"
    data = json.dumps({
        "object_type": "text",
        "text": msg,
        "link": {"web_url": "https://blog-monitor-p4nn.onrender.com", "mobile_web_url": "https://blog-monitor-p4nn.onrender.com"}
    }).encode()

    req = urllib.request.Request(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        data=urllib.parse.urlencode({"template_object": json.dumps({
            "object_type": "text",
            "text": msg,
            "link": {"web_url": "https://blog-monitor-p4nn.onrender.com"}
        })}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "reason": str(e)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
