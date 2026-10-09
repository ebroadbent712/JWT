"""The client's outfit plan as a tap-through page: the board itself up top (tap a person to jump to their pieces),
check-offs that remember themselves on the client's phone, palette, why it works and the week-before list.
Usage: python3 build_experience.py <plan.json> --out <folder>   (writes <slug>-plan.html)
Publish the HTML as an artifact and send the client the link; the PDF stays as the printable version."""
import argparse, base64, html, io, json, os, re, sys
from urllib.parse import urlparse
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
LIB = os.path.join(ROOT, "library")
sys.path.insert(0, HERE)
import build_board as bb  # noqa: E402

STORES = {"target.com": "Target", "oldnavy.gap.com": "Old Navy", "gap.com": "Gap", "gapfactory.com": "Gap Factory",
          "kohls.com": "Kohl's", "hm.com": "H&M", "walmart.com": "Walmart", "amazon.com": "Amazon", "landsend.com": "Lands' End",
          "abercrombie.com": "Abercrombie", "factory.jcrew.com": "J.Crew Factory", "jcrew.com": "J.Crew", "madewell.com": "Madewell",
          "bananarepublic.gap.com": "Banana Republic", "llbean.com": "L.L.Bean", "boden.com": "Boden", "hopeandhenry.com": "Hope & Henry",
          "hannaandersson.com": "Hanna Andersson", "janieandjack.com": "Janie and Jack", "petalandpup.com": "Petal & Pup",
          "lulus.com": "Lulus", "balticborn.com": "Baltic Born", "quince.com": "Quince", "mango.com": "Mango", "everlane.com": "Everlane",
          "dsw.com": "DSW", "nordstrom.com": "Nordstrom", "anthropologie.com": "Anthropologie", "freepeople.com": "Free People",
          "thereformation.com": "Reformation", "sezane.com": "Sézane", "shopdoen.com": "Dôen", "jacadi.us": "Jacadi",
          "rachelriley.com": "Rachel Riley", "ryleeandcru.com": "Rylee + Cru", "samedelman.com": "Sam Edelman", "clarks.com": "Clarks",
          "vince.com": "Vince", "tnuck.com": "Tuckernuck", "christydawn.com": "Christy Dawn", "fahertybrand.com": "Faherty", "littleenglish.com": "Little English", "shopdressup.com": "DressUp", "hillhousehome.com": "Hill House", "toddsnyder.com": "Todd Snyder"}
TIERS = [("everyday", "Everyday"), ("mid", "Mid"), ("splurge", "Splurge")]


def store_name(url):
    host = urlparse(url).netloc.lower()
    for dom in sorted(STORES, key=len, reverse=True):
        if host == dom or host.endswith("." + dom):
            return STORES[dom]
    return host.replace("www.", "").split(".")[0].title()


