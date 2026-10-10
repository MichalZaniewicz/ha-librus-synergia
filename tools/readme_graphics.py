"""Builds the README's SVG graphics (docs/readme/*.svg).

    python tools/readme_graphics.py <fonts dir>

The fonts dir holds the two variable fonts from Google Fonts (OFL):
  Manrope.ttf        https://github.com/google/fonts/raw/main/ofl/manrope/Manrope%5Bwght%5D.ttf
  JetBrainsMono.ttf  https://github.com/google/fonts/raw/main/ofl/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf

Every SVG embeds only the glyphs it uses (WOFF, base64), so the text renders
the same inside GitHub's <img> and HACS, where no web font can load. The
animations are CSS inside the SVG - GitHub and browsers play them in <img>;
`prefers-reduced-motion` stops them. Needs fontTools (pip install fonttools).

Colours follow the existing banners: deep navy, violet, amber, green, red.
Numbers in stats.svg are hand-kept: update STATS when they change.
"""

from __future__ import annotations

import base64
import io
import sys
from html import escape
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

OUT = Path(__file__).resolve().parent.parent / "docs" / "readme"

INK = "#0b0a1a"
DEEP = "#15122f"
PANEL = "#1c1a32"
LINE = "#2c2950"
EDGE = "#3a3570"
VIOLET = "#7c6cff"
VIOLET2 = "#948cf2"
LILAC = "#c9c4f5"
PAPER = "#f1efff"
MUTED = "#8f89c4"
AMBER = "#f6b14a"
GREEN = "#5fc98a"
RED = "#e27c81"

STATS = [("33", "sensors"), ("15", "event entities"), ("30", "blueprints"), ("66", "cards"), ("0", "captcha")]

_FONT_CACHE: dict[tuple[str, int], TTFont] = {}


class Svg:
    """One SVG file: collects the elements and the characters each font
    (family, weight) has to draw, then writes it with embedded subsets."""

    def __init__(self, width: int, height: int, label: str) -> None:
        self.w, self.h, self.label = width, height, label
        self.parts: list[str] = []
        self.defs: list[str] = []
        self.css: list[str] = []
        self.chars: dict[tuple[str, int], set[str]] = {}

    def add(self, markup: str) -> None:
        self.parts.append(markup)

    def text(
        self,
        x: float,
        y: float,
        content: str,
        size: float,
        weight: int = 600,
        fill: str = PAPER,
        mono: bool = False,
        anchor: str = "start",
        spacing: float = 0,
        cls: str = "",
    ) -> str:
        family = "mono" if mono else "sans"
        self.chars.setdefault((family, weight), set()).update(content)
        attrs = [
            f'x="{x}"',
            f'y="{y}"',
            f'font-family="{"LSMono" if mono else "LSSans"}"',
            f'font-size="{size}"',
            f'font-weight="{weight}"',
            f'fill="{fill}"',
        ]
        if anchor != "start":
            attrs.append(f'text-anchor="{anchor}"')
        if spacing:
            attrs.append(f'letter-spacing="{spacing}"')
        if cls:
            attrs.append(f'class="{cls}"')
        return f"<text {' '.join(attrs)}>{escape(content)}</text>"

    def write(self, name: str, fonts: Path) -> None:
        faces = []
        for (family, weight), chars in sorted(self.chars.items()):
            data = _subset(fonts, family, weight, "".join(sorted(chars)))
            css_family = "LSMono" if family == "mono" else "LSSans"
            faces.append(
                f"@font-face{{font-family:{css_family};font-weight:{weight};"
                f"src:url(data:font/woff;base64,{data}) format('woff')}}"
            )
        style = "".join(faces) + "".join(self.css)
        style += "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
            f'width="{self.w}" height="{self.h}" role="img" aria-label="{escape(self.label)}">'
            f"<title>{escape(self.label)}</title>"
            f"<style>{style}</style>"
            f"<defs>{''.join(self.defs)}</defs>"
            f"{''.join(self.parts)}</svg>"
        )
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_text(svg, encoding="utf-8", newline="\n")
        print(f"{name}: {len(svg) // 1024} KB")


