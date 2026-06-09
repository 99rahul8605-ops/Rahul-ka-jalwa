# 📥 Google Drive Telegram Bot

A Telegram bot that downloads files from Google Drive links and sends them back to you.

---

## 🚀 Deploy on Render

### Step 1 — Get a Bot Token
1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the steps
3. Copy the **token** it gives you

### Step 2 — Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### Step 3 — Deploy on Render
1. Go to [render.com](https://render.com) → **New → Web Service**
2. Connect your GitHub repo
3. Set these settings:
   - **Environment**: Docker
   - **Region**: Choose closest to you
   - **Instance Type**: Free (or Starter)
4. Under **Environment Variables**, add:
   | Key | Value |
   |-----|-------|
   | `BOT_TOKEN` | `your_telegram_bot_token_here` |
5. Click **Deploy**

### Step 4 — Done! 🎉
Send your bot a Google Drive link and it will download and return the file.

---

## 🔧 Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set your token
export BOT_TOKEN="your_token_here"

# Run the bot
python main.py
```

---

## 📋 Features
- ✅ Downloads public Google Drive files
- ✅ Downloads Google Drive folders (multiple files)
- ✅ Handles sharing links in all formats
- ✅ Health check server for Render port detection
- ✅ Temp files cleaned up automatically

## ⚠️ Limitations
- Files must be **publicly shared** ("Anyone with the link")
- Max file size: **50MB** (Telegram Bot API limit)
- Very large folders may time out
