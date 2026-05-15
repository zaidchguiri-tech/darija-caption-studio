import os, json, time, subprocess, traceback, logging, sys, requests as req_lib
from flask import Flask, request, jsonify, render_template, send_from_directory, Response
import httpx
from openai import OpenAI

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('darija')

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB
app.config['PROPAGATE_EXCEPTIONS'] = False

PORT     = int(os.environ.get('PORT', 8080))
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── API Credentials ─────────────────────────────────────────────────────────
OPENAI_KEY  = os.environ.get('OPENAI_API_KEY___')  or os.environ.get('OPENAI_API_KEY', '')
GEMINI_KEY  = os.environ.get('GEMINI_API_KEY___')  or os.environ.get('GEMINI_API_KEY', '')
OPENAI_BASE = (os.environ.get('OPENAI_BASE_URL')  or '').rstrip('/')
GEMINI_BASE = (os.environ.get('GEMINI_BASE_URL')   or '').rstrip('/')
CF_TOKEN    = os.environ.get('CF_AIG_TOKEN', '')

_proxy_hdrs = {'cf-aig-authorization': f'Bearer {CF_TOKEN}'} if CF_TOKEN else {}

openai_client = None

if OPENAI_KEY:
    openai_client = OpenAI(
        api_key=OPENAI_KEY,
        base_url=OPENAI_BASE or None,
    )

log.info(f"OpenAI: {'YES' if len(OPENAI_KEY)>10 else 'NO'} | "
         f"Gemini: {'YES' if len(GEMINI_KEY)>10 else 'NO'} | "
         f"CF proxy: {'SET' if CF_TOKEN else 'off'}")

ALLOWED = {'mp3','mp4','wav','m4a','webm','ogg','flac','mov','avi','mkv'}
def allowed_file(fn): return '.' in fn and fn.rsplit('.',1)[1].lower() in ALLOWED

# ── JSON error handlers ──────────────────────────────────────────────────────
def json_error(msg, code):
    log.error(f"HTTP {code}: {msg}")
    r = jsonify({'error': msg, 'status': code}); r.status_code = code; return r

@app.errorhandler(400)    ; return json_error(str(e := request.exception or 'Bad request'), 400)
@app.errorhandler(404)    ; return json_error('Endpoint not found', 404)
@app.errorhandler(413)    ; return json_error('File too large (max 500 MB)', 413)
@app.errorhandler(500)
def e500(e): return json_error('Internal server error', 500)
@app.errorhandler(Exception)
def eall(e):
    log.error(f"Unhandled: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    return jsonify({'error': f'{type(e).__name__}: {e}'}), 500

# ── Audio extraction ───────────────────────────────────────────────────────
def extract_audio(input_path):
    """
    IMPORTANT: Write ffmpeg output to /tmp (local disk).
    Network filesystems (like Vercel's ephemeral FS) cannot handle
    ffmpeg's seek()-dependent MP3 header writing.
    """
    basename = os.path.splitext(os.path.basename(input_path))[0]
    out = f'/tmp/{basename}_audio.mp3'
    log.info(f"ffmpeg: {input_path} → {out}")

    r = subprocess.run(
        ['ffmpeg', '-y', '-i', input_path,
         '-vn', '-acodec', 'libmp3lame',
         '-ar', '16000', '-ac', '1', '-q:a', '4', out],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-300:]}")
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError(f"ffmpeg produced empty output: {out}")
    log.info(f"Audio extracted: {os.path.getsize(out):,} bytes")
    return out

# ── Stage 1: Whisper transcription ────────────────────────────────────────
def whisper_transcribe(audio_path):
    log.info(f"[Whisper] → {audio_path}")
    with open(audio_path, 'rb') as f:
        resp = openai_client.audio.transcriptions.create(
            model='whisper-1',
            file=f,
            language='ar',
            response_format='verbose_json',
            timestamp_granularities=['segment'],
        )
    raw = resp.segments if hasattr(resp, 'segments') and resp.segments else []
    log.info(f"[Whisper] {len(raw)} segments | text: {(resp.text or '')[:80]}")
    if raw:
        return [{'id': i, 'start': round(float(s.start), 3),
                  'end':   round(float(s.end),   3),
                  'text':  s.text.strip()}
                 for i, s in enumerate(raw)]
    return [{'id': 0, 'start': 0.0, 'end': 30.0, 'text': (resp.text or '').strip()}]