def _subset(fonts: Path, family: str, weight: int, chars: str) -> str:
    key = (family, weight)
    if key not in _FONT_CACHE:
        source = TTFont(fonts / ("JetBrainsMono.ttf" if family == "mono" else "Manrope.ttf"))
        _FONT_CACHE[key] = instancer.instantiateVariableFont(source, {"wght": weight})
    buffer = io.BytesIO()
    _FONT_CACHE[key].save(buffer)
    font = TTFont(io.BytesIO(buffer.getvalue()))
    options = subset.Options()
    options.flavor = "woff"
    options.layout_features = ["kern", "liga"]
    options.name_IDs = []
    options.notdef_outline = False
    sub = subset.Subsetter(options)
    sub.populate(text=chars + " ")
    sub.subset(font)
    out = io.BytesIO()
    font.flavor = "woff"
    font.save(out)
    return base64.b64encode(out.getvalue()).decode("ascii")


def _background(svg: Svg, gid: str, cx: str = "18%", cy: str = "10%", glow: tuple[int, int, int] | None = None) -> None:
    svg.defs.append(
        f'<radialGradient id="{gid}" cx="{cx}" cy="{cy}" r="95%">'
        f'<stop offset="0" stop-color="#2a2470"/><stop offset=".45" stop-color="{DEEP}"/>'
        f'<stop offset="1" stop-color="{INK}"/></radialGradient>'
    )
    svg.add(f'<rect width="{svg.w}" height="{svg.h}" rx="22" fill="url(#{gid})"/>')
    if glow:
        x, y, r = glow
        svg.defs.append(
            f'<radialGradient id="{gid}g"><stop offset="0" stop-color="{VIOLET}" stop-opacity=".42"/>'
            f'<stop offset="1" stop-color="{VIOLET}" stop-opacity="0"/></radialGradient>'
        )
        svg.add(f'<circle cx="{x}" cy="{y}" r="{r}" fill="url(#{gid}g)"/>')


def _card(svg: Svg, x: float, y: float, w: float, h: float, r: float = 18, fill: str = PANEL) -> str:
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{EDGE}"/>'


MIC = "M12 15a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v7a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V22h2v-3.1A7 7 0 0 0 19 12z"


def hero(fonts: Path) -> None:
    svg = Svg(1200, 440, "Librus Synergia for Home Assistant")
    svg.css.append(
        ".typing{animation:type 9s steps(30,end) infinite;transform-box:fill-box;transform-origin:right center}"
        "@keyframes type{0%{transform:scaleX(1)}28%,100%{transform:scaleX(0)}}"
        ".answer{animation:ans 9s infinite}"
        "@keyframes ans{0%,33%{opacity:0;transform:translateY(8px)}40%,92%{opacity:1;transform:none}100%{opacity:0}}"
        ".f1{animation:fl 6s ease-in-out infinite}.f2{animation:fl 6s ease-in-out -2s infinite}"
        ".f3{animation:fl 6s ease-in-out -4s infinite}@keyframes fl{50%{transform:translateY(-7px)}}"
        ".toast{animation:toast 9s ease-out infinite}"
        "@keyframes toast{0%,46%{opacity:0;transform:translateY(-16px)}54%,94%{opacity:1;transform:none}100%{opacity:0}}"
        ".bar{animation:bar 9s linear infinite;transform-box:fill-box;transform-origin:left center}"
        "@keyframes bar{0%{transform:scaleX(.55)}100%{transform:scaleX(.8)}}"
    )
    _background(svg, "hbg", glow=(900, 220, 270))
    svg.add(svg.text(64, 92, "HOME ASSISTANT × LIBRUS SYNERGIA", 14, 700, AMBER, mono=True, spacing=2.4))
    svg.add(svg.text(64, 152, "Your child's school day,", 50, 800, PAPER, spacing=-1))
    svg.add(svg.text(64, 210, "inside Home Assistant.", 50, 800, VIOLET2, spacing=-1))
    svg.add(svg.text(64, 252, "Grades, timetable, tests, homework and messages - with", 18, 500, LILAC))
    svg.add(svg.text(64, 276, "notifications and automations that help a family's week.", 18, 500, LILAC))
    # Assist question typing itself, then the answer
    svg.add(f'<rect x="64" y="304" width="392" height="46" rx="15" fill="{VIOLET}"/>')
    svg.add(f'<g transform="translate(80 315) scale(1)"><path d="{MIC}" fill="#ffffff" transform="scale(1)"/></g>')
    svg.add(svg.text(112, 333, "What does Ola have tomorrow?", 17, 700, "#ffffff"))
    svg.add(f'<rect class="typing" x="110" y="312" width="336" height="30" fill="{VIOLET}"/>')
    svg.add(
        '<g class="answer">'
        f'<rect x="64" y="360" width="500" height="58" rx="15" fill="#2a2650"/>'
        + svg.text(84, 384, "6 lessons from 8:00 - a biology test in room 21,", 15, 600, PAPER)
        + svg.text(84, 405, "and the maths homework is due.", 15, 600, PAPER)
        + "</g>"
    )
    # Floating live tiles
    svg.add(
        '<g class="f1">'
        + _card(svg, 770, 66, 340, 100)
        + svg.text(792, 96, "NOW · LESSON 3", 11, 700, MUTED, mono=True, spacing=1.5)
        + svg.text(792, 128, "Matematyka", 23, 800, PAPER)
        + svg.text(1088, 128, "room 204", 14, 600, LILAC, anchor="end")
        + f'<rect x="792" y="143" width="296" height="7" rx="3.5" fill="{LINE}"/>'
        + f'<rect class="bar" x="792" y="143" width="296" height="7" rx="3.5" fill="{VIOLET}"/>'
        + "</g>"
    )
    svg.add(
        '<g class="f2">'
        + _card(svg, 700, 186, 256, 112)
        + svg.text(722, 216, "NEXT TEST", 11, 700, MUTED, mono=True, spacing=1.5)
        + svg.text(722, 250, "Biologia", 23, 800, PAPER)
        + svg.text(722, 277, "in 2 days · 4 topics", 14, 600, AMBER)
        + "</g>"
    )
    svg.add(
        '<g class="f3">'
        + _card(svg, 974, 186, 150, 112)
        + svg.text(996, 216, "LUCKY", 11, 700, MUTED, mono=True, spacing=1.5)
        + svg.text(996, 268, "14", 46, 800, GREEN)
        + "</g>"
    )
    svg.add(
        '<g class="toast">'
        f'<rect x="752" y="322" width="384" height="72" rx="18" fill="{PAPER}" fill-opacity=".1" stroke="#5e5a8c"/>'
        f'<rect x="770" y="339" width="38" height="38" rx="10" fill="{VIOLET}"/>'
        + svg.text(789, 364, "5", 17, 800, "#ffffff", anchor="middle")
        + svg.text(822, 353, "Ola: nowa ocena 5 z matematyki", 13.5, 700, PAPER)
        + svg.text(822, 375, "Kartkówka · waga 2 · just now", 12.5, 500, LILAC)
        + "</g>"
    )
    svg.write("hero.svg", fonts)


