"""Generate the raster architecture diagram for the Telegram dashboard docs."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/images/telegram-operations-dashboard.png"
WIDTH, HEIGHT = 1600, 900
BG = "#08111f"
PANEL = "#111f33"
PANEL_ALT = "#142944"
CYAN = "#55d6e8"
BLUE = "#6aa7ff"
GREEN = "#67df9b"
AMBER = "#ffc763"
TEXT = "#eef6ff"
MUTED = "#a9bdd2"


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    family = (
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc"
        if bold
        else "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"
    )
    if not Path(family).exists():
        family = "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"
    return ImageFont.truetype(family, size)


def box(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    title: str,
    details: list[str],
    *,
    accent: str,
    fill: str = PANEL,
) -> None:
    x1, y1, _x2, y2 = xy
    draw.rounded_rectangle(xy, radius=24, fill=fill, outline=accent, width=3)
    draw.rounded_rectangle((x1, y1, x1 + 12, y2), radius=6, fill=accent)
    draw.text((x1 + 35, y1 + 24), title, font=font(28, bold=True), fill=TEXT)
    y = y1 + 70
    for line in details:
        draw.text((x1 + 35, y), line, font=font(19), fill=MUTED)
        y += 31


def arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    color: str = BLUE,
) -> None:
    draw.line((start, end), fill=color, width=5)
    x, y = end
    if abs(end[1] - start[1]) >= abs(end[0] - start[0]):
        points = [(x, y), (x - 10, y - 16), (x + 10, y - 16)]
    else:
        points = [(x, y), (x - 16, y - 10), (x - 16, y + 10)]
    draw.polygon(points, fill=color)


def main() -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.text(
        (70, 38),
        "CCGram · Provider-neutral Telegram Operations Dashboard",
        font=font(38, bold=True),
        fill=TEXT,
    )
    draw.text(
        (72, 91),
        "One persistent message per scope · edit in place · pin when permitted",
        font=font(21),
        fill=MUTED,
    )

    box(
        draw,
        (70, 155, 750, 330),
        "GENERAL · Global Overview",
        [
            "All topics · operators · global concurrency",
            "Active / queued / cancelling / recently ended",
            "Observation + control only; never binds a CLI workspace",
        ],
        accent=CYAN,
    )
    box(
        draw,
        (850, 155, 1530, 330),
        "NAMED TOPICS · Local Overview",
        [
            "One message in every bound physical workspace",
            "Only this topic's operators, tasks, queue and ETA",
            "Same model for Claude · Codex · Gemini · Pi · Shell",
        ],
        accent=GREEN,
    )

    box(
        draw,
        (430, 400, 1170, 580),
        "OPERATIONS DASHBOARD COORDINATOR",
        [
            "Target discovery · render · content dedupe · serialized edits",
            "Restart recovery · deleted-message recreation · 429 backoff",
            "Runtime vs progress clocks · terminal-event replay",
        ],
        accent=AMBER,
        fill=PANEL_ALT,
    )
    arrow(draw, (650, 400), (650, 330), color=CYAN)
    arrow(draw, (950, 400), (950, 330), color=GREEN)

    sources = [
        (
            (70, 650, 425, 815),
            "TaskScheduler",
            [
                "(chat, topic, user, lane)",
                "runtime ≠ last progress",
                "active · queue · stalled",
            ],
            BLUE,
        ),
        (
            (462, 650, 817, 815),
            "ThreadRouter",
            ["No hardcoded IDs", "workspace display name", "group/topic discovery"],
            CYAN,
        ),
        (
            (854, 650, 1209, 815),
            "Privacy + Identity",
            ["Authorized updates only", "normal / strict", "never stores prompts"],
            GREEN,
        ),
        (
            (1246, 650, 1530, 815),
            "Durable State",
            ["mode 0600", "dashboard + task IDs", "dispatch byte offsets"],
            AMBER,
        ),
    ]
    for xy, title, details, accent in sources:
        box(draw, xy, title, details, accent=accent)
        arrow(draw, ((xy[0] + xy[2]) // 2, xy[1]), (800, 580), color=accent)

    draw.text(
        (70, 852),
        "Failure isolation: no pin permission → editable message remains · dashboard failure never blocks CLI execution or reply delivery",
        font=font(20, bold=True),
        fill=AMBER,
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT, format="PNG", optimize=True)


if __name__ == "__main__":
    main()