# ── Stage 2A: Gemini Darija refinement ─────────────────────────────────────
def _gemini_refine(texts):
    if not GEMINI_KEY or not GEMINI_BASE:
        raise RuntimeError("Gemini not configured")
    prompt = (
        'You are an expert in Moroccan Darija dialect.\n'
        'Rewrite each line into clean, natural Moroccan Darija.\n'
        '- Fix spelling and grammar errors\n'
        '- Replace non-Darija words with natural Darija equivalents\n'
        '- Remove filler words and repetition\n'
        '- Preserve meaning and approximate length\n'
        '- Do NOT translate — only improve the Darija\n\n'
        f'Return ONLY a valid JSON array of improved strings, same order, no markdown.\n\n'
        f'Lines:\n{json.dumps(texts, ensure_ascii=False)}'
    )
    r = req_lib.post(
        f'{GEMINI_BASE}/models/gemini-2.0-flash:generateContent',
        headers={'Content-Type': 'application/json', **_proxy_hdrs},
        params={'key': GEMINI_KEY},
        json={'contents': [{'parts': [{'text': prompt}]}]},
        timeout=45,
    )
    if r.status_code == 429:
        raise RuntimeError("Gemini quota exceeded (429)")
    if not r.ok:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:200]}")
    raw = r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
    s = raw.find('['); e = raw.rfind(']') + 1
    if s == -1 or e == 0:
        raise ValueError(f"Gemini bad format: {raw[:150]}")
    return json.loads(raw[s:e])

# ── Stage 2B: GPT-4o-mini Darija refinement (fallback) ───────────────────
def _gpt_refine(texts):
    prompt = (
        'You are an expert in Moroccan Darija dialect.\n'
        'Rewrite each line into clean, natural Moroccan Darija.\n'
        '- Fix spelling and grammar errors\n'
        '- Replace non-Darija words with natural Darija equivalents\n'
        '- Remove filler words and repetition\n'
        '- Preserve meaning and approximate length\n'
        '- Do NOT translate — only improve the Darija\n\n'
        f'Return ONLY a valid JSON array of improved strings, same order, no markdown.\n\n'
        f'Lines:\n{json.dumps(texts, ensure_ascii=False)}'
    )
    resp = openai_client.chat.completions.create(
        model='gpt-4o-mini',
        messages=[{'role': 'user', 'content': prompt}],
        temperature=0.2,
    )
    raw = resp.choices[0].message.content.strip()
    log.info(f"[GPT-refine] preview: {raw[:120]}")
    s = raw.find('['); e = raw.rfind(']') + 1
    if s == -1 or e == 0:
        raise ValueError(f"GPT bad format: {raw[:150]}")
    return json.loads(raw[s:e])

def refine_darija(segments):
    texts = [s['text'] for s in segments]
    try:
        improved = _gemini_refine(texts)
        engine = 'gemini-2.0-flash'
        log.info(f"[Refine] Gemini OK — {len(improved)} segments")
    except Exception as ge:
        log.warning(f"[Refine] Gemini failed ({ge}), falling back to GPT-4o-mini")
        try:
            improved = _gpt_refine(texts)
            engine = 'gpt-4o-mini'
            log.info(f"[Refine] GPT-4o-mini OK — {len(improved)} segments")
        except Exception as gpe:
            log.error(f"[Refine] Both failed ({gpe}) — keeping Whisper originals")
            return [s['text'] for s in segments], 'whisper-only'
    while len(improved) < len(segments):
        improved.append(segments[len(improved)]['text'])
    return improved[:len(segments)], engine

# ── Hybrid pipeline ─────────────────────────────────────────────────────────
def hybrid_transcribe(audio_path):
    segs = whisper_transcribe(audio_path)
    improved, engine = refine_darija(segs)
    final = [
        {'id': s['id'], 'start': s['start'], 'end': s['end'],
         'text': improved[i],           # ← improved Darija
         'whisper_text': s['text'],      # ← original Whisper output
         'refine_engine': engine}
        for i, s in enumerate(segs)
    ]
    log.info(f"[Hybrid] {len(final)} segments via {engine}")
    return final, engine

# ── Translation ────────────────────────────────────────────────────────────
LANG_NAMES = {
    'arabic':  'Modern Standard Arabic (فصحى)',
    'french':  'French',
    'english': 'English',
}

