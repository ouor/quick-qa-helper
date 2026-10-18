"""One-glance report image: numbered cards "purpose / operation / annotated screenshot", ending with the result.

Sized for an agent to Read: one page holds 12 cards in about 1400x1450 px, so text stays legible after the
model's own image downscaling. Runs with more cards are split over several pages.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLS, ROWS = 6, 2
THUMB_W = 200
CAPTION_H = 150
PAD = 14
HEADER_H = 92

BG = (245, 246, 248)
CARD = (255, 255, 255)
INK = (28, 30, 34)
MUTED = (92, 98, 110)
ACCENT = (37, 99, 235)
MARK = (230, 38, 38)
STATUS_COLORS = {"passed": (22, 140, 72), "passed_weak": (200, 120, 0), "passed_with_crash_log": (200, 120, 0), "unverified": (200, 120, 0),
                 "failed": (200, 40, 40), "max_steps": (200, 40, 40)}
# Fixed card texts. The planner writes the per-step purposes in the report language itself; these are the rest.
STRINGS = {
    "en": {
        "title": "QA run report", "task": "Task: ",
        "status": {"passed": "Goal reached, finished", "passed_weak": "Weak pass (check it)",
                   "passed_with_crash_log": "Goal reached (crash log present)", "unverified": "Ended (not verified)",
                   "failed": "Failed", "max_steps": "Stopped at step limit"},
        "failure": {"not_found": "Failed: element not found", "feature_missing": "Failed: feature missing",
                    "app_error": "Failed: app error", "blocked": "Failed: blocked (login, permission...)"},
        "dir": {"up": "up", "down": "down", "left": "left", "right": "right"},
        "tap": "Tap: {}", "long_press": "Long-press: {}", "type": "Type: \"{}\" → {}", "scroll": "Scroll {}",
        "back": "Back", "home": "Home", "enter": "Enter key", "launch": "Launch app: {}", "wait": "Wait {}s (loading)",
        "result": "Result: {}", "verifier": " · verifier p={:.3f}", "state": "App state changes ({}): ",
        "blocked": "Safe mode blocked {} tap(s)", "no_shot": "(no screenshot)",
    },
    "ko": {
        "title": "QA 실행 보고", "task": "작업: ",
        "status": {"passed": "목적 달성 및 종료", "passed_weak": "약하게 통과 (확인 필요)",
                   "passed_with_crash_log": "목적 달성 (크래시 로그 있음)", "unverified": "종료 (검증기 미확인)",
                   "failed": "실패로 종료", "max_steps": "단계 한도 초과로 중단"},
        "failure": {"not_found": "실패: 요소를 찾지 못함", "feature_missing": "실패: 기능 없음",
                    "app_error": "실패: 앱 오류", "blocked": "실패: 진행 불가 (로그인·권한 등)"},
        "dir": {"up": "위로", "down": "아래로", "left": "왼쪽으로", "right": "오른쪽으로"},
        "tap": "탭: {}", "long_press": "길게 누르기: {}", "type": "입력: \"{}\" → {}", "scroll": "스크롤 {}",
        "back": "뒤로 가기", "home": "홈으로 이동", "enter": "엔터 키 입력", "launch": "앱 실행: {}",
        "wait": "{}초 대기 (로딩)", "result": "결과: {}", "verifier": " · 검증 p={:.3f}", "state": "앱 상태 변경 {}건: ",
        "blocked": "안전 모드 차단 {}건", "no_shot": "(스크린샷 없음)",
    },
}


def strings_for(lang: str) -> dict:
    lang = (lang or "").strip().lower()
    return STRINGS["ko"] if lang.startswith(("ko", "한국")) else STRINGS["en"]

# CJK-capable fonts first (card texts may be Korean/Japanese/Chinese), then plain Latin fonts.
# QUICKQA_FONT / QUICKQA_FONT_BOLD point at any .ttf/.otf/.ttc to override.
_FONT_CANDIDATES = {
    False: ["C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/YuGothR.ttc", "C:/Windows/Fonts/msyh.ttc",
            "/System/Library/Fonts/AppleSDGothicNeo.ttc", "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "C:/Windows/Fonts/segoeui.ttf", "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    True: ["C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/msyhbd.ttc",
           "/System/Library/Fonts/AppleSDGothicNeo.ttc",
           "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
           "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
           "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
           "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
           "C:/Windows/Fonts/segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}


def _font(size: int, bold: bool = False):
    override = os.environ.get("QUICKQA_FONT_BOLD" if bold else "QUICKQA_FONT") or os.environ.get("QUICKQA_FONT")
    for path in ([override] if override else []) + _FONT_CANDIDATES[bold] + _FONT_CANDIDATES[False]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


@dataclass
class Card:
    purpose: str
    operation: str
    thumb: bytes | None
    action: str = ""
    point: tuple[int, int] | None = None     # device px
    bounds: tuple | None = None              # device px
    direction: str = ""
    count: int = 1
    final: bool = False
    status: str = ""


# ---------------------------------------------------------------------------- building cards
def _operation(step, t: dict) -> str:
    p, sel = step.plan, step.selection
    a, label = p["action"], (sel.get("label") or p["target"] or "").strip()
    label = label.replace("\n", " ")
    if a in ("tap", "long_press"):
        op = t[a].format(label)
    elif a == "type":
        op = t["type"].format(p["text"], label)
    elif a == "scroll":
        op = t["scroll"].format(t["dir"].get(sel.get("direction") or p["direction"], ""))
    elif a in ("back", "home", "enter"):
        op = t[a]
    elif a == "launch":
        op = t["launch"].format(p["text"])
    elif a == "wait":
        op = t["wait"].format(p["text"] or 1)
    else:
        op = a
    if step.result and not step.result.startswith("ok") and not step.result.startswith("verifier"):
        op += f"  ※ {step.result}"
    return op


def build_cards(steps, status: str, verified: float | None, final_shot: bytes | None,
                failure_kind: str | None = None, state_changes: list[str] | None = None,
                blocked: list[str] | None = None, t: dict = STRINGS["en"]) -> list[Card]:
    cards: list[Card] = []
    last = steps[-1] if steps else None
    for s in steps:
        a, st = s.plan["action"], s.plan["status"]
        if st != "continue" or a in ("look", "none") or s.result.startswith("failure rejected"):
            continue  # done/failed claims go to the final card; look/none change nothing on the device
        op = _operation(s, t)
        prev = cards[-1] if cards else None
        if prev and a in ("scroll", "wait") and prev.action == a and prev.operation.split("  ×")[0] == op:
            prev.count += 1
            prev.operation = f"{op}  ×{prev.count}"
            continue
        sel = s.selection
        cards.append(Card(
            purpose=s.plan.get("purpose") or s.plan["thought"], operation=op, thumb=s.thumb, action=a,
            point=(sel["x"], sel["y"]) if "x" in sel else None,
            bounds=tuple(sel["bounds"]) if sel.get("bounds") else None,
            direction=sel.get("direction", "")))
    conclusion = ""
    if last is not None:
        conclusion = last.plan.get("purpose") or last.plan["thought"]
        if last.plan.get("observation") and last.plan["observation"] != conclusion:
            conclusion += "\n" + last.plan["observation"]
    detail = t["result"].format(status) + (t["verifier"].format(verified) if verified is not None else "")
    if state_changes:
        detail += "\n" + t["state"].format(len(state_changes)) + "; ".join(state_changes[:2])
    if blocked:
        detail += "\n" + t["blocked"].format(len(blocked))
    if status == "failed":
        title = t["failure"].get(failure_kind or "", t["status"]["failed"])
    else:
        title = t["status"].get(status, status)
    cards.append(Card(purpose=title, operation=f"{detail}\n{conclusion}",
                      thumb=final_shot, final=True, status=status))
    return cards


# ---------------------------------------------------------------------------- drawing
def _wrap(draw, text: str, font, width: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for ch in para:
            if draw.textlength(cur + ch, font=font) <= width:
                cur += ch
                continue
            # break on the last space when possible so words stay whole
            cut = cur.rfind(" ")
            if cut > len(cur) * 0.5:
                lines.append(cur[:cut])
                cur = cur[cut + 1:] + ch
            else:
                lines.append(cur)
                cur = ch
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def _thumb(card: Card, dev_w: int, dev_h: int, thumb_h: int, no_shot: str = "(no screenshot)") -> Image.Image:
    if not card.thumb:
        im = Image.new("RGB", (THUMB_W, thumb_h), (220, 222, 226))
        ImageDraw.Draw(im).text((THUMB_W // 2, thumb_h // 2), no_shot, fill=MUTED, anchor="mm",
                                font=_font(16))
        return im
    im = Image.open(io.BytesIO(card.thumb)).convert("RGB").resize((THUMB_W, thumb_h))
    d = ImageDraw.Draw(im)
    sx, sy = THUMB_W / max(1, dev_w), thumb_h / max(1, dev_h)
    if card.bounds and card.action in ("type", "long_press", "tap"):
        x1, y1, x2, y2 = card.bounds
        d.rectangle((x1 * sx, y1 * sy, x2 * sx, y2 * sy), outline=MARK, width=2)
    if card.point and card.action in ("tap", "long_press", "type"):
        x, y = card.point[0] * sx, card.point[1] * sy
        r = 13
        d.ellipse((x - r, y - r, x + r, y + r), outline=MARK, width=4)
        d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=MARK)
        if card.action == "long_press":
            d.ellipse((x - r - 7, y - r - 7, x + r + 7, y + r + 7), outline=MARK, width=2)
    if card.action == "scroll":
        x1, y1, x2, y2 = card.bounds or (0, dev_h * 0.15, dev_w, dev_h * 0.85)
        cx, cy = (x1 + x2) / 2 * sx, (y1 + y2) / 2 * sy
        L = min(thumb_h, THUMB_W) * 0.3
        vec = {"down": (0, L), "up": (0, -L), "right": (L, 0), "left": (-L, 0)}.get(card.direction, (0, L))
        ex, ey = cx + vec[0], cy + vec[1]
        sx0, sy0 = cx - vec[0], cy - vec[1]
        d.line((sx0, sy0, ex, ey), fill=MARK, width=7)
        ux, uy = vec[0] / L, vec[1] / L
        head = 20
        d.polygon([(ex + ux * head, ey + uy * head),
                   (ex - uy * head * 0.8, ey + ux * head * 0.8),
                   (ex + uy * head * 0.8, ey - ux * head * 0.8)], fill=MARK)
    if card.action in ("back", "home", "enter", "launch", "wait"):
        f_badge = _font(16, True)
        badge = {"back": "◀ BACK", "home": "● HOME", "enter": "ENTER", "launch": "▶ LAUNCH", "wait": "WAIT"}[card.action]
        w = d.textlength(badge, font=f_badge) + 18
        d.rounded_rectangle(((THUMB_W - w) / 2, thumb_h - 44, (THUMB_W + w) / 2, thumb_h - 14), 9, fill=MARK)
        d.text((THUMB_W / 2, thumb_h - 29), badge, fill="white", anchor="mm", font=f_badge)
    return im


def _page(task: str, cards: list[tuple[int, Card]], page: int, pages: int, status: str,
          dev_w: int, dev_h: int, t: dict) -> Image.Image:
    thumb_h = round(THUMB_W * dev_h / max(1, dev_w))
    card_w, card_h = THUMB_W + 2 * PAD, thumb_h + CAPTION_H + 2 * PAD
    cols = min(COLS, max(len(cards), 3)) if pages == 1 else COLS
    rows = (len(cards) + cols - 1) // cols
    W = cols * card_w + (cols + 1) * PAD
    H = HEADER_H + rows * card_h + (rows + 1) * PAD
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_title, f_task = _font(26, True), _font(19)
    color = STATUS_COLORS.get(status, MUTED)
    tag = f"{t['status'].get(status, status)}" + (f"   ({page}/{pages})" if pages > 1 else "")
    d.text((PAD + 4, 14), t["title"], fill=INK, font=f_title)
    d.text((W - PAD - 4, 14), tag, fill=color, font=f_title, anchor="ra")
    for i, line in enumerate(_wrap(d, t["task"] + task, f_task, W - 2 * PAD - 8, 2)):
        d.text((PAD + 4, 50 + i * 22), line, fill=MUTED, font=f_task)

    f_num, f_purpose, f_op = _font(17, True), _font(16, True), _font(15)
    for i, (num, card) in enumerate(cards):
        r, c = divmod(i, cols)
        x0 = PAD + c * (card_w + PAD)
        y0 = HEADER_H + PAD + r * (card_h + PAD)
        border = STATUS_COLORS.get(card.status, ACCENT) if card.final else (226, 228, 232)
        d.rounded_rectangle((x0, y0, x0 + card_w, y0 + card_h), 12, fill=CARD, outline=border,
                            width=4 if card.final else 1)
        img.paste(_thumb(card, dev_w, dev_h, thumb_h, t["no_shot"]), (x0 + PAD, y0 + PAD))
        ty = y0 + PAD + thumb_h + 10
        # number badge + purpose
        badge_color = STATUS_COLORS.get(card.status, ACCENT) if card.final else ACCENT
        d.ellipse((x0 + PAD, ty, x0 + PAD + 26, ty + 26), fill=badge_color)
        d.text((x0 + PAD + 13, ty + 13), str(num), fill="white", anchor="mm", font=f_num)
        text_x, text_w = x0 + PAD + 32, THUMB_W - 32
        lines = _wrap(d, card.purpose, f_purpose, text_w, 3)
        for j, line in enumerate(lines):
            d.text((text_x, ty + 3 + j * 21), line, fill=INK, font=f_purpose)
        oy = ty + 8 + max(1, len(lines)) * 21
        max_op = max(1, (y0 + card_h - PAD - oy) // 19)
        for j, line in enumerate(_wrap(d, card.operation, f_op, THUMB_W, max_op)):
            d.text((x0 + PAD, oy + j * 19), line, fill=MUTED if not card.final else INK, font=f_op)
    return img


def render_report(run_dir: Path, task: str, status: str, verified: float | None, steps,
                  final_shot: bytes | None, dev_w: int, dev_h: int, failure_kind: str | None = None,
                  state_changes: list[str] | None = None, blocked: list[str] | None = None,
                  lang: str = "English") -> list[Path]:
    t = strings_for(lang)
    cards = build_cards(steps, status, verified, final_shot, failure_kind, state_changes, blocked, t)
    numbered = list(enumerate(cards, 1))
    per_page = COLS * ROWS
    chunks = [numbered[i:i + per_page] for i in range(0, len(numbered), per_page)]
    paths = []
    for p, chunk in enumerate(chunks, 1):
        img = _page(task, chunk, p, len(chunks), status, dev_w, dev_h, t)
        path = run_dir / ("report.png" if len(chunks) == 1 else f"report_{p}.png")
        img.save(path, optimize=True)
        paths.append(path)
    return paths
