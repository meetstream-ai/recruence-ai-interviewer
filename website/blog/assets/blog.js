// Recruence blog: contents list with the current section, reading progress, back to top, share, "explore with AI",
// theme and the index's topic filter. Everything works without it; this only adds the conveniences.
"use strict";
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const prose = $(".prose");

  function toast(text) {
    const t = document.createElement("div");
    t.className = "toast"; t.setAttribute("role", "status"); t.textContent = text;
    document.body.append(t); setTimeout(() => t.remove(), 2200);
  }
  async function copy(text, done) {
    try { await navigator.clipboard.writeText(text); toast(done); }
    catch (e) { window.prompt("Copy this:", text); }
  }

  // Theme: the system setting unless the reader picked one.
  const root = document.documentElement;
  try { const t = localStorage.getItem("rc-blog-theme"); if (t) root.dataset.theme = t; } catch (e) { /* not kept */ }
  $$("[data-theme-toggle]").forEach((b) => b.addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("rc-blog-theme", root.dataset.theme); } catch (e) { /* not kept */ }
  }));

  // Contents: the article's H2s; the FAQ heading (an H3 in the last section) with its questions nested under it.
  const toc = $(".toc");
  if (prose && toc) {
    const list = document.createElement("ol");
    let faq = null;
    const item = (h) => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = `#${h.id}`; a.textContent = h.textContent; a.dataset.target = h.id;
      li.append(a); return li;
    };
    $$("h2[id], h3#frequently-asked-questions, .faq h4[id]", prose).forEach((h) => {
      if (h.tagName === "H2") list.append(item(h));
      else if (h.tagName === "H3") { const li = item(h); faq = document.createElement("ol"); li.append(faq); list.append(li); }
      else if (faq) faq.append(item(h));
    });
    toc.append(list);
    const links = $$("a", toc);
    const heads = links.map((a) => document.getElementById(a.dataset.target));
    const mark = () => {
      let current = heads[0];
      for (const h of heads) if (h.getBoundingClientRect().top < 140) current = h;
      links.forEach((a) => a.classList.toggle("active", a.dataset.target === current.id));
    };
    document.addEventListener("scroll", mark, { passive: true }); mark();
  }

  // Reading progress and back to top.
  const bar = $(".progress span");
  const top = $(".to-top");
  const onScroll = () => {
    if (bar && prose) {
      const r = prose.getBoundingClientRect();
      const total = r.height - innerHeight * 0.6;
      bar.style.width = `${Math.max(0, Math.min(100, (-r.top / Math.max(total, 1)) * 100))}%`;
    }
    if (top) top.classList.toggle("show", scrollY > 900);
  };
  document.addEventListener("scroll", onScroll, { passive: true }); onScroll();
  if (top) top.addEventListener("click", () => scrollTo({ top: 0, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }));

  // Share and explore with AI.
  const url = location.href.split("#")[0];
  const title = document.title;
  $$("[data-share]").forEach((b) => b.addEventListener("click", (e) => {
    const kind = b.dataset.share;
    if (kind === "copy") { e.preventDefault(); copy(url, "Link copied"); return; }
    if (kind === "native" && navigator.share) { e.preventDefault(); navigator.share({ title, url }).catch(() => {}); return; }
    const target = kind === "x" ? `https://x.com/intent/post?text=${encodeURIComponent(title)}&url=${encodeURIComponent(url)}`
      : kind === "linkedin" ? `https://www.linkedin.com/sharing/share-offsite/?url=${encodeURIComponent(url)}` : null;
    if (target) { e.preventDefault(); window.open(target, "_blank", "noopener,noreferrer,width=640,height=600"); }
  }));
  $$("[data-ask]").forEach((b) => {
    const prompt = `Read ${url} and explain its main points about "${$("h1") ? $("h1").textContent : title}". Then answer my follow-up questions.`;
    if (b.dataset.ask === "perplexity") b.href = `https://www.perplexity.ai/search?q=${encodeURIComponent(prompt)}`;
    if (b.dataset.ask === "chatgpt") b.href = `https://chatgpt.com/?q=${encodeURIComponent(prompt)}`;
    if (b.dataset.ask === "claude") b.addEventListener("click", (e) => {
      e.preventDefault();
      const text = prose ? prose.innerText.split("\nExplore with AI")[0] : "";
      copy(`${$("h1") ? $("h1").textContent : title}\n${url}\n\n${text}`, "Article copied: paste it into Claude");
    });
  });

  // Index: filter the cards by topic.
  const filters = $(".filters");
  if (filters) {
    filters.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      $$("button", filters).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      const want = b.dataset.topic;
      $$(".latest .card-link").forEach((c) => { c.hidden = want !== "all" && c.dataset.topic !== want; });
    });
  }
})();
