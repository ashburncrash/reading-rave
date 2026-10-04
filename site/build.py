#!/usr/bin/env python3
"""Build site/index.html from the club folder.

Inputs
  rsvps/reader-*.md      who is in the club (ACCEPTED readers = the 12 rotating seats)
  library/books/*.md     the shelf (~50 books, each with a verified quote + a DJ request)
  dj/setlist.md          Set 1 (Boiler Room) + Set 2 (Cercle). Set 3 is generated
                         in the browser from whichever books are checked out this hour.

Rotation is a pure function of the clock (UTC hour index), so every visitor sees the
same 12 books at the same time and nothing has to run in the background.

Re-run after adding books / guests / a new DJ set:  python3 site/build.py
One-time migration of opening-night notes into the library:  python3 site/build.py --migrate
"""
import hashlib, json, re, sys
from pathlib import Path

CLUB = Path(__file__).resolve().parent.parent
SITE = CLUB / "site"
BOOKS = CLUB / "library" / "books"

TEMPLATES = {"deep", "garage", "house", "afro", "breaks", "acid", "techno", "hard",
             "melodic", "trance", "dnb", "dark"}
TAG_MOOD = {"warm-up": "deep", "warmup": "deep", "build": "house", "peak": "acid",
            "sunrise": "trance", **{t: t for t in TEMPLATES}}
SECTION_MOOD = [("warm", "deep"), ("build", "house"), ("peak", "acid"), ("sunrise", "trance"), ("cool", "trance")]


def clean(s):
    return re.sub(r"[*_]", "", s).strip().strip('"“”«»').strip()


def seed_of(artist, title):
    h = int(hashlib.md5(f"{artist}{title}".encode()).hexdigest(), 16)
    return 36 + h % 10, h % 997


def field(text, label):
    m = re.search(r"\*\*" + label + r":\*\*\s*(.+)", text)
    return m.group(1).strip() if m else None


def parse_book(path):
    """Parse one library/books/*.md (also tolerates the opening-night reading-note format)."""
    text = path.read_text()
    lines = text.splitlines()
    d = {"id": path.stem}

    book = field(text, "Book") or ""
    book = re.split(r"\s+\(Project Gutenberg|\.\s+Text from", book)[0]
    book = re.sub(r"\s*\(\d{4}\)", "", book)
    title, author = (book.split(" — ", 1) if " — " in book else
                     book.split(" by ", 1) if " by " in book else (book, ""))
    d["book"], d["author"] = clean(title), clean(author)
    d["original"] = field(text, "Original title")
    d["language"] = clean(field(text, "Language") or "English")
    d["year"] = clean(field(text, "Year") or "") or None
    d["section"] = clean(field(text, "Section read") or "")
    d["translation"] = field(text, "Translation")
    d["readBy"] = clean(field(text, "First read by") or "") or None

    src = field(text, "Source") or ""
    u = re.search(r"https?://\S+", src) or re.search(r"https?://www\.gutenberg\.org/\S+", text)
    d["source"] = u.group(0).rstrip(").,") if u else None
    m = re.search(r"Project Gutenberg eBook #(\d+)|gutenberg\.org/(?:cache/epub|ebooks)/(\d+)", text)
    d["gutenberg"] = (m.group(1) or m.group(2)) if m else None

    q = [l[1:].strip() for l in lines if l.startswith(">")]
    d["quote"] = clean(" / ".join(q))

    thought, on = [], False
    for l in lines:
        if re.match(r"(\*\*Thought:\*\*|## Thought)", l):
            on = True
            rest = re.sub(r"^(\*\*Thought:\*\*|## Thought)", "", l).strip()
            if rest:
                thought.append(rest)
            continue
        if on:
            if re.match(r"(\*\*Track|## Track|\*\*Translation|\*\*First read)", l):
                break
            thought.append(l.strip())
    d["thought"] = re.sub(r"\s+", " ", " ".join(thought)).strip()

    d["track"] = None
    idx = next((i for i, l in enumerate(lines) if re.match(r"(\*\*Track|## Track)", l)), None)
    if idx is not None:
        for j in range(idx, min(idx + 4, len(lines))):
            s = re.sub(r"^(\*\*Track[^*]*\*\*|## Track.*)", "", lines[j]).strip()
            tag = re.search(r"\[([\w-]+)\]\s*$", s)
            s = clean(re.sub(r"\s*\[[\w-]+\]\s*$", "", s)).replace('"', "")
            if " - " in s:
                a, t = s.split(" - ", 1)
                why = next((clean(x) for x in lines[j + 1:j + 4] if x.strip() and not x.startswith("**")), "")
                root, seed = seed_of(a.strip(), t.strip())
                d["track"] = {"artist": a.strip(), "title": t.strip(), "note": why,
                              "mood": TAG_MOOD.get((tag.group(1) if tag else "").lower(), "house"),
                              "root": root, "seed": seed}
                break
    return d


