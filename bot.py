"""
ScoutBot — Railway Edition
"""

import os, cv2, csv, subprocess, threading, logging, asyncio, uuid, re
from pathlib import Path
from datetime import datetime

import numpy as np
import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
import yt_dlp
from flask import Flask, render_template, request, jsonify, send_file
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "8417374602:AAHgzLA5YJp3oEtCPklpgdYI-BqolIhDeW4")
CHAT_ID    = os.environ.get("CHAT_ID",   "7125492867")
PUBLIC_URL = os.environ.get("PUBLIC_URL") or "https://stats-production-a62b.up.railway.app"
PORT       = int(os.environ.get("PORT", 5055))
WORK_DIR   = Path(os.environ.get("WORK_DIR", "/data"))
CSV_PATH   = WORK_DIR / "gols.csv"

SECONDS_BEFORE = 15
MIN_STATIC     = 2.0
DIFF_THRESHOLD = 45
PADDING_AFTER  = 2

WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s — %(message)s", level=logging.INFO)
log = logging.getLogger("scoutbot")
log.info(f"FFMPEG path: {FFMPEG}")

pending_clips: dict[str, dict] = {}
_frames_cache: dict[str, list] = {}
_frame_b64_cache: dict[str, str] = {}

# ─── SESSÕES EM DISCO ────────────────────────────────────────────────────────
def session_path(session_id: str) -> Path:
    return WORK_DIR / f"session_{session_id}.json"

def session_save(session_id: str, data: dict):
    import json
    to_save = {k: v for k, v in data.items() if k not in ("frame_b64", "frames")}
    with open(session_path(session_id), "w") as f:
        json.dump(to_save, f)

def session_load(session_id: str):
    import json
    if not session_id:
        return None
    p = session_path(session_id)
    if not p.exists():
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except:
        return None

def session_delete(session_id: str):
    try: session_path(session_id).unlink()
    except: pass

# ─── CSV ─────────────────────────────────────────────────────────────────────
def csv_ensure():
    if not CSV_PATH.exists():
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "data", "hora", "timestamp_video",
                "jogador_gol", "num_gol",
                "jogador_assist", "num_assist", "video_origem"
            ])

def csv_save(row: dict):
    csv_ensure()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            row.get("data", ""), row.get("hora", ""),
            row.get("timestamp_video", ""),
            row.get("jogador_gol", ""), row.get("num_gol", ""),
            row.get("jogador_assist", ""), row.get("num_assist", ""),
            row.get("video_origem", ""),
        ])

# ─── VÍDEO ───────────────────────────────────────────────────────────────────
def get_three_frames_b64(video_path: str) -> list:
    import base64
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps
    t1 = 20 * 60
    t2 = duration / 2
    t3 = max(0, duration - 20 * 60)
    timestamps = [
        (t1, "🕐 +20min do início"),
        (t2, f"🕑 Meio ({int(t2//60)}min)"),
        (t3, f"🕒 -20min do final ({int(t3//60)}min)"),
    ]
    results = []
    for t, label in timestamps:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ret, frame = cap.read()
        if not ret:
            continue
        h, w = frame.shape[:2]
        frame = cv2.resize(frame, (960, int(h * 960 / w)))
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        results.append({"b64": base64.b64encode(buf).decode(), "label": label, "seconds": t})
    cap.release()
    return results

def get_frame_size(video_path: str):
    cap = cv2.VideoCapture(video_path)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return w, h

def ts_fmt(seconds: float) -> str:
    s = int(seconds)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def _convert_dropbox_url(url):
    url = re.sub(r'[?&]dl=0', '?dl=1', url)
    if 'dl=1' not in url:
        url += '?dl=1' if '?' not in url else '&dl=1'
    return url.replace('www.dropbox.com', 'dl.dropboxusercontent.com')