def stats(fonts: Path) -> None:
    svg = Svg(1200, 120, "33 sensors, 15 event entities, 30 blueprints, 66 cards, no captcha")
    svg.defs.append(f'<linearGradient id="sbg" x1="0" x2="1"><stop offset="0" stop-color="{DEEP}"/><stop offset="1" stop-color="#1d1946"/></linearGradient>')
    svg.add(f'<rect width="1200" height="120" rx="18" fill="url(#sbg)" stroke="{LINE}"/>')
    col = 1200 / len(STATS)
    for i, (number, label) in enumerate(STATS):
        cx = col * i + col / 2
        if i:
            svg.add(f'<line x1="{col * i}" y1="24" x2="{col * i}" y2="96" stroke="{LINE}"/>')
        svg.add(svg.text(cx, 66, number, 40, 800, AMBER if label == "blueprints" else PAPER, anchor="middle"))
        svg.add(svg.text(cx, 92, label.upper(), 12, 700, MUTED, mono=True, anchor="middle", spacing=1.6))
    svg.write("stats.svg", fonts)


ICONS = {
    "register": "M5 3h11l3 3v15H5zM8 9h8v2H8zm0 4h8v2H8z",
    "forecast": "M12 2l3 7h7l-5.6 4.3L18.5 21 12 16.7 5.5 21l2.1-7.7L2 9h7z",
    "bell": "M12 22a2.5 2.5 0 0 0 2.5-2.5h-5A2.5 2.5 0 0 0 12 22zm7-6V11a7 7 0 0 0-5-6.7V3h-4v1.3A7 7 0 0 0 5 11v5l-2 2v1h18v-1z",
    "home": "M3 11l9-8 9 8v10h-6v-6H9v6H3z",
    "ai": "M12 2l1.8 5.2L19 9l-5.2 1.8L12 16l-1.8-5.2L5 9l5.2-1.8zM19 14l.9 2.6 2.6.9-2.6.9L19 21l-.9-2.6-2.6-.9 2.6-.9z",
    "mic": MIC,
}


