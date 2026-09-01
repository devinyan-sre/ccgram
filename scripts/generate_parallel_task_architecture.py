"""Generate the raster diagram for same-member parallel task lanes."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/images/same-member-parallel-task-architecture.png"
WIDTH, HEIGHT = 1800, 1080
BG = "#07111f"
PANEL = "#102238"
PANEL_ALT = "#15304c"
TEXT = "#f2f7ff"
MUTED = "#b4c8dd"
BLUE = "#6ea8ff"
CYAN = "#5ad8e8"
GREEN = "#6ce0a0"
AMBER = "#ffc866"
RED = "#ff7f88"
PERSISTENCE_COLUMN_MAX_X = 900


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    family = (
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc"
        if bold
        else "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"
    )
    if not Path(family).exists():
        family = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    return ImageFont.truetype(family, size)


def box(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    title: str,
    lines: list[str],
    accent: str,
    *,
    fill: str = PANEL,
) -> None:
    x1, y1, _x2, y2 = xy
    draw.rounded_rectangle(xy, radius=22, fill=fill, outline=accent, width=3)
    draw.rounded_rectangle((x1, y1, x1 + 11, y2), radius=5, fill=accent)
    draw.text((x1 + 30, y1 + 20), title, font=font(25, bold=True), fill=TEXT)
    y = y1 + 61
    for line in lines:
        draw.text((x1 + 30, y), line, font=font(18), fill=MUTED)
        y += 29


def arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    color: str,
    label: str = "",
) -> None:
    draw.line((*start, *end), fill=color, width=4)
    x, y = end
    if abs(end[0] - start[0]) > abs(end[1] - start[1]):
        draw.polygon([(x, y), (x - 15, y - 9), (x - 15, y + 9)], fill=color)
    else:
        draw.polygon([(x, y), (x - 9, y - 15), (x + 9, y - 15)], fill=color)
    if label:
        mx = (start[0] + end[0]) // 2
        my = (start[1] + end[1]) // 2
        draw.text((mx + 8, my - 25), label, font=font(16, bold=True), fill=color)


def main() -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.text(
        (65, 32),
        "CCGram · 同一成员多任务隔离架构",
        font=font(40, bold=True),
        fill=TEXT,
    )
    draw.text(
        (68, 88),
        "默认串行 · 显式并行 · 回复/任务编号精确关联 · Provider 与 tmux/herdr 无关",
        font=font(21),
        fill=MUTED,
    )

    box(
        draw,
        (65, 145, 460, 330),
        "Telegram · A 用户 / A 话题",
        [
            "普通消息 → 默认任务通道",
            "/task_parallel → 新并行通道",
            "回复消息 / /task_add T编号 → 精确补充",
            "图片、文件、相册、语音使用同一关联",
        ],
        CYAN,
    )
    box(
        draw,
        (550, 145, 1110, 330),
        "准入与关联层",
        [
            "白名单/RBAC · 消息幂等 · 歧义不猜测",
            "TaskScheduler key = 话题 + 用户 + 任务通道",
            "根问题/补充/回执/机器人回答 ↔ T编号",
            "成员/话题/全局并发上限与公平排队",
        ],
        AMBER,
        fill=PANEL_ALT,
    )
    arrow(draw, (460, 235), (550, 235), BLUE)

    lanes = [
        (
            (70, 445, 525, 755),
            "默认通道 · T0101",
            [
                "普通消息自动进入",
                "CLI 窗口 @7",
                "Provider 会话 S-A",
                "成员默认 worktree",
                "同一任务补充严格串行",
                "最终回答回复根问题 M101",
            ],
            BLUE,
        ),
        (
            (675, 445, 1130, 755),
            "并行通道 · T0102",
            [
                "由 /task_parallel 显式创建",
                "CLI 窗口 @8",
                "Provider 会话 S-B",
                "ccg/task-...-t0102 worktree",
                "本任务内部补充严格串行",
                "最终回答回复根问题 M102",
            ],
            GREEN,
        ),
        (
            (1280, 445, 1735, 755),
            "并行通道 · T0103",
            [
                "由 /task_parallel 显式创建",
                "CLI 窗口 @9",
                "Provider 会话 S-C",
                "ccg/task-...-t0103 worktree",
                "本任务内部补充严格串行",
                "最终回答回复根问题 M103",
            ],
            GREEN,
        ),
    ]
    for xy, title, lines, accent in lanes:
        box(draw, xy, title, lines, accent)
        arrow(draw, (830, 330), ((xy[0] + xy[2]) // 2, xy[1]), accent)

    box(
        draw,
        (65, 850, 830, 1010),
        "持久化恢复",
        [
            "state.json：任务通道窗口/所有者/话题/任务编号",
            "tasks.json：活动/排队/取消/租约/任务通道",
            "inbound.json：根消息、补充、回执、输出消息、窗口与任务编号",
        ],
        CYAN,
    )
    box(
        draw,
        (970, 850, 1735, 1010),
        "安全边界",
        [
            "脏仓库、非 Git、分支冲突 → 关闭失败，不共享写目录",
            "不继承 YOLO/bypass；取消确认前不释放槽位",
            "多个活动任务且无回复/编号/选择 → 拒绝发送，绝不串题",
        ],
        RED,
    )
    for x in (297, 902, 1507):
        arrow(
            draw,
            (x, 755),
            (x, 850),
            CYAN if x < PERSISTENCE_COLUMN_MAX_X else RED,
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT, format="PNG", optimize=True)


if __name__ == "__main__":
    main()
