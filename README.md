# 🎙 Darija Caption Studio

**Hybrid Whisper + AI Moroccan Darija caption generator**

Upload any audio or video file to get AI-generated Moroccan Darija captions with timestamps, improved by GPT-4o-mini (or Gemini 2.0 Flash), with optional translation to English, French, or Arabic and export to SRT/VTT/TXT/JSON.

---

## 🏗️ Architecture

```
Audio/Video Upload
       ↓
  Flask Server
       ↓
  ffmpeg (→ /tmp audio extraction)
       ↓
  OpenAI Whisper-1 (raw Darija timestamps)
       ↓
  GPT-4o-mini  OR  Gemini 2.0 Flash
  (Darija language refinement)
       ↓
  JSON: { segments: [{id, start, end, text, whisper_text, refine_engine}] }
       ↓
  Frontend editor + translation + export
```

---

## 🚀 Deploy to Vercel (Recommended)

### Prerequisites
- [Vercel account](https://vercel.com) (free, no credit card for hobby tier)
- [OpenAI API key](https://platform.openai.com/api-keys)
- Optional: [Gemini API key](https://aistudio.google.com/app/apikey)

### Step 1 — Push to GitHub

```bash
git clone https://github.com/YOUR_USERNAME/darija-caption-studio.git
cd darija-caption-studio
git init
git add .
git commit -m "Darija Caption Studio v2"
git push -u origin main
```

### Step 2 — Deploy on Vercel

```bash
npm i -g vercel
vercel --prod
```

Or via the dashboard: [vercel.com](https://vercel.com) → Import → connect GitHub repo → Add Environment Variables → Deploy.

### Step 3 — Set Environment Variables

In Vercel dashboard → Settings → Environment Variables:

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | Your OpenAI API key |
| `OPENAI_BASE_URL` | ❌ | Proxy base URL (optional) |
| `GEMINI_API_KEY` | ❌ | Gemini API key (GPT fallback used if omitted) |
| `CF_AIG_TOKEN` | ❌ | Proxy auth token (optional) |

### Step 4 — Done! 🎉

Your app will be live at: `https://darija-caption-studio.vercel.app`

---

## 🐳 Deploy to Fly.io

```bash
# Install flyctl
curl -fsSL https://fly.io/install.sh | sh
export PATH="$HOME/.fly/bin:$PATH"

# Login
flyctl auth login

# Set secrets
flyctl secrets set OPENAI_API_KEY___="sk-..." --app darija-caption-studio
flyctl secrets set GEMINI_API_KEY___="..." --app darija-caption-studio

# Deploy
flyctl deploy --app darija-caption-studio
```

Your app will be live at: `https://darija-caption-studio.fly.dev`

---

## 🧪 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Full interactive editor UI |
| `POST` | `/api/transcribe` | Upload file → `{segments, media_url, media_type, refine_engine}` |
| `POST` | `/api/translate` | `{segments, target_lang}` → `{translations: [...]}` |
| `POST` | `/api/export_srt` | `{segments}` → SRT file download |
| `GET` | `/api/health` | Server status check |

### Example — Transcribe

```bash
curl -X POST https://your-app.vercel.app/api/transcribe \
  -F "file=@audio.mp3"
```

Returns:
```json
{
  "segments": [
    {
      "id": 0,
      "start": 0.0,
      "end": 5.0,
      "text": "هاد الفيديو غادي نشرح ليكم...",
      "whisper_text": "هدا الفيديو غادي نشرح لكم...",
      "refine_engine": "gpt-4o-mini"
    }
  ],
  "media_url": "/uploads/media_1234567890.mp3",
  "media_type": "audio",
  "refine_engine": "gpt-4o-mini"
}
```

---

## ⚙️ Hybrid Pipeline Detail

### Stage 1 — Whisper Transcription
- Model: `whisper-1`
- Language hint: `ar` (Arabic)
- Format: `verbose_json` with `timestamp_granularities=['segment']`
- Output: raw Arabic/Darija text with precise start/end timestamps

### Stage 2 — Darija Language Refinement
- **Primary**: Gemini 2.0 Flash via REST API
- **Fallback**: GPT-4o-mini (used automatically if Gemini fails/quota exceeded)
- Prompt: Rewrites raw Whisper output into clean, natural Moroccan Darija
  - Fixes spelling and grammar
  - Replaces non-Darija words with Darija equivalents
  - Removes filler words and repetition
  - Preserves meaning and segment length
- Timestamps are **never modified** — only the text changes

### Stage 3 — Merge & Return
- Each segment: `text` (improved Darija) + `whisper_text` (original Whisper)
- Both versions available for display/comparison

---

## ⚠️ Critical: /tmp workaround

This app writes **all uploaded files** to `/tmp/` before processing. This is required because:

1. **ffmpeg** needs `seek()` to write MP3/Vorbis headers — network filesystems (Vercel, Fly.io, etc.) don't support this
2. The file is copied to `/uploads/` (persistent storage) only **after** `/tmp` write succeeds

On **Vercel** (ephemeral FS): `/tmp` works for the request lifecycle, uploads folder uses Vercel's disk storage.

---

## 📝 License

MIT — Zayd Chguiri / CREAO Platform