def parse_setlist(path):
    """Sets 1 and 2 only; Set 3 (crowd requests) is generated per hour in the browser."""
    tracks, section = [], ""
    for l in path.read_text().splitlines():
        if l.startswith("## "):
            section = l[3:].strip()
            continue
        if section.lower().startswith("set 3"):
            continue
        m = re.match(r"^\d+\.\s+\*\*(.+?) - (.+?)\*\*\s*(?:\[([^\]]+)\])?\s*(.*)$", l)
        if not m:
            continue
        artist, title, tag, rest = m.groups()
        mood = TAG_MOOD.get((tag or "").strip().lower()) or \
            next((v for k, v in SECTION_MOOD if k in section.lower()), "house")
        root, seed = seed_of(artist, title)
        tracks.append({"artist": artist.strip(), "title": title.strip(),
                       "note": rest.strip().lstrip(":").strip(), "section": section,
                       "for": None, "mood": mood, "root": root, "seed": seed})
    return tracks


def migrate():
    """Copy the 11 opening-night reading notes into library/books/, with the DJ's
    tagged request version of each track from the old Set 3."""
    YEARS = {"moby-dick": "1851", "leaves-of-grass": "1855", "the-time-machine": "1895", "carmilla": "1872",
             "walden": "1854", "rashomon": "1915", "the-restaurant-of-many-orders": "1924",
             "notes-from-underground": "1864", "the-lady-with-the-dog": "1899", "bay-ganyo": "1895",
             "hadzhi-dimitar": "1873"}
    reqs = {}
    sec = ""
    for l in (CLUB / "dj" / "setlist.md").read_text().splitlines():
        if l.startswith("## "):
            sec = l
        m = re.match(r"^\d+\.\s+\*\*(.+?) - (.+?)\*\*\s*\[([^\]]+)\]\s*for (Reader-\w+):?\s*(.*)$", l)
        if m and "Set 3" in sec:
            reqs[m.group(4).lower()] = m.groups()
    BOOKS.mkdir(parents=True, exist_ok=True)
    for p in sorted((CLUB / "reading-notes").glob("*.md")):
        d = parse_book(p)
        slug = re.sub(r"[^a-z0-9]+", "-", d["book"].lower().split(";")[0]
                      .replace("ō", "o").replace("ū", "u")).strip("-")
        reader = "Reader-" + p.stem.split("-", 1)[1].capitalize()
        r = reqs.get(reader.lower())
        artist, title, tag, why = (r[0], r[1], r[2], r[4]) if r else \
            (d["track"]["artist"], d["track"]["title"], "house", d["track"]["note"])
        out = [f"# {d['book']}", "", f"**Book:** {d['book']} — {d['author']}"]
        if d["original"]:
            out.append(f"**Original title:** {d['original']}")
        out += [f"**Language:** {d['language']}", f"**Year:** {YEARS.get(slug, '')}",
                f"**Source:** {d['source'] or ''}", f"**Section read:** {d['section']}",
                f"**First read by:** {reader} (opening night)", "", "**Quote (verbatim):**"]
        out += [f"> {x.strip()}" for x in d["quote"].split(" / ")]
        out.append("")
        if d["translation"]:
            out += [f"**Translation:** {d['translation']}", ""]
        out += [f"**Thought:** {d['thought']}", "", f"**Track for the DJ:** {artist} - {title} [{tag}]", why, ""]
        (BOOKS / f"{slug}.md").write_text("\n".join(out))
        print("migrated", p.name, "->", f"library/books/{slug}.md", f"[{tag}]")


def main():
    if "--migrate" in sys.argv:
        migrate()
        return
    readers = []
    rsvps = {}
    for p in sorted((CLUB / "rsvps").glob("*.md")):
        ok = "ACCEPTED" in p.read_text().upper()
        rsvps[p.stem] = ok
        if ok and p.stem.startswith("reader-"):
            readers.append({"handle": p.stem, "name": "Reader-" + p.stem.split("-", 1)[1].capitalize()})
    library, bad = [], []
    for p in sorted(BOOKS.glob("*.md")):
        b = parse_book(p)
        (library if b["quote"] and b["track"] and b["book"] else bad).append(b)
    tracks = parse_setlist(CLUB / "dj" / "setlist.md")
    data = {"readers": readers, "library": library, "tracks": tracks, "rsvps": rsvps}
    (SITE / "data.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tpl = (SITE / "template.html").read_text()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (SITE / "index.html").write_text(tpl.replace("/*__DATA__*/null", blob))
    langs = sorted({b["language"] for b in library})
    print(f"built: {len(readers)} readers, {len(library)} books ({len(langs)} languages: {', '.join(langs)}), "
          f"{len(tracks)} set tracks + {len(readers)} hourly requests, {sum(rsvps.values())}/{len(rsvps)} accepted")
    for b in bad:
        print(f"  SKIPPED {b['id']}: missing " + ", ".join(k for k in ("book", "quote", "track") if not b[k]))


if __name__ == "__main__":
    main()
