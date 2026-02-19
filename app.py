from flask import Flask, render_template, jsonify, request
import urllib.request
import json
from datetime import datetime, timezone, timedelta
import os

app = Flask(__name__)

KST = timezone(timedelta(hours=9))
WARN_HOURS = 12

# 블로그 ID 목록 (설정 페이지에서 변경 가능)
BLOG_IDS = [
    os.environ.get("BLOG1", ""),
    os.environ.get("BLOG2", ""),
    os.environ.get("BLOG3", ""),
    os.environ.get("BLOG4", ""),
    os.environ.get("BLOG5", ""),
    os.environ.get("BLOG6", ""),
]

NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")


def fetch_blog_posts(blog_id, client_id, client_secret):
    query = blog_id
    url = f"https://openapi.naver.com/v1/search/blog.json?query={urllib.parse.quote(query)}&display=10&sort=date"
    
    import urllib.parse
    url = f"https://openapi.naver.com/v1/search/blog.json?query={urllib.parse.quote(blog_id)}&display=10&sort=date"

    req = urllib.request.Request(url, headers={
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
        "User-Agent": "Mozilla/5.0"
    })

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)[:80], "posts": []}

    now = datetime.now(KST)
    posts = []

    for item in data.get("items", []):
        link = item.get("link", "") + item.get("bloggername", "")
        # 해당 블로그 ID가 포함된 글만 필터링
        if blog_id.lower() not in item.get("link", "").lower() and \
           blog_id.lower() not in item.get("bloggerlink", "").lower():
            continue

        title = item.get("title", "").replace("<b>","").replace("</b>","")
        post_date_str = item.get("postdate", "")  # YYYYMMDD

        try:
            post_dt = datetime(
                int(post_date_str[:4]),
                int(post_date_str[4:6]),
                int(post_date_str[6:8]),
                9, 0, 0, tzinfo=KST
            )
            elapsed_hours = (now - post_dt).total_seconds() / 3600

            h = int(elapsed_hours)
            m = int((elapsed_hours % 1) * 60)
            if elapsed_hours < 1:
                time_label = f"{m}분 전"
            elif elapsed_hours < 24:
                time_label = f"{h}시간 전"
            else:
                d = int(elapsed_hours // 24)
                time_label = f"{d}일 전"

            posts.append({
                "title": title,
                "hoursAgo": round(elapsed_hours, 1),
                "timeLabel": time_label,
                "date": f"{post_date_str[:4]}-{post_date_str[4:6]}-{post_date_str[6:8]}"
            })
        except:
            continue

        if len(posts) >= 5:
            break

    return {"ok": True, "posts": posts}


@app.route("/")
def index():
    blog_ids = [b for b in BLOG_IDS if b]
    return render_template("index.html", blog_ids=blog_ids)


@app.route("/api/check")
def check_all():
    client_id     = NAVER_CLIENT_ID
    client_secret = NAVER_CLIENT_SECRET
    blog_ids      = [b for b in BLOG_IDS if b]

    if not client_id or not client_secret:
        return jsonify({"error": "API 키가 설정되지 않았습니다."}), 400

    results = []
    for blog_id in blog_ids:
        result = fetch_blog_posts(blog_id, client_id, client_secret)
        results.append({
            "blog_id": blog_id,
            **result
        })

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    return jsonify({"results": results, "checked_at": now_str})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
