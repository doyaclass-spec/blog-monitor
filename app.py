from flask import Flask, render_template, jsonify
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import os

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


def fetch_blog_posts(blog_id):
    url = f"https://rss.blog.naver.com/{blog_id}.xml"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()
    except Exception as e:
        return {"ok": False, "error": str(e)[:80], "posts": []}

    try:
        root = ET.fromstring(xml_data)
        items = root.findall(".//item")
    except:
        return {"ok": False, "error": "XML 파싱 오류", "posts": []}

    if not items:
        return {"ok": False, "error": "글 없음", "posts": []}

    now = datetime.now(KST)
    posts = []

    for item in items[:5]:
        title = item.findtext("title") or ""
        pub_date_str = item.findtext("pubDate") or ""
        try:
            dt = parsedate_to_datetime(pub_date_str).astimezone(KST)
            elapsed = (now - dt).total_seconds() / 3600
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
                "timeLabel": lbl
            })
        except:
            continue

    return {"ok": True, "posts": posts}


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
        results.append({"blog_id": bid, "label": blabel, **result})

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    return jsonify({"results": results, "checked_at": now_str, "warn_hours": WARN_HOURS})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
