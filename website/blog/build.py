"""Build the Recruence blog: python website/blog/build.py

Reads every article in content/<slug>.html (a JSON header in an HTML comment, then the article body) and writes:
  <slug>/index.html   the article page (SEO and social tags, BlogPosting + FAQPage + BreadcrumbList structured data)
  index.html          the blog home: featured articles and the latest articles
  feed.xml            RSS
  sitemap.xml
  covers/<slug>.webp, covers/<slug>.jpg   cover images (needs Pillow; skipped, with a note, when it isn't installed)

Standard library only, apart from Pillow for the covers. Set BLOG_BASE_URL to where the blog is published; canonical
URLs, social cards, the feed and the sitemap use it.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE_URL = os.environ.get("BLOG_BASE_URL", "https://thalhaahamed.github.io/Recruence-AI-Interviewer/blog").rstrip("/")
REPO = "https://github.com/meetstream-ai/Recruence-AI-Interviewer"
SITE_NAME = "Recruence Blog"
AUTHOR = {
    "name": "Recruence Engineering",
    "role": "The team building Recruence",
    "bio": "We build Recruence, an open-source AI interviewer that runs structured interviews inside Google Meet, Zoom and "
           "Teams and hands back evidence-backed reports. We write about hiring workflows, voice agents and meeting APIs.",
}
NAV = [("Product", f"{REPO}#readme"), ("Docs", f"{REPO}/blob/main/docs/INTERVIEW_SETUP.md"), ("Blog", "{root}")]

ICON = {
    "x": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M17.8 3h3.1l-6.8 7.8 8 10.2h-6.3l-4.9-6.4-5.6 6.4H2.2l7.3-8.3L1.8 3h6.4l4.4 5.9L17.8 3zm-1.1 16.2h1.7L7.4 4.7H5.6l11.1 14.5z"/></svg>',
    "linkedin": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M4.98 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM3 9h4v12H3zM9 9h3.8v1.7h.05c.53-1 1.83-2.06 3.77-2.06 4.03 0 4.78 2.65 4.78 6.1V21h-4v-5.5c0-1.31-.02-3-1.83-3-1.83 0-2.11 1.43-2.11 2.9V21H9z"/></svg>',
    "link": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg>',
    "moon": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round" d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg>',
    "up": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="M12 19V5M6 11l6-6 6 6"/></svg>',
    "search": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3"/></svg>',
    "chat": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" d="M4 5h16v11H9l-5 4z"/></svg>',
    "copy": '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" d="M9 9h11v11H9zM5 15H4V4h11v1"/></svg>',
}
FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,500;12..96,600;12..96,700'
         '&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">')


def version(name: str) -> str:
    """A short fingerprint of an asset, so browsers fetch a changed stylesheet or script instead of a cached one."""
    return hashlib.sha256((HERE / "assets" / name).read_bytes()).hexdigest()[:10]


# ---------- content ----------

def load_posts() -> list[dict]:
    posts = []
    for f in sorted((HERE / "content").glob("*.html")):
        text = f.read_text(encoding="utf-8")
        m = re.match(r"\s*<!--(.*?)-->\s*(.*)", text, re.S)
        if not m:
            sys.exit(f"{f.name}: no JSON header")
        meta = json.loads(m.group(1))
        meta["body"] = m.group(2).strip()
        meta["slug"] = f.stem
        check(meta, f.name)
        posts.append(meta)
    posts.sort(key=lambda p: p["published"], reverse=True)
    return posts


def check(p: dict, name: str) -> None:
    """The publishing checklist, as far as code can check it (docs: the blog guide)."""
    problems = []
    if len(p["title"]) > 65:
        problems.append(f"title is {len(p['title'])} characters (max 65)")
    if not 50 <= len(p["seo_title"]) <= 60:
        problems.append(f"SEO title is {len(p['seo_title'])} characters (aim for 50-60)")
    d = p["description"]
    if len(d) >= 160 or not d.split()[0] in ("Explore", "Discover", "Learn", "See", "Find"):
        problems.append("meta description must be under 160 characters and start with Explore, Discover, Learn, See or Find out")
    if not 120 <= len(p["excerpt"]) <= 165:
        problems.append(f"excerpt is {len(p['excerpt'])} characters (140-160)")
    if p["keyword"].lower() not in p["title"].lower():
        problems.append("the primary keyword isn't in the title")
    body = p["body"]
    if len(re.findall(r"<h2[ >]", body)) != 7:
        problems.append(f"{len(re.findall(r'<h2[ >]', body))} H2 sections (the guide asks for seven)")
    if "<h1" in body:
        problems.append("the body has its own H1")
    if not 4 <= len(p["faq"]) <= 5:
        problems.append(f"{len(p['faq'])} FAQ questions (4-5)")
    if "Let&#x27;s get started!" not in body and "Let's get started!" not in body:
        problems.append("the introduction doesn't end with \"Let's get started!\"")
    internal = len(re.findall(r'href="(\.\./|https://github\.com/(?:ThalhaAhamed|meetstream-ai)/Recruence)', body))
    if internal < 3:
        problems.append(f"{internal} internal links (at least 3)")
    words = len(re.sub(r"<[^>]+>", " ", body).split())
    p["words"] = words + sum(len((q + " " + a).split()) for q, a in p["faq"])
    p["minutes"] = max(1, round(p["words"] / 230))
    if problems:
        sys.exit(f"{name}: " + "; ".join(problems))


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>", "", text).lower()).strip("-")


def with_ids(body: str) -> str:
    """Give every H2 and H3 an id for the contents list and links."""
    def add(m):
        tag, attrs, inner = m.group(1), m.group(2), m.group(3)
        if "id=" in attrs:
            return m.group(0)
        return f'<{tag}{attrs} id="{slugify(inner)}">{inner}</{tag}>'
    return re.sub(r"<(h[23])([^>]*)>(.*?)</\1>", add, body, flags=re.S)


def nice_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def esc(text: str) -> str:
    return html.escape(text, quote=True)


# ---------- page parts ----------

def head(title: str, description: str, canonical: str, image: str, kind: str, extra: str = "", root: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{canonical}">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta name="theme-color" content="#ff6b00">
<meta property="og:site_name" content="{SITE_NAME}">
<meta property="og:type" content="{kind}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{canonical}">
<meta property="og:image" content="{image}">
<meta property="og:image:width" content="1600">
<meta property="og:image:height" content="900">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(title)}">
<meta name="twitter:description" content="{esc(description)}">
<meta name="twitter:image" content="{image}">
<link rel="icon" type="image/png" href="{root}assets/mark-64.png">
<link rel="alternate" type="application/rss+xml" title="{SITE_NAME}" href="{BASE_URL}/feed.xml">
{FONTS}
<link rel="stylesheet" href="{root}assets/blog.css?v={version("blog.css")}">
<script>try{{var t=localStorage.getItem("rc-blog-theme");if(t)document.documentElement.dataset.theme=t}}catch(e){{}}</script>
{extra}
</head>"""


