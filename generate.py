"""
英検準1級 通勤リスニング教材 音声生成スクリプト

使い方:
    pip install edge-tts
    python generate.py              # 全部生成（作成済みの音声はスキップ）
    python generate.py --only lecture   # カテゴリを指定して生成
    python generate.py --force      # 作成済みのmp3も作り直す

content/*.json を読み込み、audio/<カテゴリ>/<id>.mp3 と tracks.js を作ります。
ffmpeg は不要です（無音部分もこのスクリプト内で作ります）。
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import edge_tts

# ===== 設定（好みに合わせて変更できます） =====================================
VOICES = {
    "M": "en-US-GuyNeural",       # 英語・男性
    "F": "en-US-JennyNeural",     # 英語・女性
    "JA": "ja-JP-NanamiNeural",   # 日本語の解説
}
EN_RATE = "+0%"        # 英語の通常スピード（本番に近い）
EN_SLOW_RATE = "-15%"  # ゆっくり版
JA_RATE = "+10%"       # 日本語解説は少し速め

THINK_SEC = 4          # 例文・言い換えの後の「考える時間」
ANSWER_SEC = 7         # 問題を解く時間
GAP = 0.8              # 文と文の間
# ============================================================================

ROOT = Path(__file__).resolve().parent
CONTENT_DIR = ROOT / "content"
AUDIO_DIR = ROOT / "audio"
CACHE_DIR = ROOT / ".cache"
LETTERS = "ABCD"

# edge-tts の出力形式（24kHz・48kbps・モノラル MP3）
BYTES_PER_SEC = 48000 // 8


# ---------------------------------------------------------------------------
# 無音の作成：edge-tts と同じ形式（MPEG-2 Layer III, 24kHz, 48kbps, mono）の
# 無音フレームを直接作るので、そのまま mp3 同士を連結できる。
#   ヘッダー FF F3 64 C0 + 残りゼロ = 1フレーム144バイト = 576サンプル = 24ms
# ---------------------------------------------------------------------------
_SILENT_FRAME = bytes([0xFF, 0xF3, 0x64, 0xC0]) + bytes(140)
_FRAME_SEC = 576 / 24000


def silence(seconds: float) -> bytes:
    return _SILENT_FRAME * max(1, round(seconds / _FRAME_SEC))


# ---------------------------------------------------------------------------
# 音声合成（キャッシュ付き・リトライ付き）
# ---------------------------------------------------------------------------
CONCURRENCY = 2   # 同時に問い合わせる数。エラーが多いときは 1 にする
_sem = None


async def synth(text: str, voice: str, rate: str) -> bytes:
    """edge-tts で音声を作り、ファイルを経由せずメモリで受け取る"""
    data = bytearray()
    async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
        if chunk["type"] == "audio":
            data += chunk["data"]
    if not data:
        raise RuntimeError("音声データが空でした")
    return bytes(data)


def describe(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


async def tts(text: str, voice: str, rate: str) -> bytes:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(CONCURRENCY)
    key = hashlib.sha1(f"{voice}|{rate}|{text}".encode()).hexdigest()
    path = CACHE_DIR / f"{key}.mp3"
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    async with _sem:
        last = None
        for attempt in range(4):
            try:
                data = await synth(text, voice, rate)
                path.write_bytes(data)
                return data
            except Exception as e:  # ネットワークの一時エラーなど
                last = e
                print(f"   retry {attempt + 1}/4 [{text[:30]}]: {describe(e)}")
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"音声を作れませんでした: {text[:40]}\n   最後のエラー: {describe(last)}")


async def preflight() -> bool:
    """最初に短い英文を1つだけ合成して、接続できるか確認する"""
    print(f"接続テスト中…（edge-tts {getattr(edge_tts, '__version__', '?')}）")
    try:
        await synth("Hello.", VOICES["M"], EN_RATE)
        print("接続テスト OK\n")
        return True
    except Exception as e:
        msg = describe(e)
        print("\n×接続テストに失敗しました")
        print("  エラー内容:", msg)
        low = msg.lower()
        print("\n考えられる原因と対処:")
        if "403" in low or "handshake" in low or "skew" in low:
            print("  ・edge-tts が古い可能性が高いです。次を実行してください:")
            print("      py -m pip install -U edge-tts")
            print("  ・パソコンの時計が大きくずれていないかも確認してください。")
        elif "certificate" in low or "sslerror" in low or "ssl error" in low:
            print("  ・会社のネットワーク（プロキシ・セキュリティ製品）が通信を検査している可能性があります。")
            print("    自宅のWi-Fiやスマホのテザリングで試してください。")
        elif "connect" in low or "dns" in low or "timeout" in low or "getaddrinfo" in low:
            print("  ・speech.platform.bing.com に接続できていません。")
            print("    インターネット接続、VPN、会社のファイアウォールを確認してください。")
        elif "voice" in low or "no audio" in low:
            print("  ・声の名前（VOICES）が正しいか確認してください: py -m edge_tts --list-voices")
        else:
            print("  1) py -m pip install -U edge-tts  で最新版にする")
            print("  2) 別のネットワーク（自宅Wi-Fi・テザリング）で試す")
        print("\n単体テスト: py -m edge_tts --text \"Hello\" --write-media test.mp3")
        return False


# ---------------------------------------------------------------------------
# 台本（セグメント列）の組み立て
#   ("say", 話者, 文, 速度)  /  ("pause", 秒)
# ---------------------------------------------------------------------------
def en(speaker, text, slow=False):
    return ("say", speaker, text, EN_SLOW_RATE if slow else EN_RATE)


def ja(text):
    return ("say", "JA", text, JA_RATE)


def pause(sec=GAP):
    return ("pause", sec)


def say_example(example, slow=False):
    """例文：文字列なら男性が読む。[[話者, 文], ...] なら会話として読む"""
    if isinstance(example, str):
        return [en("F", example, slow), pause()]
    segs = []
    for spk, line in example:
        segs += [en(spk, line, slow), pause(0.5)]
    return segs


def build_phrasal(track):
    segs = [ja(track["title"] + "。例文の後に少し間があります。意味を考えてみてください。"), pause(1.5)]
    for i, it in enumerate(track["items"], 1):
        segs += [ja(f"{i}番。"), pause(0.4),
                 en("M", it["phrase"]), pause(), en("M", it["phrase"], slow=True), pause()]
        segs += say_example(it["example"])
        segs += [pause(THINK_SEC),
                 ja(it["example_ja"]), pause(),
                 ja("意味は、" + it["meaning_ja"] + "。"), pause(0.4),
                 ja(it["trap_ja"]), pause(),
                 ja("言い換えると。"), en("M", it["paraphrase"]), pause()]
        segs += say_example(it["example"], slow=True)
        segs += [pause(2)]
    segs += [ja("このトラックは以上です。")]
    return segs


def build_dialogue(track):
    segs = [ja(track["title"] + "。会話の後に質問と選択肢が流れます。話し手の本音を考えてください。"), pause(1.5)]
    for i, it in enumerate(track["items"], 1):
        segs += [ja(f"第{i}問。"), pause()]
        for spk, line in it["lines"]:
            segs += [en(spk, line), pause(0.5)]
        segs += [pause(), en("M", "Question. " + it["question"]), pause()]
        for k, c in enumerate(it["choices"]):
            segs += [en("M", f"{LETTERS[k]}. {c}"), pause(0.6)]
        segs += [pause(ANSWER_SEC),
                 ja(f"正解は{LETTERS[it['answer']]}。"), en("M", it["choices"][it["answer"]]), pause(),
                 ja(it["explain_ja"]), pause(),
                 ja("重要表現。")]
        for phrase, mean in it["keys"]:
            segs += [en("M", phrase), pause(0.4), ja(mean), pause(0.6)]
        segs += [pause(), ja("もう一度、会話を聞きましょう。"), pause(0.5)]
        for spk, line in it["lines"]:
            segs += [en(spk, line), pause(0.5)]
        segs += [pause(2.5)]
    segs += [ja("このトラックは以上です。")]
    return segs


def build_lecture(track):
    q = track["questions"]
    segs = [ja(f"説明文。{track['title_ja']}。"), pause(), en("F", track["title"]), pause(1.2),
            en("F", track["passage"]), pause(1.5)]
    for n, item in enumerate(q, 1):
        segs += [en("M", f"Question {n}. {item['q']}"), pause()]
        for k, c in enumerate(item["choices"]):
            segs += [en("M", f"{LETTERS[k]}. {c}"), pause(0.6)]
        segs += [pause(ANSWER_SEC)]
    for n, item in enumerate(q, 1):
        segs += [ja(f"質問{n}の正解は{LETTERS[item['answer']]}。"), en("M", item["choices"][item["answer"]]), pause(),
                 ja(item["explain_ja"]), pause(1.2)]
    segs += [ja("重要語。")]
    for word, mean in track["vocab"]:
        segs += [en("F", word), pause(0.4), ja(mean), pause(0.6)]
    segs += [pause(), ja("最後に、ゆっくり版をもう一度聞きます。"), pause(),
             en("F", track["passage"], slow=True), pause(1.5),
             ja("このトラックは以上です。")]
    return segs


def build_paraphrase(track):
    segs = [ja(track["title"] + "。英文の後に間があります。選択肢ならどう言い換えられるか、考えてください。"), pause(1.5)]
    for i, it in enumerate(track["items"], 1):
        segs += [ja(f"{i}番。"), pause(0.4),
                 en("M", it["original"]), pause(THINK_SEC),
                 ja("言い換えると。"), en("F", it["paraphrase"]), pause(),
                 ja(it["meaning_ja"]), pause(0.4), ja(it["point_ja"]), pause(),
                 en("M", it["original"]), pause(0.6), en("F", it["paraphrase"]), pause(2)]
    segs += [ja("このトラックは以上です。")]
    return segs


def build_reallife(track):
    """Part 3（Real-Life形式）：状況と質問を先に聞き、その後で音声を聞いて答える"""
    segs = [ja(track["title"] + "。最初に状況と質問を聞いて、何を聞き取るべきか意識してから本文を聞きましょう。"), pause(1.5)]
    for i, it in enumerate(track["items"], 1):
        segs += [ja(f"第{i}問。"), pause(),
                 en("M", "Situation. " + it["situation"]), pause(0.6),
                 en("M", "Question. " + it["question"]), pause(3),
                 ja("では、本文です。"), pause(0.5),
                 en(it.get("speaker", "F"), it["passage"]), pause(1.2),
                 en("M", "Question. " + it["question"]), pause(0.6)]
        for k, c in enumerate(it["choices"]):
            segs += [en("M", f"{LETTERS[k]}. {c}"), pause(0.6)]
        segs += [pause(ANSWER_SEC),
                 ja(f"正解は{LETTERS[it['answer']]}。"), en("M", it["choices"][it["answer"]]), pause(),
                 ja(it["explain_ja"]), pause(2.5)]
    segs += [ja("このトラックは以上です。")]
    return segs


def build_review(track):
    """復習：指定した説明文ファイルの本文だけを連続で読む（slow: true でゆっくり版）"""
    slow = track.get("slow", False)
    segs = [ja(track["title"] + "。説明文の本文だけを続けて流します。"
               + ("ゆっくり版です。音声の少し後について、シャドーイングしてみましょう。" if slow else "")), pause(1.5)]
    for name in track["from"]:
        src = json.loads((CONTENT_DIR / name).read_text(encoding="utf-8"))
        for t in src["tracks"]:
            segs += [ja(t.get("title_ja", t["title"]) + "。"), pause(0.5),
                     en("F", t["passage"], slow=slow), pause(3)]
    segs += [ja("このトラックは以上です。")]
    return segs


BUILDERS = {
    "phrasal": build_phrasal,
    "dialogue": build_dialogue,
    "lecture": build_lecture,
    "paraphrase": build_paraphrase,
    "reallife": build_reallife,
    "review": build_review,
}


# ---------------------------------------------------------------------------
def strip_id3(b: bytes) -> bytes:
    """連結の邪魔になる ID3v2 タグが先頭にあれば取り除く"""
    if b[:3] == b"ID3" and len(b) > 10:
        size = (b[6] << 21) | (b[7] << 14) | (b[8] << 7) | b[9]
        return b[10 + size:]
    return b


async def render(segs) -> bytes:
    jobs = [tts(s[2], VOICES[s[1]], s[3]) for s in segs if s[0] == "say"]
    voiced = iter([strip_id3(b) for b in await asyncio.gather(*jobs)])
    out = bytearray()
    for s in segs:
        out += next(voiced) if s[0] == "say" else silence(s[1])
    return bytes(out)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="phrasal / dialogue / lecture / paraphrase / reallife / review")
    ap.add_argument("--force", action="store_true", help="作成済みのmp3も作り直す")
    args = ap.parse_args()

    CACHE_DIR.mkdir(exist_ok=True)
    if not await preflight():
        return
    playlist = []
    for jf in sorted(CONTENT_DIR.glob("*.json")):
        data = json.loads(jf.read_text(encoding="utf-8"))
        cat = data["category"]
        build = BUILDERS[cat]
        (AUDIO_DIR / cat).mkdir(parents=True, exist_ok=True)
        for track in data["tracks"]:
            out = AUDIO_DIR / cat / f"{track['id']}.mp3"
            rel = out.relative_to(ROOT).as_posix()
            title = track["title"] + (f"（{track['title_ja']}）" if "title_ja" in track else "")
            skip = (args.only and args.only != cat) or (out.exists() and not args.force)
            if not skip:
                print(f"▶ {rel}  {title}")
                out.write_bytes(await render(build(track)))
            if out.exists():
                playlist.append({
                    "category": cat,
                    "category_title": data["category_title"],
                    "title": title,
                    "file": rel,
                    "duration": round(out.stat().st_size / BYTES_PER_SEC),
                })

    js = "window.TRACKS = " + json.dumps(playlist, ensure_ascii=False, indent=1) + ";\n"
    (ROOT / "tracks.js").write_text(js, encoding="utf-8")
    total = sum(t["duration"] for t in playlist)
    print(f"\n完了: {len(playlist)} トラック / 合計 約{total // 60}分  → tracks.js を更新しました")


if __name__ == "__main__":
    asyncio.run(main())
