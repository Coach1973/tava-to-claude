#!/usr/bin/env python3
"""
邀請卡／EDM 一鍵出圖：呼叫 lk888 AIX 聚合引擎的 gpt-image-2 模型，
輸入主題＋欄位＋風格（A/B/C 三選一），由腳本用教練核准的固定範本組成完整 prompt，
輸出 PNG 圖片，可選擇直接推送 Telegram。

做法照產業鏈 SOP（scripts/generate_industry_chain.py）同一套：
圖片描述由腳本寫死，AI 只提供欄位，不自己寫圖片描述；不合格在花 LK888 點數之前就擋下。

用法：
  python3 generate_invitation.py --style B --title "高手都在用的行銷策略" \\
    --date "2026年12月5日（六）14:00-17:00" --venue "台南．大樹教育顧問" \\
    --host "大樹教練" --copy "30 分鐘我們只談一件事：能不能合作" \\
    [--telegram-chat-id 6124913915 --caption "說明文字"] [--dry-run]
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import lk888_guard
from datetime import datetime
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
CRED_FILE = WORKSPACE / "路徑大全_憑證層.md"
LK888_API_URL = "https://api.lk888.ai/v1/images/generations"
# 同一主題送同一個對話，10 分鐘內只做一次（照產業鏈做法，避免重複扣點）。
DEDUP_DIR = Path("/tmp/invitation_dedup")
DEDUP_SECONDS = 600
# LINE 只收公開 HTTPS 網址的圖，所以放進 event.bymyway.com 的公開資料夾，檔名帶亂數外人猜不到。
PUBLIC_IMG_DIR = WORKSPACE / "event-signup" / "invitation_images"
PUBLIC_IMG_BASE = "https://event.bymyway.com/invitation-img/"


def dedup_claim(args):
    """回傳 (lock 路徑, None) 表示可以做；(None, (主題, 幾秒前)) 表示重複要跳過。"""
    topic = (args.topic or args.title or args.prompt or "").strip()
    topic_key = re.sub(r"\s+", "", topic)
    raw = f"{args.telegram_chat_id or '-'}|{topic_key}|{args.style or '-'}"
    lock = DEDUP_DIR / f"{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}.lock"
    DEDUP_DIR.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        age = time.time() - lock.stat().st_mtime
        if age < DEDUP_SECONDS:
            return None, (topic, age)
        lock.unlink(missing_ok=True)
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return None, (topic, 0)
    return lock, None


def get_api_key():
    text = CRED_FILE.read_text(encoding="utf-8")
    m = re.search(r"^API Key：(sk-\S+)", text, re.MULTILINE)
    if not m:
        raise RuntimeError("路徑大全_憑證層.md 裡找不到 lk888 API Key")
    return m.group(1)


def get_telegram_bot_token(config_path: str = None, account: str = "bot_main"):
    cfg_path = Path(config_path) if config_path else Path.home() / ".openclaw" / "openclaw.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return cfg["channels"]["telegram"]["accounts"][account]["botToken"]


def generate_image(prompt: str, size: str = "1024x1536") -> bytes:
    api_key = get_api_key()
    payload = json.dumps({"model": "gpt-image-2", "prompt": prompt, "size": size}).encode("utf-8")
    req = urllib.request.Request(
        LK888_API_URL,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with lk888_guard.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    b64 = data["data"][0]["b64_json"]
    return base64.b64decode(b64)


def send_telegram_photo(chat_id: str, image_path: Path, caption: str = "", config_path: str = None, account: str = "bot_main"):
    bot_token = get_telegram_bot_token(config_path, account)
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    boundary = "----InvitationBoundary"
    body = []
    body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode("utf-8"))
    if caption:
        body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode("utf-8"))
    body.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{image_path.name}\"\r\nContent-Type: image/png\r\n\r\n".encode(
            "utf-8"
        )
    )
    body.append(image_path.read_bytes())
    body.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    data = b"".join(body)
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram送出失敗: {result}")
    return result


def notify_failure(args, err):
    """背景出圖失敗時要讓群組知道，不能靜悄悄。"""
    try:
        token = get_telegram_bot_token(args.openclaw_config, args.telegram_account)
        data = json.dumps({"chat_id": args.telegram_chat_id, "text": "這張圖這次沒做成功（已自動放棄，沒有重複扣點），請稍後再說一次，或請教練協助。"}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, headers={"Content-Type": "application/json"}), timeout=20)
    except Exception:
        pass


# 三種固定風格（教練核准後不要隨意改，改風格＝改範本，要教練同意）：
# A 喜慶金紅：週年慶、開幕、慶功、頒獎
# B 沉穩深藍金：講座、商務、BNI、課程招生
# C 清新手繪：社團、親子、公益、俱樂部活動（與產業鏈同一畫風）
STYLES = {
    "A": {
        "name": "喜慶金紅",
        "desc": "喜慶金紅風格：深紅底、金色線條與金箔光澤點綴，紅色燈籠與金色流蘇裝飾，大氣喜慶不俗氣",
    },
    "B": {
        "name": "沉穩深藍金",
        "desc": "沉穩商務風格：深藍底、金色細線框與低調金色光暈，簡潔大氣，適合講座與商務場合",
    },
    "C": {
        "name": "清新手繪",
        "desc": "手繪可愛風格：米白底、水彩筆觸、柔和暖色，像產業鏈夥伴圖同一畫風，親切不正式",
    },
}

PROMPT_TEMPLATE = (
    "我需要做一張邀請卡，{style_desc}。"
    "版面由上而下：{layout}"
    "所有文字必須完全正確、清晰可讀，每個文字都必須是繁體中文（台灣正體字），"
    "禁止使用簡體字、簡體字形、簡體偏旁、簡體異體字、或任何簡體中文變體，"
    "禁止出現任何英文或拼音，絕對不能出現無法顯示的亂碼字、缺漏字或方框字。"
)

TITLE_MAX = 14
COPY_MAX = 40
ENGLISH_RE = re.compile(r"[A-Za-z]")


def build_prompt_from_fields(args):
    """正式流程：欄位必須齊全、字數合規，才用核准範本組成圖片描述；不合格直接結束，不呼叫 LK888。"""
    bad = []
    if args.style not in STYLES:
        bad.append("風格必須是 A、B、C 三選一")
    if not args.title or not args.title.strip():
        bad.append("主標題一定要給")
    elif len(args.title.strip()) > TITLE_MAX:
        bad.append(f"主標題最多 {TITLE_MAX} 個字，目前 {len(args.title.strip())} 個字")
    if not args.date or not args.date.strip():
        bad.append("日期時間一定要給（例如：2026年12月5日（六）14:00-17:00）")
    if not args.venue or not args.venue.strip():
        bad.append("地點一定要給")
    if args.copy and len(args.copy.strip()) > COPY_MAX:
        bad.append(f"邀請語最多 {COPY_MAX} 個字，目前 {len(args.copy.strip())} 個字")
    for label, value in [("主標題", args.title), ("副標", args.subtitle), ("日期時間", args.date),
                         ("地點", args.venue), ("主辦人", args.host), ("邀請語", args.copy)]:
        if value and ENGLISH_RE.search(value):
            bad.append(f"{label}裡面有英文字母，請改成繁體中文（數字與標點符號可以）")
    if bad:
        print("❌ 欄位不合格，沒有出圖也沒有扣點。請改好這些再下一次指令：\n  " + "\n  ".join(bad))
        sys.exit(2)
    # 沒填的欄位不寫進描述（不要出現「副標」「」這種空字串）。
    layout = f"主標題「{args.title.strip()}」最大、最醒目置中；"
    if (args.subtitle or "").strip():
        layout += f"副標「{args.subtitle.strip()}」在主標題下方小一號；"
    layout += f"接著是日期時間「{args.date.strip()}」、地點「{args.venue.strip()}」兩行資訊，清楚好讀；"
    if (args.host or "").strip():
        layout += f"主辦人「{args.host.strip()}」寫在資訊下方；"
    if (args.copy or "").strip():
        layout += f"底部一句邀請語「{args.copy.strip()}」。"
    return PROMPT_TEMPLATE.format(style_desc=STYLES[args.style]["desc"], layout=layout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--style", default=None, help="風格三選一：A 喜慶金紅、B 沉穩深藍金、C 清新手繪")
    parser.add_argument("--title", default=None, help="主標題（最多14個字，一定要給）")
    parser.add_argument("--subtitle", default="", help="副標（可不填）")
    parser.add_argument("--date", default=None, help="日期時間（一定要給）")
    parser.add_argument("--venue", default=None, help="地點（一定要給）")
    parser.add_argument("--host", default="", help="主辦人（可不填）")
    parser.add_argument("--copy", default="", help="底部邀請語（最多40個字，可不填）")
    parser.add_argument("--prompt", default=None, help="完整圖片描述；正式流程改用欄位由範本自動組成，不建議直接給 prompt")
    parser.add_argument("--size", default="1024x1536", help="1024x1536 直式（預設）、1536x1024 橫式、1024x1024 方形")
    parser.add_argument("--out", default=None)
    parser.add_argument("--telegram-chat-id", default=None)
    parser.add_argument("--caption", default="")
    parser.add_argument("--openclaw-config", default=None, help="非預設環境時指定")
    parser.add_argument("--telegram-account", default="bot_main", help="要用哪個bot帳號送出")
    parser.add_argument("--topic", default=None, help="防重複用的主題；沒給就用主標題")
    parser.add_argument("--force", action="store_true", help="略過10分鐘防重複")
    parser.add_argument("--background", action="store_true", help="馬上回覆「已開始」，圖在背景做完後自己送進--telegram-chat-id（出圖要1~2分鐘，呼叫端不必等）")
    parser.add_argument("--publish", action="store_true", help="另存一份到公開網址（LINE送圖要用）")
    parser.add_argument("--dry-run", action="store_true", help="只檢查欄位、印出組好的圖片描述，不呼叫 LK888、不扣點（實測先用這個）")
    parser.add_argument("--_lock", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.prompt:
        prompt = args.prompt
    else:
        prompt = build_prompt_from_fields(args)

    if args.dry_run:
        print("✅ 欄位檢查通過，組好的圖片描述如下（沒有出圖、沒有扣點）：")
        print(prompt)
        return

    lock = None
    if not args.force:
        lock, dup = dedup_claim(args)
        if dup:
            print(f"⏭️ 「{dup[0]}」{int(dup[1])}秒前才生成過同一張，10分鐘內不重複扣點，這次跳過（真的要重做請加 --force）")
            return
    if args.background and args.telegram_chat_id:
        # 家長程序只負責「接單」：鎖已搶到，交給脫離終端機的子程序做完並送出，自己立刻回覆
        import subprocess
        child_argv = [a for a in sys.argv[1:] if a != "--background"] + ["--force"]
        if lock:
            child_argv += ["--_lock", str(lock)]
        log = open(WORKSPACE / "state" / "invitation_background.log", "a")
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())] + child_argv,
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        print("✅ 已開始製作，約 1~2 分鐘後圖會自動傳到群組，不用重複下指令。")
        return
    lock = Path(args._lock) if args._lock else lock
    try:
        image_bytes = generate_image(prompt, args.size)
    except Exception as e:
        if lock:
            lock.unlink(missing_ok=True)  # 生圖失敗要讓下一次能重試，不能被鎖10分鐘
        if args.telegram_chat_id and args._lock:
            notify_failure(args, e)
        raise

    if args.out:
        out_path = Path(args.out)
    else:
        slug = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = WORKSPACE / "claude-brain" / "data" / f"invitation_{slug}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(image_bytes)
    print(f"圖片已存檔：{out_path}")

    if args.publish:
        PUBLIC_IMG_DIR.mkdir(parents=True, exist_ok=True)
        public_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{os.urandom(6).hex()}.png"
        (PUBLIC_IMG_DIR / public_name).write_bytes(image_bytes)
        print(f"公開網址（LINE送圖用）：{PUBLIC_IMG_BASE}{public_name}")

    if args.telegram_chat_id:
        send_telegram_photo(args.telegram_chat_id, out_path, args.caption, args.openclaw_config, args.telegram_account)
        print(f"已推送Telegram（chat_id={args.telegram_chat_id}，帳號={args.telegram_account}）")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"錯誤：{e}", file=sys.stderr)
        sys.exit(1)
