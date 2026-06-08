# 鈦金運彩（sport-web）

獨立體育預測專案，與百家樂 `baccarat-web` 分開部署。

## 本地啟動

```bat
start.bat
```

瀏覽器：http://127.0.0.1:18011

## 部署 Render（與百家樂相同流程）

### 1. 推到 GitHub

```bash
cd 體育
git init
git add .
git commit -m "init sport-web"
git branch -M main
git remote add origin https://github.com/ShawnTsai83/sport-web.git
git push -u origin main
```

> 若 GitHub 還沒有 `sport-web` 倉庫，先到 GitHub 建立 **New repository** → 名稱 `sport-web`（空白專案即可）。

### 2. Render 建立新服務

1. 登入 [Render Dashboard](https://dashboard.render.com)
2. 點 **+ New** → **Web Service**
3. 連接 GitHub 倉庫 `ShawnTsai83/sport-web`
4. 設定：
   - **Name**: `sport-web`
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn web_api:app --host 0.0.0.0 --port $PORT`
5. **Environment Variables** 新增：
   - `ODDS_API_KEY` = 你的 The Odds API 金鑰
6. 點 **Create Web Service**

部署完成後網址類似：`https://sport-web.onrender.com`

### 預設管理員帳號

| 帳號 | 密碼 |
|------|------|
| admin01 | admin123456 |
| master01 | master123456 |

## API 額度節省（重要）

The Odds API 免費每月 500 次，每次手動更新約扣 **6 次**（6 個聯盟）。

- **登入不會扣 API**；先前額度消耗快，主因是 Render Free 休眠重啟後啟動會自動打 API
- 已改為：**啟動只讀快取，不自動打 API**
- 倉庫內附 `odds_cache_seed.json`，重啟後仍有賽事可顯示
- 「更新賽事資料」有 **24 小時冷卻**（未到期沿用快取、不扣額度）
- 需立即拉最新時，用「**強制更新**」（會扣約 6 次）
- **Render 休眠醒來不會自動打 API**（只讀快取）

環境變數可調：`ODDS_REFRESH_COOLDOWN_HOURS=12`（預設 24）

## 注意

- `members.db` 在 Render Free 重啟後可能重置（與百家樂相同）
- 賽事資料需管理員按「更新賽事資料」或「強制更新」