def _download_direct(url, session_id):
    import requests as req
    out_path = WORK_DIR / f"video_{session_id}.mp4"
    try:
        with req.get(url, stream=True, timeout=600, allow_redirects=True) as r:
            r.raise_for_status()
            with open(out_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        if out_path.exists() and out_path.stat().st_size > 1000:
            return str(out_path), out_path.name, None
        return None, None, "Arquivo vazio"
    except Exception as e:
        return None, None, str(e)

def _download_url(url: str, session_id: str):
    base_out = str(WORK_DIR / f"video_{session_id}")

    if 'drive.google.com' in url:
        log.info("Google Drive detectado")
        try:
            import gdown
            out_path = str(WORK_DIR / f"video_{session_id}.mp4")
            gdown.download(url, out_path, quiet=False)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                return out_path, f"video_{session_id}.mp4", None
            return None, None, "Arquivo vazio após download"
        except Exception as e:
            return None, None, str(e)

    if 'dropbox.com' in url or 'dropboxusercontent.com' in url:
        log.info("Dropbox detectado")
        return _download_direct(_convert_dropbox_url(url), session_id)

    last_error = ""
    for fmt in ["best", "worst", "bestvideo+bestaudio/best"]:
        ydl_opts = {
            "format": fmt,
            "outtmpl": base_out + ".%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "merge_output_format": "mp4",
            "cookiefile": str(WORK_DIR / "cookies.txt") if (WORK_DIR / "cookies.txt").exists() else None,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                video_name = info.get("title", f"video_{session_id}") + ".mp4"
            candidates = list(WORK_DIR.glob(f"video_{session_id}.*"))
            if candidates:
                return str(candidates[0]), video_name, None
        except Exception as e:
            last_error = str(e)
            continue
    return None, None, last_error

# ─── FFMPEG FUNCTIONS ────────────────────────────────────────────────────────
def detectar_segmentos(video_path: str, X, Y, W, H) -> list:
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    prev = None
    moving = False
    start = 0
    segments = []
    frame_id = 0
    last_change_time = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        crop = frame[Y:Y+H, X:X+W]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        tempo = frame_id / fps

        if prev is not None:
            score = np.mean(cv2.absdiff(prev, gray))
            if score > DIFF_THRESHOLD:
                last_change_time = tempo
                if not moving:
                    moving = True
                    start = tempo
            if moving and (tempo - last_change_time > MIN_STATIC):
                end = last_change_time
                if end - start > 2:
                    segments.append((start, end))
                moving = False

        prev = gray
        frame_id += 1

    cap.release()
    return segments

def cortar_segmento(video, start, end, output, padding=1.5):
    subprocess.run([
        FFMPEG, "-y",
        "-ss", str(max(0, start - padding)),
        "-i", video,
        "-t", str((end - start) + padding * 2),
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1",
        "-c:v", "libx264", "-preset", "fast", "-crf", "28",
        "-c:a", "aac", output
    ], capture_output=True)
    return os.path.exists(output)

def juntar_clips(clips, output):
    if not clips:
        return False
    lista = str(WORK_DIR / "lista.txt")
    with open(lista, "w") as f:
        for c in clips:
            f.write(f"file '{os.path.abspath(c)}'\n")
    subprocess.run([
        FFMPEG, "-y", "-f", "concat", "-safe", "0",
        "-i", lista, "-c", "copy", output
    ], capture_output=True)
    try: os.remove(lista)
    except: pass
    return os.path.exists(output)

def comprimir_video(input_video, output):
    subprocess.run([
        FFMPEG, "-y", "-i", input_video,
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1",
        "-c:v", "libx264", "-crf", "32", "-preset", "fast",
        "-c:a", "aac", output
    ], capture_output=True)
    return os.path.exists(output)

# ─── FLASK ───────────────────────────────────────────────────────────────────
flask_app = Flask(__name__, template_folder="templates")

@flask_app.route("/")
def index():
    return "<h2>⚽ ScoutBot online!</h2><p>Mande um link no Telegram para começar.</p>"

@flask_app.route("/roi/<session_id>")
def roi_page(session_id):
    try:
        s = session_load(session_id)
        if not s:
            return "<h2>⚠️ Sessão expirada</h2><p>Mande o link do Drive novamente no Telegram.</p>", 404
        video_path = s.get("video_path", "")
        if not video_path or not os.path.exists(video_path):
            return "<h2>⚠️ Vídeo não encontrado</h2><p>Mande o link do Drive novamente no Telegram.</p>", 404
        return render_template("select_roi.html", session_id=session_id)
    except Exception as e:
        log.error(f"roi_page error: {e}")
        return f"<h2>Erro</h2><p>{e}</p>", 500

@flask_app.route("/api/frame/<session_id>")
def api_frame(session_id):
    try:
        s = session_load(session_id)
        if not s:
            return jsonify({"ready": False, "error": "sessao_nao_encontrada"})
        if session_id not in _frames_cache:
            video_path = s.get("video_path", "")
            if video_path and os.path.exists(video_path):
                frames = get_three_frames_b64(video_path)
                _frames_cache[session_id] = frames
                _frame_b64_cache[session_id] = frames[0]["b64"] if frames else ""
            else:
                return jsonify({"ready": False, "error": "video_nao_encontrado"})
        frames = _frames_cache.get(session_id, [])
        return jsonify({"frames": frames, "frame": _frame_b64_cache.get(session_id, ""), "ready": bool(frames)})
    except Exception as e:
        log.error(f"api_frame error: {e}")
        return jsonify({"ready": False, "error": str(e)})

@flask_app.route("/api/roi/<session_id>", methods=["POST"])
def api_roi(session_id):
    try:
        s = session_load(session_id)
        if not s:
            return jsonify({"ok": False, "error": "Sessão inválida"})
        data = request.json
        vw, vh = get_frame_size(s["video_path"])
        X = max(0, int(data["x"] * vw))
        Y = max(0, int(data["y"] * vh))
        W = max(1, min(int(data["w"] * vw), vw - X))
        H = max(1, min(int(data["h"] * vh), vh - Y))
        s["roi"] = [X, Y, W, H]
        s["roi_ready"] = True
        session_save(session_id, s)
        log.info(f"ROI salvo {session_id}: {X},{Y},{W},{H}")
        return jsonify({"ok": True, "roi": [X, Y, W, H]})
    except Exception as e:
        log.error(f"api_roi error: {e}")
        return jsonify({"ok": False, "error": str(e)})

@flask_app.route("/api/csv")
def download_csv():
    csv_ensure()
    return send_file(str(CSV_PATH.resolve()), as_attachment=True, download_name="gols.csv")

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ─── BOT ─────────────────────────────────────────────────────────────────────
AGUARD_NUM_GOL, AGUARD_NOME_GOL, AGUARD_ASSIST, AGUARD_NUM_ASSIST, AGUARD_NOME_ASSIST = range(5)
URL_PATTERN = re.compile(r'https?://\S+')

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ *ScoutBot ativo!*\n\nCola um link (Drive ou Dropbox).\n\n"
        "/csv — planilha\n/status — servidor\n/cancelar — cancelar",
        parse_mode="Markdown"
    )

async def cmd_status(update, ctx):
    cookies_path = WORK_DIR / "cookies.txt"
    exists = cookies_path.exists()
    size = cookies_path.stat().st_size if exists else 0
    files = [f.name for f in WORK_DIR.iterdir()] if WORK_DIR.exists() else []
    await update.message.reply_text(
        "WORK_DIR: " + str(WORK_DIR) + "\ncookies.txt: " + str(exists) +
        "\nTamanho: " + str(size) + " bytes\nArquivos: " + str(files) +
        "\nFFMPEG: " + FFMPEG
    )

async def cmd_csv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    csv_ensure()
    await update.message.reply_document(
        document=open(CSV_PATH, "rb"), filename="gols.csv", caption="📊 Planilha de gols"
    )

async def handle_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or "cookies" not in doc.file_name.lower():
        return
    tg_file = await ctx.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(str(WORK_DIR / "cookies.txt"))
    await update.message.reply_text("✅ *cookies.txt salvo!*", parse_mode="Markdown")

async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    file_obj = update.message.video or update.message.document
    if not file_obj:
        return
    session_id = str(uuid.uuid4())[:8]
    try:
        await update.message.reply_text("📥 Baixando arquivo... aguarda.")
        tg_file = await ctx.bot.get_file(file_obj.file_id, read_timeout=300, write_timeout=300, connect_timeout=300)
        video_path = str(WORK_DIR / f"video_{session_id}.mp4")
        await tg_file.download_to_drive(video_path, read_timeout=600)
    except Exception as e:
        await update.message.reply_text(f"❌ Erro:\n`{e}`", parse_mode="Markdown")
        return
    video_name = getattr(file_obj, "file_name", None) or f"video_{session_id}.mp4"
    frames = get_three_frames_b64(video_path)
    _frames_cache[session_id] = frames
    _frame_b64_cache[session_id] = frames[0]["b64"] if frames else ""
    session_save(session_id, {"video_path": video_path, "video_name": video_name, "roi": None, "roi_ready": False})
    ctx.user_data["session_id"] = session_id
    roi_url = f"{PUBLIC_URL.rstrip('/')}/roi/{session_id}"
    await update.message.reply_text(
        f"✅ Vídeo recebido!\n\n👇 Marque o placar:\n{roi_url}\n\nDepois mande /pronto",
        disable_web_page_preview=True
    )

async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    match = URL_PATTERN.search(text)
    if not match:
        return
    url = match.group(0)
    session_id = str(uuid.uuid4())[:8]
    ctx.user_data["session_id"] = session_id
    msg = await update.message.reply_text("⏳ Baixando vídeo no Railway... pode levar alguns minutos.")
    loop = asyncio.get_event_loop()
    video_path, video_name, error = await loop.run_in_executor(None, _download_url, url, session_id)
    if error or not video_path:
        await msg.edit_text(f"❌ Erro:\n`{error}`\n\nVerifica se o link é público.", parse_mode="Markdown")
        return
    frames = get_three_frames_b64(video_path)
    _frames_cache[session_id] = frames
    _frame_b64_cache[session_id] = frames[0]["b64"] if frames else ""
    session_save(session_id, {"video_path": video_path, "video_name": video_name, "roi": None, "roi_ready": False})
    roi_url = f"{PUBLIC_URL.rstrip('/')}/roi/{session_id}"
    await msg.edit_text(
        f"✅ *{video_name}*\n\n👇 Marque o placar:\n{roi_url}\n\nDepois mande /pronto",
        parse_mode="Markdown", disable_web_page_preview=True
    )

async def cmd_pronto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session_id = ctx.user_data.get("session_id")
    s = session_load(session_id)
    if not s:
        await update.message.reply_text("❌ Nenhum vídeo. Manda um link primeiro.")
        return
    if not s.get("roi_ready"):
        roi_url = f"{PUBLIC_URL.rstrip('/')}/roi/{session_id}"
        await update.message.reply_text(f"⚠️ Área não selecionada!\n\nAcesse: {roi_url}")
        return
    await update.message.reply_text("🔍 Analisando... aguarda os clipes!")
    asyncio.ensure_future(_processar(session_id, ctx))

async def _processar(session_id: str, ctx: ContextTypes.DEFAULT_TYPE):
    s = session_load(session_id)
    if not s:
        await ctx.bot.send_message(CHAT_ID, "❌ Sessão expirada.")
        return
    video_path = s["video_path"]
    video_name = s["video_name"]
    X, Y, W, H = s["roi"]

    if not os.path.exists(video_path):
        await ctx.bot.send_message(CHAT_ID, "❌ Vídeo não encontrado. Manda o link de novo.")
        return

    await ctx.bot.send_message(CHAT_ID, f"⚙️ Detectando mudanças em *{video_name}*...", parse_mode="Markdown")

    segments = detectar_segmentos(video_path, X, Y, W, H)

    if not segments:
        await ctx.bot.send_message(CHAT_ID, "⚠️ Nenhuma mudança detectada. Tenta selecionar uma área maior.")
        try: os.remove(video_path)
        except: pass
        session_delete(session_id)
        return

    await ctx.bot.send_message(CHAT_ID, f"✅ {len(segments)} mudança(s) detectada(s)! Cortando e enviando...")

    clips = []
    for j, (s_time, e_time) in enumerate(segments):
        out = str(WORK_DIR / f"clip_{session_id}_{j}.mp4")
        if cortar_segmento(video_path, s_time, e_time, out):
            clips.append(out)

    if not clips:
        await ctx.bot.send_message(CHAT_ID, "❌ Erro ao cortar clipes.")
        return

    final = str(WORK_DIR / f"final_{session_id}.mp4")
    final_small = str(WORK_DIR / f"final_small_{session_id}.mp4")

    ok = juntar_clips(clips, final)
    if not ok:
        await ctx.bot.send_message(CHAT_ID, "❌ Erro ao juntar clipes.")
        return

    ok = comprimir_video(final, final_small)
    if not ok:
        final_small = final

    ts_inicio = ts_fmt(segments[0][0])
    ts_fim = ts_fmt(segments[-1][1])
    clip_id = f"{session_id}_final"
    pending_clips[clip_id] = {"path": final_small, "timestamp": ts_inicio, "video_origem": video_name}

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚽ Registrar gols", callback_data=f"gol:{clip_id}")
    ]])

    try:
        with open(final_small, "rb") as f:
            await ctx.bot.send_document(
                chat_id=CHAT_ID,
                document=f,
                filename=f"gols_{video_name}",
                caption=(
                    f"⚽ *{len(segments)} mudança(s) de placar*\n"
                    f"🕐 {ts_inicio} → {ts_fim}\n"
                    f"Clique para registrar os gols."
                ),
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
    except Exception as e:
        log.error(f"Erro ao enviar documento: {e}")
        await ctx.bot.send_message(CHAT_ID, f"❌ Erro ao enviar: {e}")

    for c in clips:
        try: os.remove(c)
        except: pass
    try: os.remove(final)
    except: pass
    try: os.remove(video_path)
    except: pass
    session_delete(session_id)
    _frames_cache.pop(session_id, None)
    _frame_b64_cache.pop(session_id, None)

# ─── REGISTRO DE GOL ─────────────────────────────────────────────────────────
async def cb_registrar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    clip_id = query.data.split(":", 1)[1]
    ctx.user_data["clip_id"] = clip_id
    clip = pending_clips.get(clip_id, {})
    ctx.user_data["timestamp"] = clip.get("timestamp", "?")
    ctx.user_data["video_origem"] = clip.get("video_origem", "?")
    await query.message.reply_text(
        f"⚽ Gol em *{ctx.user_data['timestamp']}*\n\nNúmero da camisa do goleador:",
        parse_mode="Markdown"
    )
    return AGUARD_NUM_GOL

async def recv_num_gol(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["num_gol"] = update.message.text.strip()
    await update.message.reply_text("Nome do goleador:")
    return AGUARD_NOME_GOL

async def recv_nome_gol(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["jogador_gol"] = update.message.text.strip()
    kb = ReplyKeyboardMarkup([["Sim", "Não"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Teve assistência?", reply_markup=kb)
    return AGUARD_ASSIST

async def recv_assist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() in ("sim", "s"):
        await update.message.reply_text("Número da camisa de quem assistiu:")
        return AGUARD_NUM_ASSIST
    await _salvar(update, ctx, "", "")
    return ConversationHandler.END

async def recv_num_assist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["num_assist"] = update.message.text.strip()
    await update.message.reply_text("Nome de quem assistiu:")
    return AGUARD_NOME_ASSIST

async def recv_nome_assist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _salvar(update, ctx, ctx.user_data.get("num_assist", ""), update.message.text.strip())
    return ConversationHandler.END

async def _salvar(update, ctx, num_assist, nome_assist):
    now = datetime.now()
    csv_save({
        "data": now.strftime("%d/%m/%Y"), "hora": now.strftime("%H:%M:%S"),
        "timestamp_video": ctx.user_data.get("timestamp", ""),
        "jogador_gol": ctx.user_data.get("jogador_gol", ""),
        "num_gol": ctx.user_data.get("num_gol", ""),
        "jogador_assist": nome_assist, "num_assist": num_assist,
        "video_origem": ctx.user_data.get("video_origem", ""),
    })
    clip_id = ctx.user_data.get("clip_id", "")
    if clip_id in pending_clips:
        try: os.remove(pending_clips[clip_id]["path"])
        except: pass
        del pending_clips[clip_id]
    assist_str = f"\n🅰️ #{num_assist} {nome_assist}" if nome_assist else ""
    await update.message.reply_text(
        f"✅ *Gol salvo!*\n⚽ #{ctx.user_data.get('num_gol','')} {ctx.user_data.get('jogador_gol','')}{assist_str}\n"
        f"🕐 {ctx.user_data.get('timestamp','')}\n\n_Use /csv para baixar._",
        parse_mode="Markdown"
    )

async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelado.")
    return ConversationHandler.END

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    csv_ensure()
    threading.Thread(target=run_flask, daemon=True).start()
    log.info(f"🌐 Flask porta {PORT} | {PUBLIC_URL}")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_registrar, pattern=r"^gol:")],
        states={
            AGUARD_NUM_GOL:     [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_num_gol)],
            AGUARD_NOME_GOL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_nome_gol)],
            AGUARD_ASSIST:      [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_assist)],
            AGUARD_NUM_ASSIST:  [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_num_assist)],
            AGUARD_NOME_ASSIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_nome_assist)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pronto", cmd_pronto))
    app.add_handler(CommandHandler("csv", cmd_csv))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookies))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("🤖 Bot iniciado!")
    app.run_polling()

if __name__ == "__main__":
    main()