def data_uri(im, w=520, q=80):
    im = im.convert("RGB")
    if im.width > w:
        im = im.resize((w, int(im.height * w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=q, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def readable_on(hexs):
    h = hexs.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#1d1d1d" if lum > 0.55 else "#ffffff"


def contact_lines(settings, e):
    """Email always if given; the phone only as the photographer wants it used (text or call)."""
    out = []
    email = settings.get("contact_email") or (settings.get("contact") if "@" in str(settings.get("contact", "")) else "")
    if email:
        out.append(f'<span class="contact"><small>Email</small> {e(email)}</span>')
    phone = settings.get("contact_phone")
    if phone:
        how = "Text" if settings.get("texting", True) else "Call"
        out.append(f'<span class="contact"><small>{how}</small> {e(phone)}</span>')
    return "\n    ".join(out)


def build(plan_path, out_dir):
    plan = json.load(open(plan_path))
    catalog = json.load(open(os.path.join(LIB, "catalog.json")))
    settings = json.load(open(os.path.join(ROOT, "settings.json")))
    settings.update(plan.get("settings_override", {}))
    items = {it["id"]: dict(it) for it in catalog["items"]}
    missing = [i for p in plan["people"] for i in p["items"] if i not in items]
    if missing:
        sys.exit(f"Unknown item ids (not in the library): {missing}")
    smart = catalog.get("smart_links", {})
    base = smart.get("base", "https://justwearthis.co/s/") if smart.get("enabled") else None

    owned = {}
    for o in plan.get("owned", []):
        if isinstance(o, dict):
            owned[o["id"]] = o
        else:
            owned[o] = {"id": o}

    pal = plan.get("palette") or bb.board_palette(plan, items, catalog)
    anchor = pal[0]["hex"] if pal else "#2F4A3A"
    first = plan["people"][0]["items"]
    for i in first:
        if items[i]["category"] == "dress" or items[i].get("layout_role") == "hero":
            anchor = items[i].get("hex") or anchor
            break

    os.makedirs(out_dir, exist_ok=True)
    slug = bb.slugify(plan["family_name"])
    # page 1 of the board is the first thing they see; each person's panel is a tap target
    bb.build(plan_path, out_dir)  # the printable PDF, with number labels
    view_dir = os.path.join(out_dir, ".page-view")
    bb.HIDE_LABELS, bb.VIEW_COLS = True, 2
    try:
        bb.build(plan_path, view_dir)  # the same board, two people across and without labels, for a phone
    finally:
        bb.HIDE_LABELS, bb.VIEW_COLS = False, None
    board_png = os.path.join(view_dir, f"{slug}-board-preview.png")
    panels = json.load(open(os.path.join(view_dir, f"{slug}-panels.json")))
    # keep only the people grid (the page has its own header, palette and story)
    full = Image.open(board_png)
    X0 = min(pn["x"] for pn in panels); X1 = max(pn["x"] + pn["w"] for pn in panels)
    Y0 = min(pn["y"] for pn in panels); Y1 = max(pn["y"] + pn["h"] for pn in panels)
    crop = full.crop((int(X0 * full.width), int(Y0 * full.height), int(X1 * full.width), int(Y1 * full.height)))
    panels = [dict(pn, x=(pn["x"] - X0) / (X1 - X0), y=(pn["y"] - Y0) / (Y1 - Y0), w=pn["w"] / (X1 - X0), h=pn["h"] / (Y1 - Y0)) for pn in panels]
    lineup_uri = data_uri(crop, w=1300, q=86)

    pieces, numbering, uses = [], {}, {}
    for p in plan["people"]:
        for i in p["items"]:
            if i not in numbering:
                numbering[i] = len(numbering) + 1
            uses.setdefault(i, []).append(p["label"])
    img_cache = {}
    for i, n in sorted(numbering.items(), key=lambda kv: kv[1]):
        it = items[i]
        name = owned.get(i, {}).get("label") or it["name"]
        if owned.get(i, {}).get("photo"):
            try:
                im = bb.client_photo(owned[i]["photo"], owned[i].get("crop"))
            except Exception:
                im = Image.open(os.path.join(LIB, "images", it["file"]))
        else:
            im = Image.open(os.path.join(LIB, "images", it["file"]))
        img_cache[i] = data_uri(im)
        links = []
        for key, lab in TIERS:
            raw = (it.get("shop_links") or {}).get(key)
            if raw:
                links.append({"tier": lab, "store": store_name(raw), "url": f"{base}{i}-{key}" if base else raw})
        pieces.append({"id": i, "n": n, "name": name, "for": uses[i], "img": i, "owned": i in owned,
                       "links": links, "cat": it["category"]})

    people = [{"label": p["label"], "items": p["items"], "note": (p.get("note") or {}).get("text", "").replace("\n", " ")}
              for p in plan["people"]]
    data = {"slug": slug, "pieces": pieces, "people": people, "checklist": plan.get("prep_checklist", [])}

    e = html.escape
    studio = settings.get("studio_name") or "your photographer"
    website = settings.get("website") or ""
    swatches = "".join(f'<li><span class="chip" style="background:{e(sw["hex"])}"></span><span>{e(sw["name"])}</span></li>' for sw in pal)
    headline = "".join(f"<span>{e(h)}</span>" for h in plan.get("headline", [])[:3])
    names = "".join(f'<a class="spot" href="#p-{k}" style="left:{pn["x"] * 100:.2f}%;top:{pn["y"] * 100:.2f}%;width:{pn["w"] * 100:.2f}%;height:{pn["h"] * 100:.2f}%" aria-label="See {e(pn["label"])}\'s outfit"></a>'
                    for k, pn in enumerate(panels))
    tabs = '<button class="tab on" data-f="all" type="button">Everyone</button>' + "".join(
        f'<button class="tab" data-f="{k}" type="button">{e(p["label"])}</button>' for k, p in enumerate(people))
    sub = " · ".join(x for x in [plan.get("subtitle_right", ""), plan.get("dress_level", "")] if x)
    imgs_js = json.dumps(img_cache)

    page = TEMPLATE
    for k, v in {
        "{{TITLE}}": e(f"{plan['family_name'].replace('The ', '')} Outfit Plan"),
        "{{FAMILY}}": e(plan["family_name"]), "{{LOCATION}}": e(plan.get("title_right", "")), "{{SUB}}": e(sub),
        "{{STUDIO}}": e(studio), "{{WEBSITE}}": e(website),
        "{{CONTACT}}": contact_lines(settings, e), "{{ACCENT}}": anchor, "{{ACCENT_INK}}": readable_on(anchor),
        "{{LINEUP}}": lineup_uri, "{{NAMES}}": names, "{{SWATCHES}}": swatches, "{{HEADLINE}}": headline,
        "{{STORY}}": e(plan.get("story", "")), "{{WHY}}": e(plan.get("why_it_works", "")), "{{TABS}}": tabs,
        "{{DATA}}": json.dumps(data), "{{IMGS}}": imgs_js, "{{TOTAL}}": str(len(pieces)),
    }.items():
        page = page.replace(k, v)
    out = os.path.join(out_dir, f"{slug}-plan.html")
    open(out, "w").write(page)
    print(f"Client page: {out} ({os.path.getsize(out) // 1024} KB)")
    return out


TEMPLATE = r"""<title>{{TITLE}}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;800;900&family=Caveat:wght@500;600&display=swap" rel="stylesheet">
<style>
/* Layout: one phone-width column; the outfit board leads, then progress, palette, each person's pieces, the week-before list. */
:root{
  --paper:#ffffff; --card:#ffffff; --ink:#1d1d1d; --soft:#6b665e; --line:#e6e0d5; --wash:#f5f2ec;
  --accent:{{ACCENT}}; --accent-ink:{{ACCENT_INK}}; --done:#2f5e45; --done-bg:#e3eee6; --have:#5b4a2e; --have-bg:#f1e8d6;
  --sans:"Inter",system-ui,-apple-system,"Segoe UI",sans-serif; --hand:"Caveat","Bradley Hand","Segoe Print",cursive;
}
*{box-sizing:border-box} [hidden]{display:none!important}
body{color-scheme:light;background:var(--paper);color:var(--ink);font:16px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:760px;margin:0 auto;padding-inline:18px;padding-block:18px 64px;display:flex;flex-direction:column;gap:40px}
a{color:inherit}
.top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.mark{font-weight:900;letter-spacing:-.01em;font-size:15px}
.by{font-size:12px;color:var(--soft);letter-spacing:.06em;text-transform:uppercase}
.hero{display:flex;flex-direction:column;gap:6px}
.eyebrow{font-size:12px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--soft)}
h1{font-size:clamp(30px,8.4vw,44px);line-height:1.04;font-weight:800;letter-spacing:-.03em;margin:4px 0 6px;text-wrap:balance}
.where{font-size:17px;color:var(--soft)}
.hello{font-family:var(--hand);font-size:27px;line-height:1.1;color:var(--ink);margin-top:10px}
.tapline{font-size:15px;color:var(--soft)}
.stage{display:flex;flex-direction:column;gap:12px}
.lineup{position:relative;background:#fff;margin-inline:-8px}
.spot{position:absolute;border-radius:4px}
.spot:hover,.spot:focus-visible{background:rgba(0,0,0,.04);outline:none}
.lineup img{display:block;width:100%;height:auto}
.names{display:flex;flex-wrap:wrap;gap:8px}
.name{font-size:14px;font-weight:600;text-decoration:none;padding:7px 13px;border:1px solid var(--line);border-radius:999px;background:var(--card)}
.name:hover,.name:focus-visible{border-color:var(--ink)}
.hint{font-size:13px;color:var(--soft)}
.progress{display:flex;flex-direction:column;gap:10px;padding:18px;border-radius:16px;background:var(--wash)}
.ptop{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.pnum{font-size:30px;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.pnum small{font-size:15px;font-weight:500;color:var(--soft);letter-spacing:0}
.bar{display:flex;gap:3px;height:10px}
.bar i{flex:1;border-radius:3px;background:var(--line)}
.bar i.ordered{background:var(--done)} .bar i.have{background:var(--accent)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--soft)}
.legend span::before{content:"";display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px;background:var(--line)}
.legend .lo::before{background:var(--done)} .legend .lh::before{background:var(--accent)}
.colors{display:grid;grid-template-columns:minmax(0,1fr);gap:16px}
.swatches{list-style:none;margin:0;padding:0;display:flex;gap:8px}
.swatches li{display:flex;flex-direction:column;gap:6px;font-size:12px;line-height:1.25;color:var(--soft);flex:1 1 0;min-width:0;max-width:72px}
.chip{display:block;width:100%;aspect-ratio:1;border-radius:10px;box-shadow:inset 0 0 0 1px rgba(0,0,0,.08)}
.headline{display:flex;flex-direction:column;font-size:21px;font-weight:600;letter-spacing:-.01em;line-height:1.3}
.story{color:var(--soft);max-width:60ch;margin:0}
h2{font-size:13px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;margin:0;color:var(--soft)}
.tabs{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;display:flex;gap:6px;overflow-x:auto;padding-block:10px;margin-inline:-18px;padding-inline:18px;background:var(--paper);scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{font:600 14px var(--sans);color:var(--ink);background:transparent;border:1px solid var(--line);border-radius:999px;padding:8px 14px;white-space:nowrap;cursor:pointer}
.tab.on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.tab:focus-visible,.piece:focus-visible,.btn:focus-visible,.seg button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.person{display:flex;flex-direction:column;gap:14px;scroll-margin-top:70px}
.phead{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.phead h3{font-size:30px;font-weight:800;letter-spacing:-.025em;margin:0}
.note{font-family:var(--hand);font-size:24px;line-height:1;color:var(--soft)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
@media (min-width:620px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
.piece{all:unset;cursor:pointer;display:flex;flex-direction:column;gap:8px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 10px 12px;min-width:0}
.ph{background:#fff;border-radius:10px;aspect-ratio:4/5;display:grid;place-items:center;overflow:hidden;position:relative}
.ph img{max-width:88%;max-height:88%;object-fit:contain}
.num{position:absolute;left:8px;top:6px;font-size:12px;font-weight:600;color:#6b665e;font-variant-numeric:tabular-nums}
.pname{font-size:14px;font-weight:600;line-height:1.3}
.status{align-self:flex-start;font-size:12px;font-weight:600;padding:3px 9px;border-radius:999px;background:var(--wash);color:var(--soft)}
.status.ordered{background:var(--done-bg);color:var(--done)} .status.have{background:var(--have-bg);color:var(--have)}
.why p{margin:0;max-width:62ch}
.list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px}
.list label{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--line);cursor:pointer}
.list input{width:20px;height:20px;margin:2px 0 0;accent-color:var(--accent);flex:none}
.list input:checked+span{color:var(--soft);text-decoration:line-through}
.foot{display:flex;flex-direction:column;gap:4px;border-top:1px solid var(--line);padding-top:20px}
.foot .sign{font-family:var(--hand);font-size:28px}
.foot small{color:var(--soft)}
.ask{margin:4px 0 8px;max-width:52ch}
.who{font-weight:600}
.contact{display:flex;gap:8px;align-items:baseline}
.contact small{font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--soft);min-width:40px}
/* piece sheet */
.scrim{position:fixed;inset:0;background:rgba(20,16,10,.45);z-index:20;display:flex;align-items:flex-end;justify-content:center}
.sheet{background:var(--paper);width:100%;max-width:560px;border-radius:22px 22px 0 0;padding:14px 18px calc(22px + env(safe-area-inset-bottom,0px));max-height:92%;overflow:auto;display:flex;flex-direction:column;gap:16px}
@media (min-width:700px){.scrim{align-items:center}.sheet{border-radius:22px}}
.grab{width:42px;height:5px;border-radius:3px;background:var(--line);align-self:center}
.sheet .big{background:#fff;border-radius:14px;display:grid;place-items:center;height:min(44vh,360px)}
.sheet .big img{max-height:90%;max-width:90%;object-fit:contain}
.sheet h4{font-size:22px;font-weight:800;letter-spacing:-.02em;margin:0;line-height:1.2}
.for{font-size:14px;color:var(--soft)}
.shop{display:flex;flex-direction:column;gap:8px}
.btn{display:flex;justify-content:space-between;align-items:center;gap:10px;text-decoration:none;padding:13px 16px;border-radius:12px;border:1px solid var(--line);background:var(--card);font-weight:600}
.btn small{font-weight:500;color:var(--soft)}
.btn.lead{background:var(--accent);color:var(--accent-ink);border-color:transparent}
.btn.lead small{color:inherit;opacity:.8}
.nolink{font-size:13px;color:var(--soft)}
.seg{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:4px;border-radius:12px;background:var(--wash)}
.seg button{font:600 14px var(--sans);border:0;border-radius:9px;padding:11px 6px;background:transparent;color:var(--ink);cursor:pointer}
.seg button.on{background:var(--card);box-shadow:0 1px 3px rgba(0,0,0,.12)}
.close{all:unset;cursor:pointer;align-self:center;font-weight:600;font-size:14px;color:var(--soft);padding:6px 12px}
.owned{font-family:var(--hand);font-size:24px}
@media (prefers-reduced-motion:no-preference){.sheet{animation:up .22s ease-out}@keyframes up{from{transform:translateY(24px);opacity:.6}}}
</style>

<div class="wrap">
  <header class="top"><span class="mark">JUST WEAR THIS.</span><span class="by">Styled by {{STUDIO}}</span></header>

  <section class="hero">
    <span class="eyebrow">Your outfit plan</span>
    <h1>{{FAMILY}}</h1>
    <span class="where">{{LOCATION}}</span>
    <span class="where">{{SUB}}</span>
    <span class="hello">Here's what everyone's wearing.</span>
    <span class="tapline">Tap any piece to shop it.</span>
  </section>

  <section class="stage" aria-label="The family together">
    <div class="lineup"><img src="{{LINEUP}}" alt="The family outfit board">{{NAMES}}</div>
    <span class="hint">Tap anyone on the board to see their pieces.</span>
  </section>

  <section class="colors">
    <h2>Your colors</h2>
    <ul class="swatches">{{SWATCHES}}</ul>
    <div class="headline">{{HEADLINE}}</div>
    <p class="story">{{STORY}}</p>
  </section>

  <section class="progress" aria-live="polite">
    <div class="ptop"><span class="pnum" id="pnum">0 <small>of {{TOTAL}} pieces ready</small></span><span class="hint" id="pmsg">Mark each piece as you go.</span></div>
    <div class="bar" id="bar"></div>
    <div class="legend"><span class="lo">Ordered</span><span class="lh">Have it</span><span>Still need</span></div>
  </section>


  <section aria-label="Everyone's pieces">
    <div class="tabs" role="tablist">{{TABS}}</div>
    <div id="people" style="display:flex;flex-direction:column;gap:36px;margin-top:14px"></div>
  </section>

  <section class="why colors">
    <h2>Why it works</h2>
    <p>{{WHY}}</p>
  </section>

  <section class="colors">
    <h2>The week before</h2>
    <ul class="list" id="prep"></ul>
  </section>

  <footer class="foot">
    <span class="sign">Can't wait to see you!</span>
    <p class="ask">Questions about a piece, a size that isn't working, or something you'd like to swap? Reach out to {{STUDIO}} anytime. We'll figure it out together before your session.</p>
    <span class="who">{{STUDIO}}</span>
    {{CONTACT}}
    <small>{{WEBSITE}}</small>
    <small>Your printable outfit board came with this link as a PDF. Links show the look, not always the exact piece.</small>
  </footer>
</div>

<div class="scrim" id="scrim" hidden>
  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sname" id="sheet"></div>
</div>

<script>
const D = {{DATA}};
const IMG = {{IMGS}};
const KEY = "jwt-" + D.slug;
let S = {};
try { S = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { S = {}; }
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} };
const byId = Object.fromEntries(D.pieces.map(p => [p.id, p]));
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const stat = id => byId[id].owned ? "have" : (S[id] || "need");
const LABEL = {need: "Still need", ordered: "Ordered", have: "Have it"};

function render() {
  const wrap = document.getElementById("people");
  wrap.innerHTML = D.people.map((p, k) => `
    <div class="person" id="p-${k}" data-k="${k}">
      <div class="phead"><h3>${esc(p.label)}</h3>${p.note ? `<span class="note">${esc(p.note)}</span>` : ""}</div>
      <div class="grid">${p.items.map(id => { const it = byId[id], st = stat(id); return `
        <button class="piece" type="button" data-id="${id}" aria-label="${esc(it.name)}, ${LABEL[st]}">
          <span class="ph"><span class="num">${String(it.n).padStart(2, "0")}</span><img src="${IMG[id]}" alt=""></span>
          <span class="pname">${esc(it.name)}</span>
          <span class="status ${st}">${it.owned ? "You have this" : LABEL[st]}</span>
        </button>`; }).join("")}</div>
    </div>`).join("");
  wrap.querySelectorAll(".piece").forEach(b => b.addEventListener("click", () => openSheet(b.dataset.id)));
  progress();
}

function progress() {
  const ids = D.pieces.map(p => p.id);
  const sts = ids.map(stat);
  const ready = sts.filter(s => s !== "need").length;
  document.getElementById("pnum").innerHTML = `${ready} <small>of ${ids.length} pieces ready</small>`;
  document.getElementById("bar").innerHTML = sts.map(s => `<i class="${s === "need" ? "" : s}"></i>`).join("");
  const left = ids.length - ready;
  document.getElementById("pmsg").textContent = left === 0 ? "Everyone's ready. See you soon!" : left <= 3 ? `Almost there: ${left} to go.` : "Mark each piece as you go.";
}

function openSheet(id) {
  const it = byId[id], st = stat(id);
  const lead = it.links.length ? it.links[Math.min(1, it.links.length - 1)].tier : "";
  const shop = it.owned ? `<span class="owned">You already have this one.</span>` :
    (it.links.length ? `<div class="shop">${it.links.map(l => `<a class="btn ${l.tier === lead ? "lead" : ""}" href="${l.url}" target="_blank" rel="noopener">
        <span>${l.tier}</span><small>${esc(l.store)} &rsaquo;</small></a>`).join("")}</div>
      <span class="nolink">${it.links.length < 3 ? "Fewer than three options means we didn't find a great match at every price." : "Mix and match: splurge on the favorite, save on the rest."}</span>` : "");
  document.getElementById("sheet").innerHTML = `
    <span class="grab"></span>
    <div class="big"><img src="${IMG[id]}" alt="${esc(it.name)}"></div>
    <div><h4 id="sname">${String(it.n).padStart(2, "0")} &nbsp;${esc(it.name)}</h4><span class="for">For ${esc(it.for.join(", "))}</span></div>
    ${shop}
    ${it.owned ? "" : `<div class="seg" role="group" aria-label="Where this piece stands">
      ${["need", "ordered", "have"].map(s => `<button type="button" data-s="${s}" class="${st === s ? "on" : ""}" aria-pressed="${st === s}">${LABEL[s]}</button>`).join("")}</div>`}
    <button class="close" type="button" id="closeBtn">Done</button>`;
  const scrim = document.getElementById("scrim");
  scrim.hidden = false;
  document.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", () => {
    S[id] = b.dataset.s; save();
    document.querySelectorAll(".seg button").forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
    render(); filter(current);
  }));
  document.getElementById("closeBtn").addEventListener("click", closeSheet);
  document.getElementById("closeBtn").focus();
}
function closeSheet() { document.getElementById("scrim").hidden = true; }
document.getElementById("scrim").addEventListener("click", e => { if (e.target.id === "scrim") closeSheet(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeSheet(); });

let current = "all";
function filter(f) {
  current = f;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("on", t.dataset.f === f));
  document.querySelectorAll(".person").forEach(p => p.hidden = !(f === "all" || p.dataset.k === f));
}
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => filter(t.dataset.f)));
document.querySelectorAll(".spot").forEach(a => a.addEventListener("click", () => filter("all")));

document.getElementById("prep").innerHTML = D.checklist.map((c, i) => `<li><label><input type="checkbox" id="prep-${i}" ${S["prep-" + i] ? "checked" : ""}><span>${esc(c)}</span></label></li>`).join("");
document.querySelectorAll("#prep input").forEach(cb => cb.addEventListener("change", () => { S[cb.id] = cb.checked; save(); }));
render();
</script>
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--out", default="output")
    a = ap.parse_args()
    build(a.plan, a.out)
