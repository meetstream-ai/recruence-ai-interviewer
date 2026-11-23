# Recruence blog

A static blog: plain HTML, one stylesheet, one script. Nothing runs on a server.

```
content/<slug>.html   an article: a JSON header in an HTML comment, then the body
assets/               blog.css, blog.js, mark.png (the Recruence mark on an orange tile), robot.png (source for the artwork)
images/               screenshots used inside articles (WebP)
covers/               cover images (WebP for pages, JPEG for social cards)
art.py                draws the covers and the mark (Pillow), in the blog's house style
build.py              writes <slug>/index.html, index.html, feed.xml and sitemap.xml
```

## Build and preview

```bash
pip install pillow                       # only needed to redraw the covers
python website/blog/build.py
python -m http.server 8510 --directory website   # then open http://127.0.0.1:8510/blog/
```

`BLOG_BASE_URL` sets the published address that canonical links, social cards, the feed and the sitemap use. The default is `https://thalhaahamed.github.io/Recruence-AI-Interviewer/blog`. Pages link to each other with relative paths, so the blog works under any folder.

## Writing an article

Copy an existing file in `content/` and change its header:

| Field | Rule (checked by `build.py`) |
|---|---|
| `title` | The H1. Under 65 characters, containing `keyword` |
| `seo_title` | 50–60 characters |
| `description` | Meta description. Under 160 characters, starting with Explore, Discover, Learn, See or Find out |
| `excerpt` | The card summary, about 140–160 characters |
| `keyword`, `secondary` | The primary keyword and 3–6 supporting ones |
| `category`, `tags` | The card label and 2–5 tags |
| `faq` | 4–5 `[question, answer]` pairs. They're rendered where `<!--faq-->` sits, and as FAQPage structured data |
| `cta` | The soft call to action at the end |
| `featured` | `true` on the one article shown large on the blog home |

The body uses H2 for the seven sections and never an H1. The introduction is 3–4 paragraphs, answers the question within the first 100 words, and ends with "In this article, we'll explore…" and "Let's get started!". Include at least three internal links (other articles use `../<slug>/`), primary sources for facts, and alt text on every image. `build.py` refuses to build an article that breaks the checkable rules.

The look follows the MeetStream blog: white pages, zinc greys, `#FF6B00` orange accents, Bricolage Grotesque. Covers are dark with an orange floor glow, a monospace category label, a white title and a diagram drawn for that article (add one to `DIAGRAMS` in `art.py` for a new article; never a stock image).

Diagrams are HTML inside `<figure class="figure"><div class="card">…</div></figure>`: `.flow` with `.step` items for a sequence, `.lanes` for layers. `.answer` holds a short answer and `.note` a field note.