def header(root: str, current: str = "Blog") -> str:
    links = "".join(f'<a href="{href.format(root=root)}"{" aria-current=page" if label == current else ""}{" class=keep" if label == "Blog" else ""}>{label}</a>'
                    for label, href in NAV)
    return f"""<body>
<a class="skip" href="#main">Skip to content</a>
<header class="site-head"><div class="wrap">
  <a class="brand" href="{root}"><img src="{root}assets/mark.png" alt="" width="32" height="32"><b>Recruence</b><small>Blog</small></a>
  <nav class="nav" aria-label="Main">{links}
    <button class="icon-btn" type="button" data-theme-toggle aria-label="Switch between light and dark">{ICON['moon']}</button>
    <a class="btn btn-primary" href="{REPO}/releases/latest">Download</a>
  </nav>
</div></header>"""


def footer(root: str) -> str:
    year = datetime.now(timezone.utc).year
    return f"""<footer class="site-foot"><div class="wrap">
  <a class="brand" href="{root}"><img src="{root}assets/mark.png" alt="" width="26" height="26"><b>Recruence</b></a>
  <nav aria-label="Footer"><a href="{root}">Blog</a><a href="{REPO}#readme">Product</a><a href="{REPO}/blob/main/docs/INTERVIEW_SETUP.md">Docs</a><a href="{REPO}/releases/latest">Download</a><a href="{root}feed.xml">RSS</a></nav>
  <span>© {year} Recruence contributors · MIT licensed</span>
</div></footer>
<button class="to-top" type="button" aria-label="Back to top">{ICON['up']}</button>
<script src="{root}assets/blog.js?v={version("blog.js")}" defer></script>
</body>
</html>
"""


def avatar(root: str) -> str:
    return f'<span class="avatar"><img src="{root}assets/mark.png" alt="" width="46" height="46"></span>'


def card(p: dict, root: str, cls: str = "card-link") -> str:
    return f"""<a class="{cls}" href="{root}{p['slug']}/" data-topic="{slugify(p['category'])}">
  <div class="thumb"><img src="{root}covers/{p['slug']}.webp" alt="{esc(p['cover_alt'])}" width="1600" height="900" loading="lazy"></div>
  <div><span class="eyebrow">{esc(p['category'])}</span>
  <h3>{esc(p['title'])}</h3>
  <p>{esc(p['excerpt'])}</p>
  <div class="meta">{nice_date(p['published'])} · {p['minutes']} min read</div></div>
</a>"""