def _chips(svg: Svg, x: float, y: float, chips: list[tuple[str, str]]) -> None:
    colours = {
        "v": ("#2a2470", LILAC),
        "g": ("#1d3a32", GREEN),
        "a": ("#3a2f1f", AMBER),
        "r": ("#3a2230", RED),
    }
    cx = x
    for label, kind in chips:
        bg, fg = colours[kind]
        width = 22 + len(label) * 7.3
        svg.add(f'<rect x="{cx}" y="{y}" width="{width}" height="26" rx="13" fill="{bg}"/>')
        svg.add(svg.text(cx + width / 2, y + 17.5, label, 12, 700, fg, mono=True, anchor="middle"))
        cx += width + 8


def tile(fonts: Path, name: str, icon: str, title: str, lines: list[str], extra) -> None:
    svg = Svg(400, 250, title)
    svg.defs.append(f'<linearGradient id="tbg" x1="0" y1="0" x2=".6" y2="1"><stop offset="0" stop-color="#1d1a3d"/><stop offset="1" stop-color="#120f28"/></linearGradient>')
    svg.add(f'<rect x="1" y="1" width="398" height="248" rx="20" fill="url(#tbg)" stroke="{LINE}"/>')
    svg.add(f'<rect x="24" y="24" width="44" height="44" rx="13" fill="{VIOLET}" fill-opacity=".18"/>')
    svg.add(f'<g transform="translate(34 34) scale(1)"><path d="{ICONS[icon]}" fill="{VIOLET2}"/></g>')
    svg.add(svg.text(24, 104, title, 21, 800, PAPER))
    for i, line in enumerate(lines):
        svg.add(svg.text(24, 132 + i * 21, line, 15, 500, MUTED))
    extra(svg)
    svg.write(name, fonts)


def features(fonts: Path) -> None:
    tile(
        fonts, "feature-register.svg", "register", "Everything from the e-register",
        ["Grades, attendance, timetable, tests,", "messages and the lucky number."],
        lambda s: _chips(s, 24, 196, [("grades", "v"), ("attendance", "v"), ("messages", "v")]),
    )

    def bars(s: Svg) -> None:
        heights = [26, 38, 32, 46, 54]
        for i, h in enumerate(heights):
            colour = AMBER if i == len(heights) - 1 else VIOLET
            s.add(f'<rect x="{24 + i * 30}" y="{224 - h}" width="22" height="{h}" rx="5" fill="{colour}" fill-opacity="{1 if i == 4 else .75}"/>')
        s.add(s.text(186, 216, "predicted 6", 13, 700, AMBER, mono=True))

    tile(fonts, "feature-forecast.svg", "forecast", "Report card forecast",
         ["Every subject's grade before it's", "written, and how many 6s lift it."], bars)
    tile(
        fonts, "feature-notifications.svg", "bell", "30 ready-made notifications",
        ["New grade, cancelled lesson, test", "tomorrow - one click to import."],
        lambda s: _chips(s, 24, 196, [("new grade", "g"), ("cancelled", "r"), ("test", "a")]),
    )
    tile(
        fonts, "feature-home.svg", "home", "The school day at home",
        ["Wake the house before the first", "lesson, skip the alarm on a day off."],
        lambda s: _chips(s, 24, 196, [("school day", "v"), ("at school", "v"), ("pick-up", "a")]),
    )
    tile(
        fonts, "feature-ai.svg", "ai", "Weekly AI summary",
        ["Your own AI model sums up the week", "with 2-4 concrete to-dos."],
        lambda s: _chips(s, 24, 196, [("grades ok", "g"), ("attendance", "a"), ("2 to-dos", "v")]),
    )

    def chat(s: Svg) -> None:
        s.add(f'<rect x="24" y="190" width="300" height="36" rx="13" fill="{VIOLET}"/>')
        s.add(s.text(40, 213, "When is the next maths test?", 14, 700, "#ffffff"))

    tile(fonts, "feature-assist.svg", "mic", "Ask Assist",
         ["Ask by voice or in the chat - it", "answers from your child's data."], chat)


def _window(svg: Svg, x: float, y: float, w: float, h: float, title: str) -> None:
    svg.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{INK}" stroke="{EDGE}"/>')
    svg.add(f'<path d="M{x} {y + 16}a16 16 0 0 1 16-16h{w - 32}a16 16 0 0 1 16 16v26H{x}z" fill="{DEEP}"/>')
    svg.add(f'<line x1="{x}" y1="{y + 42}" x2="{x + w}" y2="{y + 42}" stroke="{LINE}"/>')
    for i, colour in enumerate((RED, AMBER, GREEN)):
        svg.add(f'<circle cx="{x + 22 + i * 18}" cy="{y + 21}" r="5.5" fill="{colour}"/>')
    svg.add(svg.text(x + 86, y + 26, title, 13, 700, MUTED, mono=True, spacing=.5))