def translate_captions(segments, target_lang):
    lang_name = LANG_NAMES.get(target_lang, target_lang)
    texts = [s['text'].strip() for s in segments]
    log.info(f"[Translate] {len(texts)} → {lang_name}")
    prompt = (
        f"Translate each line to {lang_name}. Keep exact count and order.\n"
        f"Return ONLY a valid JSON array, no explanation, no markdown.\n\n"
        f"Lines:\n{json.dumps(texts, ensure_ascii=False)}"
    )
    resp = openai_client.chat.completions.create(
        model='gpt-4o-mini',
        messages=[{'role': 'user', 'content': prompt}],
        temperature=0.3,
    )
    raw = resp.choices[0].message.content.strip()
    s = raw.find('['); e = raw.rfind(']') + 1
    if s == -1 or e == 0:
        raise ValueError(f"Translation bad format: {raw[:200]}")
    return json.loads(raw[s:e])

# ── SRT helper ──────────────────────────────────────────────────────────────
def srt_time(sec):
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace('.', ',')

def make_srt(segments):
    lines = []
    for i, seg in enumerate(segments, 1):
        lines += [
            str(i),
            f"{srt_time(seg['start'])} --> {srt_time(seg['end'])}",
            seg['text'].strip(), '',
        ]
    return '\n'.join(lines)

# ── Routes ─────────────────────────────────────────────────────────────────
@app.route('/')
def index(): return render_template('index.html')

@app.route('/uploads/<path:filename>')
def serve_upload(filename): return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/api/transcribe', methods=['POST'])
def api_transcribe():
    log.info(f"POST /api/transcribe — files: {list(request.files.keys())}")
    try:
        if 'file' not in request.files:
            return json_error('No file field in request', 400)
        f = request.files['file']
        if not f or not f.filename:
            return json_error('Empty file upload', 400)
        if not allowed_file(f.filename):
            return json_error(f'Unsupported file type: {f.filename}', 400)

        ts = str(int(time.time()))
        ext = f.filename.rsplit('.', 1)[1].lower()
        name = f'media_{ts}.{ext}'

        # Save to /tmp (local disk — required for ffmpeg)
        tmp = f'/tmp/{name}'
        f.save(tmp)
        if not os.path.exists(tmp):
            raise RuntimeError(f"Upload save failed: {tmp}")
        if os.path.getsize(tmp) == 0:
            raise RuntimeError("Uploaded file is 0 bytes")
        log.info(f"Saved to /tmp: {name} ({os.path.getsize(tmp):,} bytes)")

        # Copy to uploads/ for browser access
        serve = os.path.join(UPLOAD_FOLDER, name)
        with open(tmp, 'rb') as src, open(serve, 'wb') as dst:
            dst.write(src.read())

        # Extract audio + run hybrid pipeline
        audio = extract_audio(tmp)
        segments, engine = hybrid_transcribe(audio)

        # Cleanup /tmp
        for p in [tmp, audio]:
            try: os.remove(p)
            except: pass

        is_video = ext in {'mp4', 'webm', 'mov', 'avi', 'mkv'}
        return jsonify({
            'segments':      segments,
            'media_url':      f'/uploads/{name}',
            'media_type':     'video' if is_video else 'audio',
            'refine_engine':  engine,
        })

    except Exception as e:
        log.error(f"/api/transcribe FAILED: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return json_error(f'{type(e).__name__}: {str(e)}', 500)

@app.route('/api/translate', methods=['POST'])
def api_translate():
    try:
        data = request.get_json(force=True, silent=True) or {}
        segs = data.get('segments', [])
        tgt  = data.get('target_lang', 'english')
        if not segs:
            return json_error('No segments provided', 400)
        return jsonify({'translations': translate_captions(segs, tgt)})
    except Exception as e:
        log.error(f"/api/translate FAILED: {e}")
        return json_error(str(e), 500)

@app.route('/api/export_srt', methods=['POST'])
def api_export_srt():
    try:
        data = request.get_json(force=True, silent=True) or {}
        return Response(
            make_srt(data.get('segments', [])),
            mimetype='text/plain',
            headers={'Content-Disposition': 'attachment; filename=captions.srt'},
        )
    except Exception as e:
        return json_error(str(e), 500)

@app.route('/api/health')
def health():
    return jsonify({
        'status':       'ok',
        'openai_key':   bool(len(OPENAI_KEY) > 10),
        'gemini_key':   bool(len(GEMINI_KEY) > 10),
        'proxy':        bool(CF_TOKEN),
        'pipeline':     'whisper + gemini/gpt-darija-refine',
    })

if __name__ == '__main__':
    log.info("=== Darija Caption Studio v2 — Hybrid Pipeline ===")
    log.info(f"Port: {PORT} | Upload folder: {UPLOAD_FOLDER}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