def article(p: dict, posts: list[dict]) -> str:
    root = "../"
    url = f"{BASE_URL}/{p['slug']}/"
    image = f"{BASE_URL}/covers/{p['slug']}.jpg"
    faq_html = "".join(f'<h4 id="{slugify(q)}">{esc(q)}</h4>\n<p>{a}</p>\n' for q, a in p["faq"])
    body = with_ids(p["body"]).replace("<!--faq-->", f'<h3 id="frequently-asked-questions">Frequently Asked Questions</h3>\n<div class="faq">\n{faq_html}</div>')
    related = [x for x in posts if x["slug"] != p["slug"]][:3]
    ld = [
        {"@context": "https://schema.org", "@type": "BlogPosting", "headline": p["title"], "description": p["description"],
         "image": image, "datePublished": p["published"], "dateModified": p.get("updated", p["published"]),
         "author": {"@type": "Organization", "name": AUTHOR["name"], "url": REPO},
         "publisher": {"@type": "Organization", "name": "Recruence", "logo": {"@type": "ImageObject", "url": f"{BASE_URL}/assets/mark.png"}},
         "mainEntityOfPage": url, "keywords": ", ".join([p["keyword"], *p["secondary"]]), "wordCount": p["words"]},
        {"@context": "https://schema.org", "@type": "FAQPage",
         "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": re.sub(r"<[^>]+>", "", a)}} for q, a in p["faq"]]},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Blog", "item": f"{BASE_URL}/"},
            {"@type": "ListItem", "position": 2, "name": p["title"], "item": url}]},
    ]
    extra = (f'<meta property="article:published_time" content="{p["published"]}">'
             f'<meta property="article:modified_time" content="{p.get("updated", p["published"])}">'
             + "".join(f'<meta property="article:tag" content="{esc(t)}">' for t in p["tags"])
             + "".join(f'<script type="application/ld+json">{json.dumps(x, ensure_ascii=False)}</script>' for x in ld))
    updated = p.get("updated", p["published"])
    dates = nice_date(p["published"]) + (f" · Updated {nice_date(updated)}" if updated != p["published"] else "")
    share = (f'<div class="share" aria-label="Share">'
             f'<a class="icon-btn" href="#" data-share="x" aria-label="Share on X">{ICON["x"]}</a>'
             f'<a class="icon-btn" href="#" data-share="linkedin" aria-label="Share on LinkedIn">{ICON["linkedin"]}</a>'
             f'<a class="icon-btn" href="#" data-share="copy" aria-label="Copy the link">{ICON["link"]}</a></div>')
    tags = "".join(f'<span class="tag">{esc(t)}</span>' for t in p["tags"])
    return f"""{head(p['seo_title'], p['description'], url, image, 'article', extra, root)}
{header(root)}
<div class="progress" aria-hidden="true"><span></span></div>
<main id="main">
<header class="post-head"><div class="wrap"><div class="inner">
  <a class="eyebrow" href="{root}">{esc(p['category'])}</a>
  <h1>{esc(p['title'])}</h1>
  <p class="dek">{esc(p['dek'])}</p>
</div>
<div class="byline">
  <div class="who">{avatar(root)}<div><b>{AUTHOR['name']}</b><span><time datetime="{p['published']}">{dates}</time> · {p['minutes']} min read</span></div></div>
  {share}
</div></div></header>
<div class="wrap post-grid">
  <aside class="rail rail-left" aria-label="Contents"><details open><summary class="toc-title">Contents</summary><nav class="toc"></nav></details></aside>
  <article class="prose">
{body}
    <div class="ask-ai"><span>Explore with AI</span>
      <a class="pill" href="#" data-ask="perplexity" target="_blank" rel="noopener">{ICON['search']}Ask Perplexity</a>
      <a class="pill" href="#" data-ask="chatgpt" target="_blank" rel="noopener">{ICON['chat']}Open in ChatGPT</a>
      <a class="pill" href="#" data-ask="claude">{ICON['copy']}Copy for Claude</a>
    </div>
    <div class="end-cta">
      <p class="cta-title">{esc(p['cta']['title'])}</p>
      <p>{esc(p['cta']['text'])}</p>
      <div class="row"><a class="btn btn-primary" href="{p['cta']['href']}">{esc(p['cta']['button'])}</a><a class="btn btn-ghost" href="{REPO}/blob/main/docs/INTERVIEW_SETUP.md">Read the setup guide</a></div>
    </div>
    <div class="tags-share">
      <div><p class="cta-title">Tags</p><div class="tags">{tags}</div></div>
      <div><p class="cta-title">Share</p>{share}</div>
    </div>
  </article>
  <aside class="rail rail-right" aria-label="About the author">
    <div class="author-card">{avatar(root)}<b>{AUTHOR['name']}</b><p>{AUTHOR['bio']}</p>
      <div class="links"><a href="{REPO}">GitHub</a><a href="{REPO}/releases/latest">Releases</a></div></div>
    <div class="cta-card"><b>Run your first AI interview</b><p>Desktop app for Windows, macOS and Linux, or Docker. Bring your own AI.</p>
      <a class="btn btn-primary" href="{REPO}/releases/latest">Download</a><a class="plain" href="{REPO}#readme">See how it works</a></div>
  </aside>
</div>
<section class="related" aria-labelledby="related-title"><div class="wrap">
  <h2 class="section-title" id="related-title">You might also like</h2>
  <div class="cards">{''.join(card(x, root) for x in related)}</div>
</div></section>
</main>
{footer(root)}"""