def trailer_soon(fonts: Path) -> None:
    svg = Svg(1200, 720, "New trailer coming soon")
    svg.css.append(".pulse{animation:pulse 2.6s ease-in-out infinite;transform-box:fill-box;transform-origin:center}"
                   "@keyframes pulse{50%{transform:scale(1.08);opacity:.75}}")
    _background(svg, "tbg2", cx="50%", cy="0%", glow=(600, 380, 330))
    _window(svg, 40, 34, 1120, 610, "Home Assistant · Librus Synergia")
    svg.add(f'<circle class="pulse" cx="600" cy="320" r="62" fill="{AMBER}" fill-opacity=".16"/>')
    svg.add(f'<circle cx="600" cy="320" r="46" fill="{AMBER}"/>')
    svg.add(f'<path d="M588 296v48l38-24z" fill="{INK}"/>')
    svg.add(svg.text(600, 430, "New trailer coming soon", 34, 800, PAPER, anchor="middle"))
    svg.add(svg.text(600, 466, "45 seconds of what it does - the new cards included.", 17, 500, LILAC, anchor="middle"))
    svg.add(svg.text(80, 690, "45 SECONDS: WHAT IT DOES", 13, 700, AMBER, mono=True, spacing=2))
    svg.add(svg.text(1120, 690, "grades · tests · notifications · AI summary · cards", 13, 600, MUTED, mono=True, anchor="end"))
    svg.write("trailer-soon.svg", fonts)


CHAPTERS = [("Setup", "v"), ("The school day", "v"), ("Grades & forecast", "v"), ("Attendance", "v"),
            ("Notifications", "a"), ("AI summary", "v"), ("Assist", "v"), ("Cards", "g")]


def video_header(fonts: Path) -> None:
    svg = Svg(1200, 170, "Watch the 3-minute tour")
    svg.defs.append(f'<linearGradient id="vbg" x1="0" x2="1"><stop offset="0" stop-color="{DEEP}"/><stop offset="1" stop-color="#1d1946"/></linearGradient>')
    svg.add(f'<rect width="1200" height="170" rx="20" fill="url(#vbg)" stroke="{LINE}"/>')
    svg.add(f'<circle cx="68" cy="66" r="30" fill="{AMBER}"/><path d="M60 50v32l25-16z" fill="{INK}"/>')
    svg.add(svg.text(116, 60, "Don't want to read? Watch the 3-minute tour", 26, 800, PAPER))
    svg.add(svg.text(116, 88, "English voice-over, Polish subtitles. The player starts muted - click the speaker.", 15, 500, LILAC))
    _chips(svg, 38, 118, CHAPTERS)
    svg.write("video-header.svg", fonts)


def video_soon(fonts: Path) -> None:
    svg = Svg(1200, 675, "New 3-minute tour coming soon")
    svg.add(f'<rect width="1200" height="675" rx="14" fill="#000000"/>')
    svg.defs.append(f'<radialGradient id="vsg" cx="50%" cy="40%" r="70%"><stop offset="0" stop-color="#2a2470"/><stop offset="1" stop-color="#05040c"/></radialGradient>')
    svg.add(f'<rect width="1200" height="675" rx="14" fill="url(#vsg)"/>')
    svg.add('<circle cx="600" cy="300" r="52" fill="#000000" fill-opacity=".45" stroke="#ffffff" stroke-opacity=".85" stroke-width="3"/>')
    svg.add('<path d="M585 274v52l44-26z" fill="#ffffff"/>')
    svg.add(svg.text(600, 400, "New 3-minute tour coming soon", 32, 800, PAPER, anchor="middle"))
    svg.add(svg.text(600, 434, "Setup, the school day, grades, notifications, the AI summary, Assist and the cards.", 16, 500, LILAC, anchor="middle"))
    svg.add('<rect x="0" y="625" width="1200" height="50" fill="#000000" fill-opacity=".55"/>')
    svg.add(svg.text(24, 656, "0:00 / 3:00", 14, 600, "#ffffff"))
    svg.add('<rect x="130" y="648" width="960" height="4" rx="2" fill="#ffffff" fill-opacity=".35"/>')
    svg.write("video-soon.svg", fonts)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    fonts = Path(sys.argv[1])
    hero(fonts)
    stats(fonts)
    features(fonts)
    trailer_soon(fonts)
    video_header(fonts)
    video_soon(fonts)


if __name__ == "__main__":
    main()
