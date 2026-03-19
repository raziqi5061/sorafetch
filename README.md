# SoraFetch — Sora Video Downloader

Ek complete Sora video downloader jisme FastAPI backend aur modern frontend hai.

---

## 📁 Files Structure

```
sora-downloader/
├── main.py          ← FastAPI backend (Python)
├── requirements.txt ← Python dependencies
├── index.html       ← Frontend (browser mein kholein)
└── README.md        ← Ye file
```

---

## 🚀 Setup — Step by Step

### Step 1: Python install karein (agar nahi hai)
```
https://python.org/downloads — Python 3.10+ chahiye
```

### Step 2: Dependencies install karein
```bash
pip install -r requirements.txt
```

### Step 3: Backend start karein
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Terminal mein ye dikhega:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### Step 4: Frontend kholein
- `index.html` file browser mein double-click karke kholein
- Ya: `http://localhost:8000` pe bhi kaam karega

### Step 5: Video download karein
- API Server box mein `http://localhost:8000` likha hona chahiye
- Status **✓ Online** dikhni chahiye
- Sora video link paste karein aur Download dabayein!

---

## 🌐 Online Deploy (free)

### Railway pe deploy karein:
1. https://railway.app account banayein
2. New project → Deploy from GitHub
3. `main.py` aur `requirements.txt` upload karein
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Aapko ek public URL milegi — woh `index.html` ke API box mein paste karein

### Render pe deploy karein:
1. https://render.com account banayein
2. New Web Service → Connect repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port 10000`

---

## ⚠️ Note

Sora videos download karne ke liye aapke paas video ka direct CDN link hona chahiye.
Agar Sora page ka link hai (sora.com/videos/...) toh backend automatically redirect follow karta hai.

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Backend online check |
| `/info?url=...` | GET | Video info fetch karo |
| `/download?url=...` | GET | Video stream karo |
| `/batch/info` | POST | Multiple videos ka info |