def index(posts: list[dict]) -> str:
    root = ""
    lead = next((p for p in posts if p.get("featured")), posts[0])
    side = [p for p in posts if p is not lead][:2]
    topics = sorted({p["category"] for p in posts})
    filters = '<button type="button" data-topic="all" aria-pressed="true">All</button>' + "".join(
        f'<button type="button" data-topic="{slugify(t)}" aria-pressed="false">{esc(t)}</button>' for t in topics)
    ld = {"@context": "https://schema.org", "@type": "Blog", "name": SITE_NAME, "url": f"{BASE_URL}/",
          "blogPost": [{"@type": "BlogPosting", "headline": p["title"], "url": f"{BASE_URL}/{p['slug']}/", "datePublished": p["published"]} for p in posts]}
    title = "Recruence Blog: AI Interviews and Hiring Engineering"
    desc = "Explore practical guides on AI interviewers, structured interviews, evidence-based scoring and running voice agents in Google Meet, Zoom and Teams."
    return f"""{head(title, desc, f'{BASE_URL}/', f'{BASE_URL}/covers/{lead["slug"]}.jpg', 'website', f'<script type="application/ld+json">{json.dumps(ld)}</script>', root)}
{header(root)}
<main id="main">
<section class="blog-hero"><div class="wrap">
  <span class="eyebrow">Recruence Blog</span>
  <h1>Field notes on AI interviews</h1>
  <p>How structured interviews, voice agents and evidence-based scoring actually work, from the team building Recruence.</p>
</div></section>
<section class="wrap featured" aria-label="Featured">
  <div class="lead">{card(lead, root)}</div>
  <div class="side">{''.join(card(p, root) for p in side)}</div>
</section>
<section class="latest" aria-labelledby="latest-title"><div class="wrap">
  <h2 class="section-title" id="latest-title">Latest articles</h2>
  <div class="filters" role="group" aria-label="Filter by topic">{filters}</div>
  <div class="cards">{''.join(card(p, root) for p in posts)}</div>
</div></section>
</main>
{footer(root)}"""


def feed(posts: list[dict]) -> str:
    items = "".join(f"""
  <item><title>{esc(p['title'])}</title><link>{BASE_URL}/{p['slug']}/</link><guid>{BASE_URL}/{p['slug']}/</guid>
    <pubDate>{datetime.fromisoformat(p['published']).replace(tzinfo=timezone.utc).strftime('%a, %d %b %Y 00:00:00 +0000')}</pubDate>
    <description>{esc(p['excerpt'])}</description>{''.join(f'<category>{esc(t)}</category>' for t in p['tags'])}</item>""" for p in posts)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>{SITE_NAME}</title><link>{BASE_URL}/</link>
  <description>Field notes on AI interviews from the team building Recruence.</description><language>en</language>{items}
</channel></rss>
"""


def sitemap(posts: list[dict]) -> str:
    urls = [(f"{BASE_URL}/", posts[0]["published"])] + [(f"{BASE_URL}/{p['slug']}/", p.get("updated", p["published"])) for p in posts]
    rows = "".join(f"\n  <url><loc>{u}</loc><lastmod>{d}</lastmod></url>" for u, d in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}\n</urlset>\n'


# ---------- covers ----------

def covers(posts: list[dict]) -> None:
    try:
        import art  # Pillow
    except ImportError:
        print("Pillow isn't installed: covers and the mark not redrawn (pip install pillow)")
        return
    art.draw_all(posts)


def main() -> None:
    sys.path.insert(0, str(HERE))
    posts = load_posts()
    covers(posts)
    for p in posts:
        (HERE / p["slug"]).mkdir(exist_ok=True)
        (HERE / p["slug"] / "index.html").write_text(article(p, posts), encoding="utf-8")
    (HERE / "index.html").write_text(index(posts), encoding="utf-8")
    (HERE / "feed.xml").write_text(feed(posts), encoding="utf-8")
    (HERE / "sitemap.xml").write_text(sitemap(posts), encoding="utf-8")
    for p in posts:
        print(f"{p['slug']}: {p['words']} words, {p['minutes']} min, title {len(p['title'])} chars, description {len(p['description'])} chars")


if __name__ == "__main__":
    main()
